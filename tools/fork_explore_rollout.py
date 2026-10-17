"""Stepwise-pause rollout for fork_explore (директива Архитектора 17.10, контракт v4).

Фиксированная глубина — потолок, не семантика. Ветка шагает: выполнила шаг →
СТОИТ на границе → меж-шаговый гейт решает «продолжай следующий шаг» или
«прервись». Бесперспективные ветки отсекаются ДО траты следующего
prefill+decode; перспективные углубляются избирательно (после depth_default —
только пока траектория приносит новое). Пауза внутренняя: на «прервись»
следующий chat-запрос просто не отправляется, состояние ветки (trace, бюджет,
step_index) — checkpoint в fork journal.

Единый примитив продолжения — per-branch continue-flag (``cont``):
шаг на границе спрашивает его вместоimplicit-«да». Внешний преэмпт
(task-b78849c10d) позже ложится СВЕРХУ как control-socket verb «не планировать
следующий шаг», который лишь выставляет этот же флаг в False — отдельного
механизма не нужно (дизайн-предложение: docs/design/fork-explore.md).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Optional

from tools.fork_explore_prefix import build_branch_suffix, step_instruction
from tools.fork_explore_scoring import _INTENT_TOOL_RE, fact_conflicts

logger = logging.getLogger(__name__)

_WORDS = re.compile(r"[a-zа-ё0-9_]+", re.IGNORECASE)
_NO_PROGRESS_JACCARD = 0.85  # шаг почти повторяет прошлое — траектория встала
_DEEPEN_NOVELTY = 0.5  # за пределами depth_default углубляемся только за новизной


class ContinueFlag:
    """Единый примитив «продолжай / прервись» для одной ветки (contract v4).

    Граница шага спрашивает флаг вместо implicit-«да»: pause — это просто
    «next request не отправляется». Внутренний гейт и внешний преэмпт
    (control-socket verb «не планировать следующий шаг», task-b78849c10d)
    влияют на МИР через один и тот же объект — второго примитива нет.
    """

    def __init__(self) -> None:
        self._continue = threading.Event()
        self._continue.set()

    def get(self) -> bool:
        return self._continue.is_set()

    def stop(self) -> None:
        self._continue.clear()

    def resume(self) -> None:
        self._continue.set()


def _step_tokens(step: dict) -> set[str]:
    return {t for t in _WORDS.findall(str(step.get("content") or "").lower())
            if len(t) > 2}


def step_novelty(step: dict, prev_steps: list[dict]) -> float:
    """Доля нового в content шага против union прошлых (0..1); первый шаг — 1.0."""
    cur = _step_tokens(step)
    if not prev_steps:
        return 1.0
    old: set[str] = set()
    for s in prev_steps:
        old |= _step_tokens(s)
    union = cur | old
    return 1.0 - (len(cur & old) / len(union)) if union else 1.0


def inter_step_gate(partial: dict, cfg: dict, facts: Optional[list[str]],
                    remaining_s: float, step_index: int, depth_cap: int,
                    tokens_cap: int, planned_depth: int) -> tuple[bool, str]:
    """Граница шага: продолжать ли следующий prefill+decode.

    (True, "continue") / (False, stop_reason). Детерминирована и дешёва:
    только то, что уже видно в траектории, плюс потолок и бюджет.
    depth_default — план; сверх него избирательное углубление: только пока
    последний шаг приносит новое (иначе ветка доживает потолок впустую).
    """
    steps = partial.get("steps") or []
    if steps and steps[-1].get("kind") in (None, "", "error"):
        return False, "bad_json"
    if any(bool(s.get("infeasible")) for s in steps):
        return False, "infeasible"
    forbidden = set(cfg.get("forbidden_tools") or [])
    for s in steps:
        for i in (s.get("intents") or []):
            tool = str(i.get("tool", "")) if isinstance(i, dict) else ""
            if not _INTENT_TOOL_RE.match(tool) or tool in forbidden:
                return False, "forbidden_intent"
    if fact_conflicts(partial, facts):
        return False, "fact_conflict"
    novelty = step_novelty(steps[-1], steps[:-1]) if len(steps) >= 2 else 1.0
    if len(steps) >= 2 and novelty < 1.0 - _NO_PROGRESS_JACCARD:
        return False, "no_progress"
    if step_index >= planned_depth and novelty < _DEEPEN_NOVELTY:
        return False, "selective_halt"
    if step_index >= depth_cap:             # потолок исчерпан — это главный факт границы
        return False, "depth_cap"
    if remaining_s <= 5.0:                 # следующий шаг гарантированно не влезет
        return False, "deadline"
    if int(partial.get("tokens", 0)) > tokens_cap:
        return False, "token_cap"
    return True, "continue"


def redrive_from_checkpoint(client: Any, model: str, cfg: dict, deadline: float,
                            cp: dict[str, Any], *, chat: Optional[Callable] = None,
                            facts: Optional[list[str]] = None) -> dict[str, Any]:
    """One lazy re-drive leg (STITCH-DESIGN-8012 §3.4): replay from the last
    boundary checkpoint of a resumed_pending row up to depth_max under the
    same per-branch caps. The journal row is flipped to 'running' by the
    CALLER (CAS from resumed_pending is the driver's ownership claim)."""
    roller = BranchRoller(client, model, cfg, deadline, facts=facts, chat=chat)
    return roller.run([], str(cp["branch_ref"]), "", resume_cp=cp)


class BranchRoller:
    """Одна ветка: шаг → граница (пауза) → gate/cont → шаг… до потолка depth_max.

    Пауза внутренняя: на «прервись» следующий chat-вызов не отправляется.
    Каждая граница вызывает checkpoint (trace/бюджет/step_index) — журнал
    ведётся и для веток, умирающих до join. chat late-bound в __call__-аргументах,
    чтобы monkeypatch seam ``fork_explore_tool._chat`` работал и здесь.
    """

    def __init__(self, client: Any, model: str, cfg: dict, deadline: float, *,
                 facts: Optional[list[str]] = None,
                 cont: Optional[Callable[[], bool]] = None,
                 checkpoint: Optional[Callable[[dict], None]] = None,
                 chat: Optional[Callable] = None):
        self.client = client
        self.model = model
        self.cfg = cfg
        self.deadline = deadline
        self.facts = facts or []
        self.cont = cont
        self.checkpoint = checkpoint
        self._chat = chat  # None → resolved lazily through the tool seam

    def chat(self, messages, **kw):
        if self._chat is None:
            from tools.fork_explore_tool import _chat
            return _chat(self.client, self.model, messages, **kw)
        return self._chat(self.client, self.model, messages, **kw)

    def run(self, prefix: list[dict], ref: str, strategy: str,
            resume_cp: Optional[dict] = None) -> dict[str, Any]:
        cfg = self.cfg
        state: dict[str, Any] = {"ref": ref, "strategy_text": strategy, "steps": [],
                                 "intents": [], "reads": [], "tokens": 0,
                                 "stop_reason": "depth_cap"}
        depth_cap = int(cfg["depth_max"])
        planned_depth = min(int(cfg["depth_default"]), depth_cap)
        tokens_cap = depth_cap * int(cfg["tokens_per_step_max"])
        messages = list(prefix) + [build_branch_suffix(ref, strategy)]
        t0 = time.monotonic()
        step_no = 0
        if resume_cp is not None:
            # Lazy re-drive (STITCH-DESIGN-8012 §3.4): continue the trajectory
            # from its last boundary checkpoint. step_index+1 semantics come
            # from keeping step_no at cp.step_index — the while-loop's first
            # increment produces exactly the next step. Read-request FILE
            # BODIES are not persisted (only the trace), so a resumed leg
            # replays decisions/actions but re-issues reads as fresh steps.
            steps_seed = json.loads(resume_cp.get("steps_json") or "[]")
            state["steps"] = [dict(s) for s in steps_seed]
            state["intents"] = [it for s in state["steps"]
                                for it in (s.get("intents") or []) if isinstance(it, dict)]
            state["tokens"] = int(resume_cp.get("tokens", 0))
            step_no = int(resume_cp.get("step_index", 0))
            for s in state["steps"]:
                messages.append({"role": "assistant",
                                 "content": json.dumps(s, ensure_ascii=False)})
            messages.append({"role": "user",
                             "content": step_instruction(step_no + 1, depth_cap)})
        self._checkpoint(state, step_no)
        while step_no < depth_cap:
            if self.deadline - time.monotonic() <= 0:
                state["stop_reason"] = "deadline"
                break
            step_no += 1
            step, raw = self._one_step(messages, state, step_no)
            if step is None:
                state["steps"].append({"kind": "error", "content": (raw or "")[:300]})
                state["stop_reason"] = "bad_json"
                self._checkpoint(state, step_no)
                break
            messages.append({"role": "assistant",
                             "content": json.dumps(step, ensure_ascii=False)})
            state["steps"].append(step)
            for it in step.get("intents") or []:
                if isinstance(it, dict):
                    state["intents"].append(it)
            if bool(step.get("infeasible")):
                state["stop_reason"] = "infeasible"
                break
            if str(step.get("kind", "")) == "stop":
                state["stop_reason"] = "complete"
                break
            self._feed_step(messages, step, state, step_no, depth_cap)
            # ── граница шага: ветка СТОИТ, следующий запрос ещё не отправлен ──
            self._checkpoint(state, step_no)
            verdict, reason = inter_step_gate(
                state, cfg, self.facts, self.deadline - time.monotonic(),
                step_no, depth_cap, tokens_cap, planned_depth)
            if not verdict:
                state["stop_reason"] = reason
                break
            if self.cont is not None and not self.cont():
                state["stop_reason"] = "paused_by_request"
                break

        state["latency_ms"] = int((time.monotonic() - t0) * 1000)
        return state

    # -- one model step + the single sanctioned bad-JSON retry (SPEC 5.3) ----
    def _one_step(self, messages: list, state: dict, step_no: int):
        kw = {"max_tokens": int(self.cfg["tokens_per_step_max"]),
              "temperature": float(self.cfg["temperature"]),
              "timeout": min(60.0, max(1.0, self.deadline - time.monotonic())),
              "seed": 17,
              "no_think": bool(self.cfg.get("disable_thinking", True))}
        from tools.fork_explore_tool import _parse_json
        content, used = self.chat(messages, **kw)
        state["tokens"] += used
        step = _parse_json(content)
        if step is None and self.cfg.get("bad_json_retry", True) \
                and self.deadline - time.monotonic() > 10:
            # colder, seed varied, same messages — escapes a truncation/verbosity
            # pocket once; a second failure is final (SPEC 5.3: bad schema must
            # not eat the branch's whole budget)
            content, used = self.chat(
                messages, **dict(kw, temperature=0.1, seed=1017,
                                 timeout=min(60.0, max(1.0, self.deadline - time.monotonic()))))
            state["tokens"] += used
            step = _parse_json(content)
        if step is None:
            return None, content
        return step, ""

    # -- read_request feed or plain next-step instruction (byte-stable) ------
    def _feed_step(self, messages: list, step: dict, state: dict,
                   step_no: int, depth_cap: int) -> None:
        if str(step.get("kind", "")) != "read_request":
            messages.append(step_instruction(step_no + 1, depth_cap))
            return
        from tools.fork_explore_tool import _MAX_READS_PER_BRANCH, _safe_read
        chunks = []
        for p in (step.get("paths") or [])[:_MAX_READS_PER_BRANCH - len(state["reads"])]:
            text = _safe_read(str(p), self.cfg.get("read_roots") or [])
            if text is not None:
                state["reads"].append(str(p))
                chunks.append(f'<file path="{p}">\n{text}\n</file>')
        messages.append({"role": "user",
                         "content": "Данные файлов (только для чтения):\n"
                                    + "\n".join(chunks) if chunks else
                                    "Чтение недоступно (вне read_roots или пусто)."})

    def _checkpoint(self, state: dict, step_index: int) -> None:
        """trace/бюджет/step_index на границу; journal-failure не убивает rollout."""
        if self.checkpoint is None:
            return
        try:
            self.checkpoint({
                "branch_ref": state["ref"], "step_index": step_index,
                "tokens": state["tokens"], "stop_reason": state["stop_reason"],
                "cont_state": (None if self.cont is None else bool(self.cont())),
                "budget_remaining_tokens": max(
                    0, int(self.cfg["depth_max"]) * int(self.cfg["tokens_per_step_max"])
                    - state["tokens"]),
                "steps": [dict(s) for s in state["steps"]],
            })
        except (sqlite3.Error, OSError, ValueError) as exc:
            # checkpoint is diagnostics, never a fork failure: a lost boundary
            # row must not kill an in-flight rollout
            logger.debug("fork checkpoint lost for %s@%d: %s",
                         state["ref"], step_index, exc, exc_info=True)
