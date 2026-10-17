#!/usr/bin/env python3
"""k / deadline calibration for fork_explore on the fleet SGLang (SPEC 7.4).

Answers, with real numbers from :8012 (glm-5.3-flash-libert):

  1. how long ONE branch step (byte-stable big shared prefix + branch suffix,
     json_object, enable_thinking=false) takes at fan-out width k — p50/p95;
  2. how long one blind judge match takes (the swap-pairwise tournament runs
     matches sequentially, so they add to the wall-clock linearly);
  3. the strict-JSON schema_ok RATE of the branch prompt on the live model —
     the open verify-step item ("калибровка branch-промпта под строгий JSON").

fork_deadline_s must cover: depth_default sequential steps at width k
(server-side queueing included in p95) + judge matches + synth. The script
prints the recommendation; a human/agent decides and edits the defaults.

Usage: .venv/bin/python scripts/fork_k_deadline_calibration.py \
           [--base-url http://192.168.9.100:8012] [--out FILE]
Exit 0 always (calibration data, not a gate); non-zero only if the server
is unreachable at baseline.
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
import urllib.request

DEFAULT_BASE = "http://192.168.9.100:8012"

# Real prompt objects from the shipped code — calibration is only valid if it
# measures what production sends.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.fork_explore_prefix import (BRANCH_SYSTEM_PROMPT, build_branch_suffix,
                                       build_shared_prefix)  # after sys.path bootstrap

# A deliberately LARGE shared context (the "большой общий контекст" of the
# task): several thousand tokens of dense prose, identical across all k.
_SHARED = (
    "PR-1 реализован: read-only dry-роллауты, жёсткие гейты, слепой swap-pairwise "
    "судья, JoinPacket <=1500 токенов, журнал fork_rate/win_over_default/wasted_tokens "
    "в sqlite, unit-тесты зелёные. SGLang-хост :8012 обслуживает судью и ветки; "
    "RadixAttention греется общим побайтовым префиксом, поэтому калибровка шлёт "
    "именно его. Кандидат направления А: пошаговая пауза роллаута (ветка шагает, "
    "стоит на границе, ждёт «продолжай» или «прервись»; checkpoint в журнал). "
    "Кандидат направления Б: кэш-форк KV-пула через s.fork(n) вместо k полных "
    "префиллов. Ограничения: меж-шаговый гейт отсекает бесперспективные ветки до "
    "следующего prefill+decode; перспективные углубляются избирательно; внешний "
    "преэмпт сверху — отдельный дизайн-вопрос (control-socket verb поверх "
    "per-branch continue-flag). Отмена ветки = обрыв клиента; load-тест слотов "
    "пройден. Судья не должен быть моделью веток (self-preference). JoinPacket "
    "жёстко ограничен 1500 токенами и проходит sanitize. Журнал profile-aware, "
    "ретенция 200 прогонов. Бюджет сессии на 24 часа ограничен; вилка, "
    "пересекающая бюджет, отвергается до первого чата. "
) * 8  # ~4k tokens of shared prefix


def _post(base: str, payload: dict, timeout: float = 240.0) -> dict:
    req = urllib.request.Request(
        f"{base}/v1/chat/completions", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _model(base: str) -> str:
    with urllib.request.urlopen(f"{base}/v1/models", timeout=5) as resp:
        return json.loads(resp.read().decode())["data"][0]["id"]


def _step_ok(content: str) -> bool:
    """Hard-gate schema check, same shape as tools.fork_explore_tool._parse_json
    + hard_gates schema_ok: one JSON object with kind+content."""
    try:
        val = json.loads(content)
    except (ValueError, TypeError):
        return False
    return isinstance(val, dict) and bool(val.get("kind")) and ("content" in val)


def _one_call(base: str, model: str, messages: list, max_tokens: int,
              temperature: float) -> dict:
    t0 = time.monotonic()
    resp = _post(base, {
        "model": model, "messages": messages, "max_tokens": max_tokens,
        "temperature": temperature, "seed": 17,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"enable_thinking": False}})
    ms = int((time.monotonic() - t0) * 1000)
    ch = resp["choices"][0]
    usage = resp.get("usage") or {}
    return {"ms": ms, "tokens": int(usage.get("completion_tokens", 0) or 0),
            "finish": ch.get("finish_reason"), "ok": _step_ok(ch["message"]["content"] or "")}


def measure_width(base: str, model: str, prefix: list, k: int, temp: float,
                  max_tokens: int) -> dict:
    """k concurrent branch-step calls sharing the byte-identical prefix."""
    results: list = []
    lock = threading.Lock()

    def worker(i: int):
        msgs = list(prefix) + [build_branch_suffix(f"cal{i}", f"стратегия калибровки {i}")]
        try:
            r = _one_call(base, model, msgs, max_tokens, temp)
        except Exception as exc:  # health: allow BLE001 -- a failed probe is data, not a crash
            r = {"ms": -1, "tokens": 0, "finish": f"error:{type(exc).__name__}", "ok": False}
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(k)]
    wall0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)
    wall_ms = int((time.monotonic() - wall0) * 1000)
    ms = sorted(r["ms"] for r in results)
    return {
        "k": k, "wall_ms": wall_ms,
        "p50_ms": ms[len(ms) // 2], "p95_ms": ms[min(len(ms) - 1, int(len(ms) * 0.95))],
        "max_ms": ms[-1],
        "tokens_mean": round(statistics.mean([r["tokens"] for r in results]), 1),
        "schema_ok": f"{sum(1 for r in results if r['ok'])}/{len(results)}",
        "finishes": sorted({str(r["finish"]) for r in results}),
    }


def measure_judge(base: str, model: str, mt: int, reps: int) -> dict:
    system = ("Ты — слепой судья dry-роллаутов. Верни ТОЛЬКО JSON: "
              '{"preferred":"X|Y|tie","decisive_reason":"...","fatal_flaws":{"X":[],"Y":[]}}')
    body_a = "ТРАЕКТОРИЯ:\n" + ("шаг: действие по спецификации; шаг: решение; шаг: stop. " * 40)
    out = []
    for _ in range(reps):
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": f'<candidate id="X">{body_a}</candidate>\n'
                                            f'<candidate id="Y">{body_a} (вариант)</candidate>\n'
                                            "Сравни вслепую, preferred."}]
        out.append(_one_call(base, model, msgs, mt, 0.0))
    ms = sorted(o["ms"] for o in out)
    return {"reps": reps, "p50_ms": ms[len(ms) // 2], "p95_ms": ms[-1],
            "verdict_ok": f"{sum(1 for o in out if o['ok'] or o['finish'] == 'stop')}/{reps}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--widths", default="1,2,4,8")
    ap.add_argument("--temp", type=float, default=0.7)
    ap.add_argument("--step-tokens", type=int, default=1000)
    ap.add_argument("--judge-mt", type=int, default=4000)
    ap.add_argument("--judge-reps", type=int, default=3)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    model = _model(base)
    prefix = build_shared_prefix(
        task="Что делать следующим: PR-2 (пошаговая пауза) или сшивка с преэмтом?",
        fork_point="Сшивка rollout-веток с внешним преэмтом",
        shared_context=_SHARED,
        acceptance=["обратимо/staged", "не рвёт prompt-cache", "одна ветка = один PR"],
        facts=[":8012 жив: SGLang glm-5.3-flash-libert", "журнал уже пишет fork_rate/wasted_tokens"])
    widths = [int(w) for w in args.widths.split(",")]

    rows = [measure_width(base, model, prefix, k, args.temp, args.step_tokens)
            for k in widths]
    judge = measure_judge(base, model, args.judge_mt, args.judge_reps)

    print(f"model={model} step_max_tokens={args.step_tokens} temp={args.temp}")
    print(f"{'k':>3} {'p50_ms':>8} {'p95_ms':>8} {'wall_ms':>8} {'tok_mean':>8} "
          f"{'schema_ok':>9}  finishes")
    for r in rows:
        print(f"{r['k']:>3} {r['p50_ms']:>8} {r['p95_ms']:>8} {r['wall_ms']:>8} "
              f"{r['tokens_mean']:>8} {r['schema_ok']:>9}  {r['finishes']}")
    print(f"judge: p50={judge['p50_ms']}ms p95={judge['p95_ms']}ms reps={judge['reps']}")

    # Wall-clock model (SPEC 5.4 sequential depth + sequential tournament):
    worst = max(rows, key=lambda r: r["p95_ms"])
    step95 = worst["p95_ms"] / 1000
    depth = 2  # depth_default
    matches = 4  # up to 4 paths -> clusters reps, swap doubles each match, ~4 typical
    rec_deadline = depth * step95 + matches * judge["p95_ms"] / 1000 + 15
    print(f"\nRECOMMENDED fork_deadline_s >= {int(rec_deadline)} "
          f"(depth*step_p95@k={worst['k']} + {matches}*judge_p95 + 15s margin)")
    print(f"RECOMMENDED k_max ceiling check: at k={max(widths)} a step still "
          f"finishes in {worst['max_ms']}ms max.")

    if args.out:
        Path(args.out).write_text(json.dumps(
            {"model": model, "widths": rows, "judge": judge,
             "recommended_fork_deadline_s": int(rec_deadline)},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
