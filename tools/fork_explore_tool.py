"""fork_explore — dry-rollout развилки направлений внутри одной сессии
(PR-1 каркас + PR-2 scoring pipeline).

Единица форка — ТРАЕКТОРИЯ (куда повернёт работа), не ответ. Инструмент
получает общий большой контекст + развилку + критерии приёмки направления,
прогоняет по каждой ветке dry-роллаут глубиной 1..N модельных шагов, судит
слепым pairwise-судьёй (swap для контроля позиционного bias), даёт родиться
пути В из сравнения (synthesize), и возвращает в главный контекст ОДИН
санитизированный JoinPacket ≤ join_max_tokens. Полные трассы — в fork journal
(sqlite), в контекст не попадают.

Read-only по построению: в ветке исполняется ТОЛЬКО chat-вызов модели и
ограниченное чтение файлов из read_roots; ни одного write-пути в коде
роллаута. Желания ветки оформляет intents (needs_approval для необратимых),
выполняет их главная сессия после join.

Design doc (путь к cache-fork через SGLang RadixAttention): docs/design/fork-explore.md.
Spec: ~/projects/fork-reasoning/SPEC.md (v1.0, 17.10.2026).
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Optional

from tools.fork_explore_rollout import ContinueFlag  # contract v4: пошаговая пауза
from tools.fork_explore_scoring import (  # scoring pipeline (PR-2, SPEC §7):
    # hard gates → clustering → blind swap-pairwise judge → aggregate with margin
    _digest_text, cluster_traces, hard_gates, judge_pair, majority_reached,
    passing_traces, tournament)

logger = logging.getLogger(__name__)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_READ_CONTENT_CAP = 8000
_MAX_READS_PER_BRANCH = 2

SYNTH_SYSTEM = """Ты — синтезатор развилки. Тебе даны траектории двух лучших путей, их \
сильные стороны и фатальные слабости (судейские замечания). Предложи НОВЫЙ путь В, \
лишённый слабостей A и Б и сохраняющий их силы, либо откажись.
Верни ТОЛЬКО JSON: {"propose_new_path": true|false, "label": "короткое имя", \
"strategy": "формулировка пути для ветки (2-4 предложения)", "rationale": "почему он сильнее"}"""


# --------------------------------------------------------------------------- chat

# Providers that 400 on chat_template_kwargs (non-SGLang/vLLM relays) are memoed
# for the process; the first call falls back once, later calls skip the field —
# same ladder shape as agent/auxiliary_structured_output's response_format memo.
_CHAT_TEMPLATE_KWARGS_REJECTED = False


def _chat(client: Any, model: str, messages: list[dict], *, max_tokens: int,
          temperature: float, timeout: float, seed: Optional[int] = None,
          no_think: bool = True) -> tuple[str, int]:
    """One OpenAI-compatible chat call; returns (content, completion_tokens).
    Abandoning the future on deadline is our cancel — the server stops the
    generation when the client connection closes (verified live:
    scripts/fork_cancel_loadtest.py PASS, slot freed in 0.5s).

    Thinking models (GLM/Qwen on SGLang) bill reasoning_tokens INSIDE the
    completion budget and truncate the JSON answer at tokens_per_step_max —
    verified live on :8012 (finish=length, reasoning 58/70). Fork steps and
    judge verdicts are cheap structured turns, so thinking is disabled via
    chat_template_kwargs.enable_thinking; providers that reject the parameter
    fall back to plain calls once and remember.
    """
    global _CHAT_TEMPLATE_KWARGS_REJECTED
    kwargs: dict[str, Any] = {"model": model, "messages": messages,
                              "temperature": temperature, "max_tokens": max_tokens,
                              "timeout": max(1.0, timeout),
                              "response_format": {"type": "json_object"}}
    if seed is not None:
        kwargs["seed"] = seed
    if no_think and not _CHAT_TEMPLATE_KWARGS_REJECTED:
        kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as exc:  # only to classify a param rejection; else re-raise
        text = str(exc).lower()
        if "extra_body" in kwargs and ("chat_template_kwargs" in text
                                       or "enable_thinking" in text):
            _CHAT_TEMPLATE_KWARGS_REJECTED = True
            logger.info("fork_explore: endpoint rejects chat_template_kwargs; "
                        "continuing with thinking enabled")
            kwargs.pop("extra_body")
            resp = client.chat.completions.create(**kwargs)
        else:
            raise
    usage = getattr(resp, "usage", None)
    tokens = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
    return (resp.choices[0].message.content or ""), tokens


def _parse_json(text: str) -> Optional[dict]:
    m = _JSON_RE.search(text or "")
    if not m:
        return None
    try:
        val = json.loads(m.group(0))
    except ValueError:
        return None
    return val if isinstance(val, dict) else None


# --------------------------------------------------------------------------- branch rollout

def _safe_read(path_str: str, roots: list[str]) -> Optional[str]:
    """Read-only tool of a branch, bounded by construction: absolute path must
    live under one of read_roots (.. cannot escape), result capped."""
    import os
    if not roots or not path_str or len(path_str) > 512:
        return None
    real = os.path.realpath(path_str)
    for root in roots:
        rroot = os.path.realpath(root)
        try:
            if os.path.commonpath([real, rroot]) != rroot:
                continue
        except ValueError:
            continue
        try:
            with open(real, "r", encoding="utf-8-sig", errors="replace") as fh:
                return fh.read(_READ_CONTENT_CAP)
        except OSError:
            return None
    return None


def run_branch(client: Any, model: str, prefix: list[dict], ref: str, strategy: str,
               cfg: dict, deadline: float, chat: Optional[Callable] = None, *,
               facts: Optional[list[str]] = None,
               cont: Optional[ContinueFlag] = None,
               join_id: str = "") -> dict[str, Any]:
    """Simulated rollout of one path — contract v4: ПОШАГОВАЯ ПАУЗА.

    Each model step ends at a boundary where the branch stands still; the
    inter-step gate (tools/fork_explore_rollout.py) decides «continue» vs
    «stop» BEFORE the next prefill+decode is paid for, and every boundary
    checkpoints trace/бюджет/step_index into the fork journal. depth_default
    is the plan, depth_max only the ceiling (selective deepening past the
    plan requires a novel step). External preemption rides the same primitive:
    ``cont`` (ContinueFlag) — a control-socket verb later just flips it.
    Read-only by construction: chat + bounded reads; writes exist as intents.
    """
    from tools.fork_explore_journal import write_checkpoint
    from tools.fork_explore_rollout import BranchRoller
    checkpoint = ((lambda cp, jid=join_id: write_checkpoint(jid, cp))
                  if join_id else None)
    roller = BranchRoller(client, model, cfg, deadline,
                          facts=facts, cont=(cont.get if cont else None),
                          checkpoint=checkpoint, chat=chat)
    return roller.run(prefix, ref, strategy)


# Per-branch continue flags of in-flight forks: join_id -> {ref -> ContinueFlag}.
# The future control-socket verb «не планировать следующий шаг» (сшивка с
# task-b78849c10d) is just request_stop() below — no second mechanism.
_ACTIVE_FORK_FLAGS: dict[str, dict[str, ContinueFlag]] = {}


def request_stop(join_id: str, ref: str = "") -> int:
    """Внешний преэмпт живых веток форка: опустить per-branch continue-flag.
    Ветка остановится на своей следующей границе шага (не посреди decode).
    ref="" — все ветки форка. Возвращает число ФАКТИЧЕСКИ переведённых
    in-stop флагов (идемпотентно: уже стоящая on-stop ветка не считается)."""
    flags = _ACTIVE_FORK_FLAGS.get(join_id) or {}
    targets = ([flags[ref]] if ref in flags
               else ([] if ref else list(flags.values())))
    stopped = sum(1 for f in targets if f.get())
    for f in targets:
        f.stop()
    return stopped


# --------------------------------------------------------------------------- synthesize (путь В)
# hard gates / clustering / blind judge / aggregate live in tools/fork_explore_scoring.py

def maybe_synthesize(cfg: dict, synth_chat: Callable, task: str, criteria: str,
                     ranked: list[dict], flaws: dict[str, list]) -> Optional[dict]:
    if not cfg.get("synthesize") or len(ranked) < 2:
        return None
    blocks = []
    for t in ranked[:2]:
        blocks.append(f"ПУТЬ {t['ref']}: {t.get('strategy_text','')[:200]}\n"
                      f"ТРАЕКТОРИЯ:\n{_digest_text(t)}\n"
                      f"СЛАБОСТИ (судья): {json.dumps(flaws.get(t['ref'], []), ensure_ascii=False)[:400]}")
    user = (f"РАЗВИЛКА:\n{task[:1200]}\n\nКРИТЕРИИ:\n{criteria[:800]}\n\n"
            + "\n\n".join(blocks)
            + "\n\nПредложи новый путь В (или отказ).")
    try:
        content, _used = synth_chat([{"role": "system", "content": SYNTH_SYSTEM},
                                     {"role": "user", "content": user}], max_tokens=800)
        parsed = _parse_json(content) or {}
        if parsed.get("propose_new_path") and str(parsed.get("strategy", "")).strip():
            return {"ref": "synth", "label": str(parsed.get("label", "В"))[:60],
                    "strategy": str(parsed["strategy"])[:600]}
    except (OSError, ValueError, TimeoutError, RuntimeError) as exc:
        # synthesis is opportunistic: any call/parse failure = no path В, never a fork failure
        logger.debug("fork synthesize failed: %s", exc, exc_info=True)
    return None


# --------------------------------------------------------------------------- main entry

def _new_join_id() -> str:
    return time.strftime("fj_%Y%m%d_%H%M%S_") + secrets.token_hex(2)


def resolve_rollout_depth(cfg: dict, requested: Any) -> int:
    """depth — ПОТОЛОК, не семантика (приёмка PR-2): запрошенная глубина
    зажимается в [1, depth_max]; отсутствующая/нечислимая → depth_default
    (тоже под потолком). Ветка вправе остановиться раньше (infeasible/stop/
    deadline/early-stop) — потолок это только разрешает."""
    fallback = int(cfg["depth_default"])
    try:
        want = fallback if requested is None else int(requested)
    except (TypeError, ValueError):
        want = fallback
    return max(1, min(int(cfg["depth_max"]), want))


def _resolve_base_version(args: dict, session_id: str) -> Optional[int]:
    bv = args.get("base_version")
    if isinstance(bv, int):
        return bv
    if not session_id:
        return None
    try:  # version-lite: last message uid of the session, best-effort
        from hermes_state import SessionDB
        from hermes_constants import get_hermes_home
        db = SessionDB(get_hermes_home() / "state.db")
        uid = db.get_last_message_uid(session_id)
        return int(uid) if uid and str(uid).isdigit() else None
    except (ImportError, OSError, sqlite3.Error, ValueError) as exc:
        # base_version is provenance, not behaviour: a missing db is fine
        logger.debug("fork base_version lookup failed: %s", exc, exc_info=True)
        return None


def _judge_and_synthesize(cfg: dict, task: str, criteria: str, prefix: list, deadline: float,
                          client: Any, model: str, judge_client: Any, judge_model: str,
                          traces: list, facts: list[str], join_id: str = ""
                          ) -> tuple[str, list, dict, list, list]:
    """PR-2 scoring pipeline (SPEC §7): hard gates → trajectory clustering →
    blind swap-pairwise judge on cluster reps → aggregate S = w_j*J + w_c*C − w_$*cost.
    Returns (strategy, ranked_traces, flaws_by_ref, open_issues, clusters);
    mutates traces (a synthesized В joins them as a full participant: the same
    gates — including facts_ok — and the same journal)."""
    open_issues: list[str] = []
    if judge_model and judge_model == model:
        # SPEC 7.3 self-preference: the judge should be another model than the
        # branches. The fork still runs (blindness + swap control bias), but the
        # main session must see that the ranking is not cross-model verified.
        open_issues.append("судья = модель веток (self-preference риск, SPEC 7.3) — "
                           "для критичных решений задайте fork_explore.judge отдельно")
    passing = passing_traces(traces, cfg, facts)
    if not passing:
        # Reject still journals the fork STRUCTURE: clustering runs over every
        # landed trace (the judge simply never sees gate-failed routes).
        clusters = [{"rep": c["rep"], "members": list(c["members"])}
                    for c in cluster_traces(
                        traces, float(cfg.get("cluster_similarity", 0.8)))]
        return ("reject", [], {},
                open_issues + ["ни одна ветка не прошла hard gates"], clusters)
    fact_gated = sorted(t["ref"] for t in traces if not t["gates"]["facts_ok"])
    if fact_gated:
        open_issues.append("отсеяны гейтом противоречия фактам: " + ", ".join(fact_gated))

    jmt = int((cfg.get("judge") or {}).get("max_tokens") or 4000)

    def judge_chat(msgs, max_tokens=None):
        return _chat(judge_client, judge_model, msgs,
                     max_tokens=int(max_tokens or jmt), temperature=0.0,
                     timeout=min(60.0, max(1.0, deadline - time.monotonic())),
                     no_think=bool((cfg.get("judge") or {}).get("disable_thinking",
                                                                True)))

    ranked, meta = tournament(judge_chat, task, criteria, passing, facts,
                              float(cfg.get("cluster_similarity", 0.8)),
                              cfg.get("score_weights") or {}, jmt)
    flaws, clusters = meta["flaws"], meta["clusters"]
    top, second = ranked[0], (ranked[1] if len(ranked) > 1 else None)
    margin = round(top["score"] - (second["score"] if second else 0.0), 3)
    if meta.get("judge_degraded"):
        strategy = "select_with_caveat"
        open_issues.append("судья недоступен — победитель выбран детерминированно "
                           "(первая прошедшая ветка), ранжирование не проверено")
        for t in passing:
            t["score"] = 0.0
        ranked = sorted(passing, key=lambda t: t["ref"])
        margin = 0.0
    elif margin < float(cfg["min_margin_delta"]):
        strategy = "select_with_caveat"
        open_issues.append(f"margin {margin} < min_margin_delta {cfg['min_margin_delta']}")
    else:
        strategy = "select"
    if meta.get("inconsistent_pairs"):
        open_issues.append(f"судья дал {meta['inconsistent_pairs']} несогласованных пар (position flip)")
    if second is None and strategy == "select" and len(passing) > 1:
        strategy = "select_with_caveat"

    # путь В: синтез из прав/слабостей A и Б, обязанный пройти ТЕ ЖЕ гейты
    def synth_chat(msgs, max_tokens):
        return _chat(client, model, msgs, max_tokens=max_tokens,
                     temperature=float(cfg["temperature"]),
                     timeout=min(60.0, max(1.0, deadline - time.monotonic())),
                     no_think=bool(cfg.get("disable_thinking", True)))

    synth = maybe_synthesize(cfg, synth_chat, task, criteria, ranked,
                             {t["ref"]: flaws.get(t["ref"], []) for t in ranked})
    if synth:
        st = run_branch(client, model, prefix, synth["ref"], synth["strategy"],
                        cfg, deadline, facts=facts, join_id=join_id)
        st["gates"] = hard_gates(st, cfg, facts)
        st["gates_ok"] = all(st["gates"].values())
        st["failed_gates"] = [k for k, v in st["gates"].items() if not v]
        traces.append(st)  # путь В — полноправный участник: тот же журнал, те же гейты
        if st["gates_ok"]:
            w, _r, _fl, deg = judge_pair(judge_chat, task, criteria, st, ranked[0], facts, jmt)
            if not deg and w == st["ref"]:
                st["score"] = round(ranked[0]["score"] + float(cfg["min_margin_delta"]), 3)
                ranked.insert(0, st)
                strategy = "synthesize"
    return strategy, ranked, flaws, open_issues, clusters


def fork_explore(args: dict, session_id: str = "") -> str:
    from tools.fork_explore_config import load_fork_config, resolve_chat_client
    from tools.fork_explore_guard import (build_packet, collect_intents, enforce_size,
                                          sanitize)
    from tools.fork_explore_journal import session_tokens_used, write_run
    from tools.fork_explore_prefix import build_shared_prefix, dedup_paths, prefix_hash

    cfg = load_fork_config()
    if not cfg.get("enabled", True):
        return json.dumps({"ok": False, "error": "fork_explore disabled (config fork_explore.enabled)"})

    task = str(args.get("task") or "").strip()
    shared_context = str(args.get("shared_context") or "").strip()
    raw_paths = args.get("paths") or []
    criteria_list = [str(c) for c in (args.get("acceptance_direction") or []) if str(c).strip()]
    facts = [str(f) for f in (args.get("confirmed_facts") or []) if str(f).strip()]
    if not task or not shared_context or len(raw_paths) < 2:
        return json.dumps({"ok": False,
                           "error": "task, shared_context and >=2 paths are required"})
    criteria = "\n".join(f"- {c}" for c in criteria_list) or "- направление должно быть выполнимо"

    paths = []
    for i, p in enumerate(raw_paths[: int(cfg["k_max"])]):
        if isinstance(p, dict):
            paths.append({"ref": str(p.get("ref") or f"b{i + 1}")[:16],
                          "strategy": str(p.get("strategy") or p.get("label") or "")[:1200]})
        else:
            paths.append({"ref": f"b{i + 1}", "strategy": str(p)[:1200]})
    paths = dedup_paths(paths)
    if len(paths) < 2:
        return json.dumps({"ok": False, "error": "paths deduplicated to <2 (too similar)"})

    # depth — потолок, не семантика: запрошенная глубина зажимается к
    # depth_max и на этом форке становится ПОТОЛКОМ (ветка вправе встать
    # раньше на любой границе шага); без запроса — план depth_default с
    # избирательным углублением до depth_max (contract v4).
    depth = resolve_rollout_depth(cfg, args.get("depth"))
    cfg = dict(cfg, depth_default=depth)
    if args.get("depth") is not None:
        cfg = dict(cfg, depth_max=depth)

    # budget-lite: refuse before burning if this session's 24h fork spend is exhausted
    spent = session_tokens_used(session_id)
    est_cost = len(paths) * int(cfg["depth_default"]) * int(cfg["tokens_per_step_max"])
    if spent + est_cost > int(cfg["session_token_budget"]):
        return json.dumps({"ok": False, "error": f"fork budget exhausted: {spent} tokens used in 24h",
                           "session_token_budget": cfg["session_token_budget"]})

    client, model = resolve_chat_client(cfg.get("branch_endpoint") or {}, "fork_explore")
    if client is None or not model:
        return json.dumps({"ok": False, "error": "no chat endpoint/model "
                           "(set fork_explore.branch_endpoint or a working main model)"})
    jcfg = cfg.get("judge") or {}
    judge_client, judge_model = resolve_chat_client(jcfg, "fork_explore_judge")
    if judge_client is None:
        judge_client, judge_model = client, model
        logger.info("fork_explore: judge endpoint unavailable, falling back to branch model")

    prefix = build_shared_prefix(task, str(args.get("fork_point") or ""), shared_context,
                                 criteria_list, facts)
    phash = prefix_hash(prefix)
    join_id = _new_join_id()
    deadline = time.monotonic() + float(cfg["fork_deadline_s"])

    def branch_job(p, flag):
        return run_branch(client, model, prefix, p["ref"], p["strategy"], cfg,
                          deadline, facts=facts, cont=flag, join_id=join_id)

    t_start = time.monotonic()
    traces: list = []
    # Пошаговая пауза (contract v4): у каждой живой ветки свой continue-flag;
    # внешний преэмпт (request_stop) гасит его, ветка встаёт на границе шага.
    flags = {p["ref"]: ContinueFlag() for p in paths}
    _ACTIVE_FORK_FLAGS[join_id] = flags
    # Early stop (SPEC 5.4): once ⌈k/2⌉+1 equivalent gate-passing branches have
    # landed, queued branches are cancelled (running ones keep their own deadline
    # — an in-flight HTTP call is only freed by disconnect; see design doc).
    try:
        with ThreadPoolExecutor(max_workers=min(int(cfg["k_max"]), len(paths))) as pool:
            futures = [pool.submit(branch_job, p, flags[p["ref"]]) for p in paths]
            try:
                for fut in as_completed(futures, timeout=max(1.0, deadline - time.monotonic())):
                    try:
                        traces.append(fut.result())
                    except (TimeoutError, RuntimeError, OSError) as exc:
                        logger.debug("fork branch dropped before join: %s", exc, exc_info=True)
                    if bool(cfg.get("early_stop_consensus", True)) and \
                            majority_reached(traces, cfg, facts, len(paths)):
                        for other in futures:
                            other.cancel()
            except TimeoutError:
                # fan-out deadline: unfinished branches exit the fork (wasted tokens
                # they already burned stay charged in the journal)
                logger.debug("fork fan-out deadline reached with %d/%d traces",
                             len(traces), len(paths), exc_info=True)
    finally:
        _ACTIVE_FORK_FLAGS.pop(join_id, None)

    strategy, ranked, _flaws, open_issues, clusters = _judge_and_synthesize(
        cfg, task, criteria, prefix, deadline, client, model,
        judge_client, judge_model or model, traces, facts, join_id)
    tokens_total = sum(t["tokens"] for t in traces)

    per_branch = [{
        "ref": t["ref"], "strategy": t["strategy_text"][:160],
        "status": ("winner" if (ranked and ranked[0] is t) else
                   ("gate_failed" if not t["gates_ok"] else "rejected")),
        "stop_reason": t["stop_reason"], "score": t.get("score", 0),
        "tokens": t["tokens"], "latency_ms": t["latency_ms"],
        "gates": t["gates"],
        **({"cluster_size": t["cluster_size"]} if t.get("cluster_size") else {}),
        "summary": str((t["steps"][0].get("content") if t["steps"] else "") or "")[:200],
        "key_findings": [str(s.get("content", ""))[:160] for s in t["steps"]
                         if str(s.get("kind", "")) == "decision"][:5],
        "risks": sorted({str(r)[:160] for s in t["steps"] for r in (s.get("risks") or [])})[:5],
    } for t in traces]
    winner_ref = ranked[0]["ref"] if ranked and strategy != "reject" else None
    winner = None
    if winner_ref is not None:
        top = ranked[0]
        second_score = ranked[1].get("score", 0) if len(ranked) > 1 else 0.0
        winner = {"path_ref": winner_ref, "score": top.get("score", 0),
                  "margin": round(top.get("score", 0) - second_score, 3),
                  "summary": per_branch[next(i for i, t in enumerate(traces) if t["ref"] == winner_ref)]["summary"],
                  "direction": top["strategy_text"]}
    winner_trace = next((t for t in traces if t["ref"] == winner_ref), {"intents": []})
    intents = collect_intents([winner_trace], winner_ref or "", session_id, join_id)
    # проигравшие ветки тоже могли найти полезные reversible-intents — дедуп в общий список
    intents += [i for i in collect_intents([t for t in traces if t["ref"] != winner_ref],
                                           "", session_id, join_id)
                if not i["needs_approval"]][:4]
    rejected = [{"branch_ref": b["ref"],
                 "reason": ("hard gates: " + ",".join(
                                k for k, v in b["gates"].items() if not v)
                            if b["status"] == "gate_failed"
                            else f"score {b['score']} < winner {winner_ref}")}
                for b in per_branch if b["status"] != "winner"]
    provenance = {"k": len(paths), "depth": cfg["depth_default"], "prefix_hash": phash,
                  "branch_model": model, "judge_model": judge_model,
                  "shadow_only": bool(cfg.get("shadow_only", True)),
                  "clusters": clusters,
                  "score_weights": cfg.get("score_weights") or {},
                  "tokens_total": tokens_total,
                  "wasted_tokens": sum(b["tokens"] for b in per_branch if b["status"] != "winner"),
                  "journal_ref": f"journal://forks/{join_id}"}
    packet = build_packet(join_id, _resolve_base_version(args, session_id), strategy,
                          winner, per_branch, intents, rejected, open_issues, provenance)
    packet = sanitize(packet)
    packet, truncated = enforce_size(packet, int(cfg["join_max_tokens"]))
    if truncated:
        packet["_note"] = "JoinPacket was size-trimmed"
        # the cap is the contract, the note is decoration: if the note itself
        # crosses the line it was just trimmed to, the note goes away
        from agent.model_metadata import estimate_tokens_rough
        if estimate_tokens_rough(json.dumps(packet, ensure_ascii=False)) \
                > int(cfg["join_max_tokens"]):
            packet.pop("_note")

    write_run({"join_id": join_id, "session_id": session_id, "strategy": strategy,
               # shadow_only (решение 17.10): журнал ведётся, боевого fan-out'а
               # нет — run помечается shadow, калибровка SPEC 7.4 идёт по этим
               # строкам журнала.
               "status": "shadow" if bool(cfg.get("shadow_only", True)) else "joined",
               "winner": winner, "prefix_hash": phash,
               "params": {"paths": paths, "cfg": {k: cfg.get(k) for k in
                                                  ("depth_default", "depth_max",
                                                   "tokens_per_step_max", "temperature",
                                                   "fork_deadline_s", "min_margin_delta",
                                                   "cluster_similarity", "score_weights",
                                                   "early_stop_consensus", "shadow_only")}},
               "packet": packet, "tokens_total": tokens_total,
               "wasted_tokens": provenance["wasted_tokens"],
               "latency_ms": int((time.monotonic() - t_start) * 1000),
               "branches": [{"ref": t["ref"], "strategy_text": t["strategy_text"],
                             "steps": t["steps"], "intents": t["intents"],
                             "gates": t["gates"], "score": t.get("score", 0),
                             "score_components": {
                                 "judge_win_rate": t.get("judge_win_rate"),
                                 "consensus": t.get("consensus"),
                                 "cluster_size": t.get("cluster_size"),
                                 "failed_gates": t.get("failed_gates", [])},
                             "tokens": t["tokens"], "latency_ms": t["latency_ms"],
                             "stop_reason": t["stop_reason"], "reads": t["reads"]}
                            for t in traces]})
    logger.info("fork_explore %s: strategy=%s winner=%s tokens=%d", join_id, strategy,
                winner_ref, tokens_total)
    return json.dumps({"ok": True, "join_id": join_id,
                       "journal_ref": f"journal://forks/{join_id}", "packet": packet},
                      ensure_ascii=False)


FORK_EXPLORE_SCHEMA: dict[str, Any] = {
    "name": "fork_explore",
    "description": (
        "Dry-rollout развилки направлений: прогоняет по каждому пути траекторию (куда пойдёт "
        "работа, 1..N модельных шагов, read-only по построению — записи только как intents), "
        "судит слепым pairwise-сравнением, может родить путь В из слабостей A/Б. Возвращает "
        "один JoinPacket (≤1500 токенов) с выбранным направлением, находками и open_issues; "
        "полные трассы — в fork journal. Используй на развилке, где цена неверного "
        "направления выше стоимости форка; единица сравнения — траектория, не ответ."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Развилка: что за направление выбираем."},
            "fork_point": {"type": "string", "description": "Где именно расходятся пути (опционально)."},
            "shared_context": {"type": "string",
                               "description": "Общий большой контекст для всех веток (передаётся побайтово одинаковым стабильным префиксом)."},
            "paths": {"type": "array", "minItems": 2,
                      "items": {"type": "object", "properties": {
                          "ref": {"type": "string"}, "strategy": {"type": "string"}},
                          "required": ["strategy"]},
                      "description": "Пути А, Б, ... с формулировкой стратегии каждого."},
            "acceptance_direction": {"type": "array", "items": {"type": "string"},
                                     "description": "Критерии приёмки НАПРАВЛЕНИЯ (не ответа)."},
            "confirmed_facts": {"type": "array", "items": {"type": "string"},
                                "description": "Подтверждённые факты: противоречие им — гейт."},
            "read_roots": {"type": "array", "items": {"type": "string"},
                           "description": "Каталоги, которые веткам разрешено читать (read-only)."},
            "base_version": {"type": "integer",
                             "description": "Версия контекста, на которой строился форк (для optimistic join)."},
            "depth": {"type": "integer",
                      "description": "Потолок глубины роллаута на ветку (зажимается к fork_explore.depth_max; "
                                     "ветка может остановиться раньше). Не задан — depth_default."},
        },
        "required": ["task", "shared_context", "paths"],
    },
}


from tools.registry import registry  # (registry-first import chain, session_fork pattern)


def _fork_enabled() -> bool:
    from tools.fork_explore_config import load_fork_config
    # load_fork_config already fails closed to built-in defaults on an unreadable
    # config.yaml; a raise here means genuinely broken plumbing — let it surface.
    return bool(load_fork_config().get("enabled", True))


registry.register(
    name="fork_explore",
    toolset="sessions",  # rides with session_fork/session_join (fork/join family)
    schema=FORK_EXPLORE_SCHEMA,
    handler=lambda args, **kw: fork_explore(
        args, session_id=kw.get("session_id") or ""),
    check_fn=_fork_enabled,
    emoji="🌿")
