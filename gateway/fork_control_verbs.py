"""gateway.fork_control_verbs — control-socket mount for the fork_explore continuation gate.

Thin wire wrappers over ``tools/fork_explore_verbs`` (PR-2b): pause/resume/close a branch
or a whole fork through the fork_gate CAS hub. There is NO second semantics here — from
outside the owning process an in-flight HTTP step is reachable only through the step
client's own timeout (§VERDICT-2), so the machine path is exactly the soft DB-gate path.
The wrapper's only job is to expose the existing function-shaped results on the socket in
STITCH-DESIGN-8012 §3.2 style; every error code (``unknown_branch``, ``already_paused``, …)
is produced by the verbs module itself.

Wire (origin is mandatory at the request root for all three — enforced + injected into
``params`` by ``GatewayControlServer``):

    → {"verb": "fork.pause",  "origin": "cli", "params": {"join_id": "…", "ref": "", "variant": "soft"}}
    ← {"ok": true,  "result": {"ok": true, "result": {…marked_paused…, "variant": "soft"}}}
    → {"verb": "fork.resume", "params": {"join_id": "…", "ref": ""}}
    ← …{"queued_for_redrive": N, "disposition": "deferred"}   (lazy re-drive §0.7)
    → {"verb": "fork.close",  "params": {"join_id": "…", "ref": "", "reason": "human"}}
Errors arrive inside ``result`` with their machine codes, exactly as the verbs module words them.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def _origin(params: dict) -> str:
    """Wire-root origin injected by GatewayControlServer; direct callers must pass it."""
    return str(params.get("origin") or "").strip()


def fork_control_verbs() -> dict[str, Callable[..., dict]]:
    """The fork-half of the preempt ``verb_handlers=`` spread (see ``preempt_verb_handlers``)."""
    from tools.fork_explore_verbs import close_branch, pause_branch, resume_branch

    def _pause(params: Optional[dict] = None) -> dict:
        params = params or {}
        result = pause_branch(str(params.get("join_id") or ""),
                              ref=str(params.get("ref") or ""),
                              variant=str(params.get("variant") or "soft"))
        logger.info("FORKVERB fork.pause %s/%s origin=%s -> %s",
                    params.get("join_id"), params.get("ref"), _origin(params),
                    result.get("result", result))
        return result

    def _resume(params: Optional[dict] = None) -> dict:
        params = params or {}
        result = resume_branch(str(params.get("join_id") or ""), ref=str(params.get("ref") or ""))
        logger.info("FORKVERB fork.resume %s/%s origin=%s -> %s",
                    params.get("join_id"), params.get("ref"), _origin(params),
                    result.get("result", result))
        return result

    def _close(params: Optional[dict] = None) -> dict:
        params = params or {}
        result = close_branch(str(params.get("join_id") or ""),
                              ref=str(params.get("ref") or ""),
                              reason=str(params.get("reason") or "human"))
        logger.info("FORKVERB fork.close %s/%s origin=%s -> %s",
                    params.get("join_id"), params.get("ref"), _origin(params),
                    result.get("result", result))
        return result

    return {"fork.pause": _pause,
            "fork.resume": _resume,
            "fork.close": _close}
