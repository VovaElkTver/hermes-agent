"""Pure rule engine for the supervisor plugin (no Hermes imports -> unit-testable).

A trigger is a dict from ``plugins.entries.supervisor.settings.triggers`` in config.yaml:

    - name: test-failure
      when: post_tool_call          # pre_tool_call | post_tool_call ("on" is a YAML boolean!)
      tool: "terminal"              # regex over tool_name (full match); omit = any tool
      args_regex: {command: "pytest"}   # every listed arg must match (re.search)
      result_regex: "FAILED|Traceback"  # post only; re.search over the result text
      status: error                 # post only; match on hook status (ok/error/blocked)
      tool_count_gte: 40            # child's tool calls so far (inclusive)
      consecutive_errors_gte: 3     # post only
      action: steer                 # block | annotate | steer | interrupt | escalate
      message: "Tests failed. Read the first failure, fix only that, rerun."
      max_fires: 2                  # per child; default unlimited
      roles: [leaf]                 # optional filter on child_role

Actions allowed per event:
    pre_tool_call : block, steer, interrupt, escalate
    post_tool_call: annotate, steer, interrupt, escalate
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

PRE_ACTIONS = {"block", "steer", "interrupt", "escalate"}
POST_ACTIONS = {"annotate", "steer", "interrupt", "escalate"}
VALID_EVENTS = ("pre_tool_call", "post_tool_call")

# A verification command "succeeded" for gate purposes = status ok AND the result text does not
# look like a failing run. The text patterns mirror the built-in test-failure trigger regexes;
# the exit_code pattern covers dict-shaped terminal results before they reach context.
DEFAULT_VERIFY_FAILURE_REGEX = (r"(?i)(\d+ (failed|errors)|\bFAILED\b|Traceback \(most recent call"
                                r'|\"exit_code\":\s*[1-9])')


@dataclass
class ChildState:
    """Everything the supervisor tracks about one live child."""

    subagent_id: str
    session_id: str
    parent_session_id: str = ""
    parent_subagent_id: str = ""
    role: str = "leaf"
    goal: str = ""
    tool_count: int = 0
    consecutive_errors: int = 0
    unverified_writes: list[str] = field(default_factory=list)
    fires: dict[str, int] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)  # bounded tool log for the judge
    pending_review: Any = None  # concurrent.futures.Future for the LLM judge
    halted: bool = False


@dataclass
class Decision:
    trigger: str
    action: str
    message: str


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _fullmatch(pattern: Optional[str], value: str) -> bool:
    return pattern is None or re.fullmatch(pattern, value or "") is not None


def _args_match(spec: Optional[dict], args: Any) -> bool:
    if not spec:
        return True
    if not isinstance(args, dict):
        return False
    for key, pattern in spec.items():
        if re.search(pattern, _text(args.get(key))) is None:
            return False
    return True


def _compile_ok(rx: Any) -> bool:
    if rx is None:
        return True
    try:
        re.compile(_text(rx))
        return True
    except (re.error, TypeError):
        return False


def validate_triggers(triggers: Any) -> list[str]:
    """Return human-readable config errors for a triggers list (empty list = good).

    Called at register() so a malformed triggers block is reported once at startup instead of
    crashing evaluate() inside every pre_tool_call — pre_tool_call fails CLOSED, so an
    unvalidated bad list would block every single child tool call (total paralysis).
    """
    errors: list[str] = []
    if triggers is None:
        return errors
    if not isinstance(triggers, list):
        return [f"triggers: must be a list, got {type(triggers).__name__}"]
    for i, trig in enumerate(triggers):
        where = f"triggers[{i}]"
        if not isinstance(trig, dict):
            errors.append(f"{where}: must be a mapping, got {type(trig).__name__}")
            continue
        where = f"{where} ({trig.get('name', '?')})"
        when = trig.get("when", "post_tool_call")
        if when not in VALID_EVENTS:
            errors.append(f"{where}: when={when!r} invalid (use one of {VALID_EVENTS}; note: 'on:' is a YAML boolean)")
            continue
        action = trig.get("action", "annotate")
        allowed = PRE_ACTIONS if when == "pre_tool_call" else POST_ACTIONS
        if action not in allowed:
            errors.append(f"{where}: action {action!r} not allowed for {when} (allowed: {sorted(allowed)})")
        for key in ("tool", "result_regex"):
            if not _compile_ok(trig.get(key)):
                errors.append(f"{where}: {key} is not a valid regex: {trig.get(key)!r}")
        spec = trig.get("args_regex")
        if spec is not None:
            if not isinstance(spec, dict):
                errors.append(f"{where}: args_regex must be a mapping of arg-name -> regex")
            else:
                for k, rx in spec.items():
                    if not _compile_ok(rx):
                        errors.append(f"{where}: args_regex[{k!r}] is not a valid regex: {rx!r}")
        for key in ("tool_count_gte", "consecutive_errors_gte", "max_fires"):
            if key in trig:
                try:
                    int(trig[key])
                except (TypeError, ValueError):
                    errors.append(f"{where}: {key}={trig[key]!r} is not an integer")
    return errors


def evaluate(
    triggers: Iterable[dict],
    event: str,
    state: ChildState,
    tool_name: str,
    args: Any,
    result: Any = None,
    status: str = "",
) -> list[Decision]:
    """Return every trigger decision that fires for this tool boundary, in config order.

    A single trigger raising (bad regex, wrong types) is logged and skipped — it must not abort
    the whole evaluation nor, via pre_tool_call's fail-closed policy, block every child call.
    """
    allowed = PRE_ACTIONS if event == "pre_tool_call" else POST_ACTIONS
    out: list[Decision] = []
    for trig in triggers or []:
        try:
            if not isinstance(trig, dict):
                continue
            if trig.get("when", "post_tool_call") != event:
                continue
            action = trig.get("action", "annotate")
            if action not in allowed:
                continue
            name = trig.get("name") or f"{event}:{action}:{trig.get('tool', '*')}"
            roles = trig.get("roles")
            if roles and state.role not in roles:
                continue
            max_fires = trig.get("max_fires")
            if max_fires is not None and state.fires.get(name, 0) >= int(max_fires):
                continue
            if not _fullmatch(trig.get("tool"), tool_name):
                continue
            if not _args_match(trig.get("args_regex"), args):
                continue
            if "tool_count_gte" in trig and state.tool_count < int(trig["tool_count_gte"]):
                continue
            if event == "post_tool_call":
                if trig.get("status") and trig["status"] != status:
                    continue
                rx = trig.get("result_regex")
                if rx and re.search(rx, _text(result)) is None:
                    continue
                if "consecutive_errors_gte" in trig and state.consecutive_errors < int(trig["consecutive_errors_gte"]):
                    continue
            state.fires[name] = state.fires.get(name, 0) + 1
            msg = trig.get("message") or f"Supervisor trigger '{name}' fired on {tool_name}."
            out.append(Decision(trigger=name, action=action, message=msg))
        except Exception as exc:
            logger.warning("supervisor: skipping malformed trigger %r: %s",
                           (trig.get("name") if isinstance(trig, dict) else trig), exc)
    return out


def verify_gate(gate: dict, state: ChildState, tool_name: str, args: Any) -> Optional[Decision]:
    """Verify-after-write: block the next mutation while earlier writes are unverified.

    A mutation (``mutating_tools``) records its path (``record_mutation``). A verifying-tool
    call (``verifying_tools`` + ``verify_command_regex``) clears the backlog only AFTER it has
    actually succeeded — see ``clear_gate_on_verified``, which runs on post_tool_call with the
    real result/status. Blocking decisions are made here on pre_tool_call only.
    """
    if not gate or not gate.get("enabled", True):
        return None
    mutating = gate.get("mutating_tools", ["write_file", "patch"])
    limit = int(gate.get("max_unverified_writes", 1))

    if tool_name in mutating and len(state.unverified_writes) >= limit:
        pending = ", ".join(state.unverified_writes[-5:])
        return Decision(
            trigger="verify_after_write",
            action="block",
            message=(
                "[supervisor] Checkpoint required before the next edit. Unverified changes: "
                f"{pending}. Run a verification command (tests/lint/type-check) that covers "
                "them, read its output, fix anything it reports, then continue editing."
            ),
        )
    return None


def _is_verification(gate: dict, tool_name: str, args: Any) -> bool:
    verifying = gate.get("verifying_tools", ["terminal"])
    if tool_name not in verifying:
        return False
    verify_rx = gate.get("verify_command_regex", r"(pytest|ruff|mypy|npm (run )?test|cargo test|go test|make (test|check))")
    cmd = _text(args.get("command") if isinstance(args, dict) else args)
    return re.search(verify_rx, cmd) is not None


def clear_gate_on_verified(gate: dict, state: ChildState, tool_name: str, args: Any,
                           result: Any, status: str) -> bool:
    """Post-tool half of the verify-after-write gate. Returns True if the backlog was cleared.

    Clears ONLY on evidence of a passing run: hook status not error/timeout AND the result text
    does not look like a failing test/build run. A failing pytest no longer counts as
    verification; the backlog survives and the worker must fix and rerun.
    """
    if not gate or not gate.get("enabled", True) or not state.unverified_writes:
        return False
    if not _is_verification(gate, tool_name, args):
        return False
    if status in ("error", "timeout", "blocked"):
        return False
    failure_rx = gate.get("verify_failure_regex", DEFAULT_VERIFY_FAILURE_REGEX)
    try:
        if re.search(failure_rx, _text(result)):
            return False
    except re.error:
        logger.warning("supervisor: bad verify_failure_regex, treating run as unverified")
        return False
    state.unverified_writes.clear()
    return True


def record_mutation(gate: dict, state: ChildState, tool_name: str, args: Any, status: str) -> None:
    if not gate or not gate.get("enabled", True) or status not in ("", "ok", "success"):
        return
    if tool_name in gate.get("mutating_tools", ["write_file", "patch"]):
        path = ""
        if isinstance(args, dict):
            path = _text(args.get("path") or args.get("file_path") or "?")
        state.unverified_writes.append(path or "?")
