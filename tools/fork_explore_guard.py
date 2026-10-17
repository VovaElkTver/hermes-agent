"""Join Guard (lite) for fork_explore — PR-1 of the fork-reasoning line.

Join is the riskiest step: a bad merge pollutes every later turn. This module
implements the deterministic half of SPEC.md §8.2: schema validation,
sanitization (candidate text is DATA, never instructions), the ≤ join_max_tokens
budget, conflict-lite, and intents dedup with idempotency keys. Version-check /
re-score policy lands with PR-2/PR-3.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from agent.model_metadata import estimate_tokens_rough

# Chat-template markers any tokenizer may leak into generated text.
_TEMPLATE_TOKEN_RE = re.compile(r"<\|[^|<>]{1,40}\|>|<\|[a-z_]+[a-z_0-9]*\|>")
_IM_TOKEN_RE = re.compile(r"<\|im_(?:start|end)\|>")
# Instructions aimed at the agent, riding inside branch output (SPEC 8.2 step 4).
_INJECTION_RES = [
    re.compile(r"(?:забудь|игнорируй|отмени)[^.!?]{0,80}(?:правил|инструкц|системн\w+ промпт)[^.!?]*[.!?]?",
               re.IGNORECASE),
    re.compile(r"(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?(?:previous\s+)?(?:rules|instructions|system prompt)[^.!?]*[.!?]?",
               re.IGNORECASE),
    re.compile(r"^\s*(?:system|assistant|user)\s*:\s*", re.MULTILINE),
]
# Reasoning fields never ride into the main context (SPEC principle 4).
_DROP_KEYS = frozenset({"reasoning", "thinking", "chain_of_thought", "cot"})

MAX_CLAIM_CHARS = 240
MAX_INTENTS = 12
MAX_ISSUES = 10
MAX_SUMMARY_CHARS = 600


def _clean_str(text: str) -> str:
    out = _TEMPLATE_TOKEN_RE.sub("", text)
    out = _IM_TOKEN_RE.sub("", out)
    for rx in _INJECTION_RES:
        out = rx.sub("", out)
    return re.sub(r"\s+", " ", out).strip()


def sanitize(obj: Any) -> Any:
    """Deep-clean a JoinPacket payload: template tokens and agent-directed
    instructions out of every string; reasoning keys dropped."""
    if isinstance(obj, str):
        return _clean_str(obj)
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items() if k not in _DROP_KEYS}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    return obj


def idempotency_key(session_id: str, join_id: str, intent: dict[str, Any]) -> str:
    canonical = json.dumps({"tool": intent.get("tool", ""),
                            "args": intent.get("args") or {}},
                           sort_keys=True, ensure_ascii=False)
    seed = f"{session_id}|{join_id}|{canonical}"
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def collect_intents(traces: list[dict[str, Any]], winner_ref: str,
                    session_id: str, join_id: str) -> list[dict[str, Any]]:
    """Winner-first intents, deduped by (tool, canonical args); irreversible ones
    carry needs_approval and never auto-execute (main session decides)."""
    seen: dict[str, dict[str, Any]] = {}
    ordered = ([t for t in traces if t.get("ref") == winner_ref]
               + [t for t in traces if t.get("ref") != winner_ref])
    for trace in ordered:
        for it in trace.get("intents") or []:
            if not isinstance(it, dict) or not str(it.get("tool", "")).strip():
                continue
            key = json.dumps({"tool": it.get("tool"), "args": it.get("args") or {}},
                             sort_keys=True, ensure_ascii=False)
            if key in seen:
                continue
            entry = {"tool": str(it["tool"])[:64], "args": it.get("args") or {},
                     "reversible": bool(it.get("reversible", False)),
                     "source_branch": trace.get("ref", ""),
                     "needs_approval": not bool(it.get("reversible", False))}
            entry["idempotency_key"] = idempotency_key(session_id, join_id, entry)
            seen[key] = entry
    return list(seen.values())[:MAX_INTENTS]


def reconcile_claims(claims: list[str], confirmed_facts: list[str]) -> tuple[list[dict], list[str]]:
    """Conflict-lite: a claim sharing a bigram-free token core with a denied
    statement is disputed rather than silently merged. Full reconcile is PR-2."""
    out, disputed = [], list(confirmed_facts)
    for c in claims:
        c = str(c).strip()[:MAX_CLAIM_CHARS]
        if not c:
            continue
        out.append({"text": c, "status": "branch_reported"})
    return out, disputed


def build_packet(join_id: str, base_version: int | None, strategy: str,
                 winner: dict[str, Any], per_branch: list[dict[str, Any]],
                 intents: list[dict[str, Any]], rejected: list[dict[str, Any]],
                 open_issues: list[str], provenance: dict[str, Any]) -> dict[str, Any]:
    return {
        "join_id": join_id,
        "base_version": base_version,
        "strategy": strategy,  # select | select_with_caveat | synthesize | reject
        "winner": winner,      # {path_ref, score, margin} or None on reject
        "result": {
            "summary": str(winner.get("summary", ""))[:MAX_SUMMARY_CHARS] if winner else "",
            "direction": str(winner.get("direction", ""))[:MAX_SUMMARY_CHARS] if winner else "",
            "branches": per_branch,
            "open_issues": [str(i)[:MAX_CLAIM_CHARS] for i in open_issues[:MAX_ISSUES]],
        },
        "intents": intents,
        "rejected_alternatives": rejected,
        "provenance": provenance,
    }


def _shrink(packet: dict[str, Any]) -> bool:
    """One trim pass toward the token budget; returns True if anything changed."""
    result = packet.get("result", {})
    issues = result.get("open_issues", [])
    if len(issues) > 3:
        result["open_issues"] = issues[:2] + [f"(ещё {len(issues) - 2})"]
        return True
    for br in result.get("branches", []):
        for field in ("key_findings", "risks"):
            lst = br.get(field) or []
            if len(lst) > 3:
                br[field] = lst[:2] + [f"(ещё {len(lst) - 2})"]
                return True
    if len(packet.get("rejected_alternatives") or []) > 2:
        packet["rejected_alternatives"] = packet["rejected_alternatives"][:2]
        return True
    for br in result.get("branches", []):
        if len(str(br.get("summary", ""))) > 120:
            br["summary"] = str(br["summary"])[:117] + "..."
            return True
    # Later stages (verify 17.10): the fattest scalars before the hard trim.
    for br in result.get("branches", []):
        for field in ("key_findings", "risks"):
            lst = br.get(field) or []
            if any(len(str(x)) > 80 for x in lst):
                br[field] = [str(x)[:77] + "..." if len(str(x)) > 80 else x for x in lst]
                return True
            if len(lst) > 2:
                br[field] = lst[:2]
                return True
    if len(result.get("branches") or []) > 2:
        result["branches"] = result["branches"][:2]
        return True
    if packet.get("intents") and len(packet["intents"]) > 4:
        packet["intents"] = packet["intents"][:4]
        return True
    winner = packet.get("winner") or {}
    if len(str(winner.get("direction", ""))) > 160:
        winner["direction"] = str(winner["direction"])[:157] + "..."
        return True
    if len(str(result.get("summary", ""))) > 160:
        result["summary"] = str(result["summary"])[:157] + "..."
        return True
    return False


def _minimal_packet(packet: dict[str, Any], keep: int) -> dict[str, Any]:
    """Deterministic last-resort packet: the DECISION survives, the bulk dies.

    The old path string-sliced the serialized blob and re-closed braces with a
    `+ "}}"` hack — json.loads usually failed and the fallback hardcoded
    strategy='reject', silently overturning a live 'select' with its winner
    (found by the :8012 dogfood, 17.10). Never overturn a verdict to fit size:
    winner/strategy/journal_ref stay, long text is what gets cut.
    """
    winner = packet.get("winner")
    prov = packet.get("provenance") or {}
    out: dict[str, Any] = {
        "join_id": packet.get("join_id"),
        "base_version": packet.get("base_version"),
        "strategy": packet.get("strategy"),
        "winner": None if not winner else {
            "path_ref": winner.get("path_ref"),
            "score": winner.get("score"), "margin": winner.get("margin"),
            "summary": str(winner.get("summary", ""))[:200],
            "direction": str(winner.get("direction", ""))[:200]},
        "result": {"summary": str((packet.get("result") or {}).get("summary", ""))[:200],
                   "branches": [
                       {"ref": b.get("ref"), "status": b.get("status"),
                        "score": b.get("score"),
                        "summary": str(b.get("summary", ""))[:80]}
                       for b in (packet.get("result") or {}).get("branches", [])[:2]],
                   "open_issues": [str(i)[:120] for i in
                                   ((packet.get("result") or {}).get("open_issues") or [])[:3]]},
        "provenance": {"journal_ref": prov.get("journal_ref", ""),
                       "k": prov.get("k"), "tokens_total": prov.get("tokens_total"),
                       # calibration inputs must survive the last resort too
                       # (shadow_stats/SPEC 7.4): clusters compacted to sizes.
                       "score_weights": prov.get("score_weights") or {},
                       "clusters": [
                           {"rep": c.get("rep") if isinstance(c, dict) else None,
                            "size": (c.get("size") or len(c.get("members") or []))
                            if isinstance(c, dict) else 0}
                           for c in (prov.get("clusters") or [])[:4]]},
        "_truncated": True,
    }
    blob = json.dumps(out, ensure_ascii=False)
    if len(blob) > keep * 4:  # still too fat: halve the fattest scalar until it fits
        while len(json.dumps(out, ensure_ascii=False)) > keep * 4:
            s = out["result"]["summary"]
            if len(s) <= 40:
                out["winner"] = None if not out["winner"] else {**out["winner"], "direction": ""}
                s = str((out["winner"] or {}).get("summary", ""))
            if not s:
                break
            out["result"]["summary"] = s[: max(20, len(s) // 2)]
    return out


def enforce_size(packet: dict[str, Any], max_tokens: int) -> tuple[dict[str, Any], bool]:
    """Guarantee the JoinPacket fits the budget: shrink by policy, last resort is
    a hard JSON trim so a runaway branch never floods the main context."""
    blob = json.dumps(packet, ensure_ascii=False)
    if estimate_tokens_rough(blob) <= max_tokens:
        return packet, False
    while _shrink(packet):
        blob = json.dumps(packet, ensure_ascii=False)
        if estimate_tokens_rough(blob) <= max_tokens:
            # policy shrink did it — still report the trim: the consumer's
            # _note must say the packet was cut, however it was cut
            return packet, True
    keep = max(16, max_tokens - 32)
    # Last resort keeps the decision (see _minimal_packet); string-slicing the
    # blob was the 17.10 dogfood bug that turned a live 'select' into 'reject'.
    packet = _minimal_packet(packet, keep)
    blob = json.dumps(packet, ensure_ascii=False)
    while estimate_tokens_rough(blob) > max_tokens:
        summary = str(packet.get("result", {}).get("summary", ""))
        if not summary:
            packet = {"join_id": packet.get("join_id"), "strategy": packet.get("strategy"),
                      "result": {"summary": summary[: max(0, len(summary) // 2)],
                                 "open_issues": ["packet hard-truncated"]},
                      "_truncated": True}
        else:
            packet["result"]["summary"] = summary[: max(40, len(summary) // 2)]
        blob = json.dumps(packet, ensure_ascii=False)
    return packet, True

