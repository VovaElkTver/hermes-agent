"""Optional LLM judge for intermediate checkpoints (OpenAI-compatible /chat/completions).

Runs off the agent thread (ThreadPoolExecutor) so post_tool_call returns immediately and never
trips plugins.hook_callback_timeout. The verdict is consumed at the child's NEXT pre_tool_call
(gate) and/or delivered via steer_subagent().
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any

JUDGE_SYSTEM = """You are a supervisory reviewer for an autonomous coding/research subagent.
You see the subagent's GOAL and its most recent tool calls (name, arguments, truncated result).
Decide whether the work so far is on track. Judge only what the evidence shows.

Return ONLY a JSON object:
{"verdict": "pass" | "revise" | "halt",
 "reason": "<one sentence, cite the specific tool call>",
 "instruction": "<for revise: one concrete, imperative next step; else empty>"}

Rules:
- "pass": progress is consistent with the goal; no unverified risky change; no ignored errors.
- "revise": the agent ignored an error, skipped verification after an edit, drifted from the goal,
  repeated the same failing action, or is editing files outside the stated scope.
- "halt": destructive or irreversible action, secrets exposure, or clear goal abandonment.
- Never invent problems; when uncertain, choose "pass".
"""


def _clip(value: Any, n: int) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[: n // 2] + f"\n...[{len(s) - n} chars cut]...\n" + s[-n // 2 :]


def build_messages(goal: str, history: list[dict[str, Any]], max_chars: int) -> list[dict[str, str]]:
    per_call = max(400, max_chars // max(1, len(history)))
    lines = []
    for i, h in enumerate(history, 1):
        lines.append(
            f"#{i} {h['tool']} status={h.get('status', '')}\n"
            f"args: {_clip(h.get('args'), per_call // 3)}\n"
            f"result: {_clip(h.get('result'), per_call)}"
        )
    user = f"GOAL:\n{goal}\n\nRECENT TOOL CALLS (oldest first):\n" + "\n\n".join(lines)
    return [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}]


def review(cfg: dict, goal: str, history: list[dict[str, Any]]) -> dict[str, str]:
    """Blocking HTTP call; run inside an executor. Fails open to 'pass' on any error."""
    try:
        url = cfg.get("base_url", "http://127.0.0.1:8000/v1").rstrip("/") + "/chat/completions"
        key = os.environ.get(cfg.get("api_key_env", "SUPERVISOR_JUDGE_API_KEY"), "EMPTY")
        body = {
            "model": cfg.get("model", "judge"),
            "messages": build_messages(goal, history, int(cfg.get("max_context_chars", 12000))),
            "temperature": 0,
            "max_tokens": int(cfg.get("max_tokens", 300)),
        }
        if cfg.get("json_mode", True):
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=float(cfg.get("timeout_seconds", 20))) as resp:
            content = json.loads(resp.read())["choices"][0]["message"]["content"] or ""
        m = re.search(r"\{.*\}", content, re.S)
        verdict = json.loads(m.group(0)) if m else {}
        v = str(verdict.get("verdict", "pass")).lower()
        return {
            "verdict": v if v in ("pass", "revise", "halt") else "pass",
            "reason": str(verdict.get("reason", ""))[:500],
            "instruction": str(verdict.get("instruction", ""))[:800],
        }
    except Exception as exc:  # fail open: the judge is advisory, rule triggers are the hard gate
        return {"verdict": "pass", "reason": f"judge unavailable: {type(exc).__name__}", "instruction": ""}
