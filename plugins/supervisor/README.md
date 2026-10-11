# Supervisory Workflow for Hermes Agent — Configuration Guide and Reference Implementation

Target: hermes-agent `main` as of 2026-10-10. Every hook name, RPC method, payload field and config key used here was checked against the source tree and the official docs ([Subagent Delegation](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation), [Event Hooks](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks), [Programmatic Integration](https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration)).

## 1. What you get

| Layer | Where it runs | What it can do at a child's tool boundary | File |
|---|---|---|---|
| Orchestrator prompt | Parent model | Plan, dispatch with a checkpoint contract and `output_schema`, review each completion, steer/stop via `delegate_task(action=...)` | `prompts/orchestrator.md` |
| Supervisor plugin (main) | In-process, every child thread | Block, annotate, steer, interrupt, escalate; verify-after-write gate; optional LLM judge | `plugins/supervisor/` |
| Shell-hook fallback | Subprocess per event | Block / modify only (no steer) | `agent-hooks/child-gate.py` |
| External JSON-RPC supervisor | Separate process driving `tui_gateway` | Watch `subagent.*` events, read live transcript tails, `subagent.steer` / `subagent.interrupt` / `session.steer` | `rpc/supervisor_rpc.py` |
| Config | `~/.hermes/config.yaml` | Delegation topology, plugin enable, triggers, judge | `config/config.supervisor.yaml` |

```
user ─► parent (orchestrator persona) ──delegate_task──► child sa-0-…  ──tool call──► executor
          ▲  review completions,                              │                         │
          │  action=list/steer/stop                           │   pre_tool_call ◄───────┤  block / gate / judge verdict
          │                                                   │   post_tool_call ◄──────┤  counters, triggers, judge submit
          │  [supervisor escalation] (steer to parent         │   transform_tool_result ◄┘  append [supervisor] notes
          │   orchestrator if it is itself a subagent)        │
          └──────────────────────────── supervisor plugin ────┴─► steer_subagent / interrupt_subagent
external process ─stdio JSON-RPC─► tui_gateway ─ events: subagent.start/tool/complete ─► rules ─► subagent.steer / .interrupt
```

## 2. Key facts the design rests on

1. Hooks fire inside children. Children are full `AIAgent` instances whose tool calls go through the same executor and the same plugin dispatcher. `pre_tool_call` / `post_tool_call` get the child's own `session_id`, and `subagent_start` gives the mapping `child_session_id → child_subagent_id` plus `parent_subagent_id`, `child_role`, and `child_goal` ([hooks catalog](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks)). That mapping is how the plugin tells child calls apart from parent calls.
2. There are four delivery channels, each with different timing:

   | Channel | Mechanism | When the child sees it |
   |---|---|---|
   | block | `pre_tool_call` returns `{"action":"block","message":…}` | Immediately, as that call's tool result |
   | annotate | `transform_tool_result` appends text | Immediately, inside the current tool result |
   | steer | `steer_subagent(id, text)` | At the next iteration boundary; the in-flight tool is never cut |
   | interrupt | `interrupt_subagent(id)` | Cooperative stop at the next boundary; the partial result returns to the parent |

   A steer reply of `queued` does not mean it was delivered. If the child finishes first, the parent's completion entry carries `missed_steer` ([delegation docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation)).
3. Orchestrator children wait for their own workers synchronously within the turn, so an orchestrator model can't steer its workers mid-batch. Mid-flight review therefore belongs to the plugin (or the RPC supervisor). The model reviews between tasks. Setting `delegation.independent_completions: true` gives the top-level parent one wake-up per finished task.
4. Steer and interrupt authority:
   - Gateway RPCs (`subagent.steer`, `subagent.interrupt`, `subagent.tail`) only accept the exact live session and transport that spawned the child.
   - In-process callers (the plugin) use the unscoped helper contract ([delegation docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation); `tools/delegate_tool_registry.py`).
5. Plugin settings belong in `config.yaml` under `plugins.entries.<id>.settings` and are read with `ctx.get_config("a.b")` (`hermes_cli/plugins.py`, `plugins_state.py`). Reserved roots are `model`, `plugins`, `security`, and `settings`.
6. Failure semantics:
   - A Python `pre_tool_call` callback that overruns `plugins.hook_callback_timeout` (default 30 s) fails closed and blocks the tool.
   - Other bounded hooks fail open.
   - `subagent_stop` always runs on the caller's thread.
   - Shell hooks fail open unless `fail_closed: true` is set ([hooks docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks)).

## 3. Install

```bash
# 1. plugin
mkdir -p ~/.hermes/plugins && cp -r plugins/supervisor ~/.hermes/plugins/
# 2. config — merge config/config.supervisor.yaml into ~/.hermes/config.yaml (don't overwrite)
# 3. persona — paste prompts/orchestrator.md into personalities.supervisor.system_prompt, then in chat:
#      /personality supervisor
# 4. verify
hermes plugins list            # supervisor: enabled
tail -f ~/.hermes/supervisor/audit.jsonl   # "start" records appear when a child spawns
```

Run the offline tests (no Hermes needed; the delegate registry is stubbed):

```bash
pip install pytest pyyaml && python -m pytest -q tests/     # 9 passed
```

## 4. Orchestrator prompt design

Full text: `prompts/orchestrator.md`. It has three parts:

1. Supervisor system prompt. The parent runs a loop: plan, dispatch, monitor, review, correct or accept.
   - Each phase needs an explicit deliverable, a verification command, and a path scope.
   - Review uses a rubric table: `schema_valid`, evidence per checkpoint, scope, unresolved `[supervisor]` events, terminal status, and worktree inspection.
   - Fix rounds are capped at 2 per phase so the parent can't loop forever.
2. Worker checkpoint contract. This goes into every task's `context`, because children inherit none of the parent's conversation. It tells the worker:
   - Work in act → verify → read-output steps.
   - Expect `[supervisor]` messages through all three channels (blocked result, appended note, out-of-band steer).
   - Never retry a blocked call unchanged.
   - Report every supervisor event in its final answer.
3. Checkpoint report schema. This is passed as `output_schema`. The parent validates it with one correction retry, and results gain `schema_valid` / `schema_errors`. Every checkpoint carries `verified` plus `evidence` (the command and its key output line), which makes review mechanical.

Design rules behind the prompt:

- Prefix every intervention with `[supervisor]`. The contract can then refer to one recognizable token, and the parent can grep summaries for unresolved interventions.
- Use block for must-not-happen events and steer for course corrections. A block message is the strongest correction because it replaces the tool result and arrives that same iteration.
- Write steer text as directives (what to stop, what to do, what evidence to produce). Never use steer to widen scope; dispatch a new task instead.
- Don't trust worker claims on their own: the parent re-runs at least one verification itself.

## 5. Step-by-step verification with the plugin

All settings live in `config.yaml` under `plugins.entries.supervisor.settings`.

### 5.1 Verify-after-write gate (deterministic)

```yaml
gate:
  mutating_tools: [write_file, patch]
  verifying_tools: [terminal]
  verify_command_regex: "(pytest|ruff|mypy|npm (run )?test|cargo test|go test|make (test|check))"
  max_unverified_writes: 2
```

- Each successful write is recorded as unverified.
- Once the backlog reaches the limit, the next write is blocked with a message listing the unverified files.
- A `terminal` call whose command matches `verify_command_regex` clears the backlog **only when it passes**: the hook status is not `error`/`timeout` and the result text does not match `verify_failure_regex` (defaults catch `N failed`, `FAILED`, tracebacks, non-zero `exit_code`). A failing pytest is no longer "verification" — the backlog survives and the child must fix and rerun.
- The result is a hard act → verify (passing) → act rhythm that the child can't skip.

### 5.2 Triggers

The trigger key is `when:`, not `on:`. YAML 1.1 parses `on` as boolean `true`; the tests caught this.

| Field | Meaning |
|---|---|
| `when` | `pre_tool_call` or `post_tool_call` |
| `tool` | Regex, full match on the tool name |
| `args_regex` | `{arg: regex}`; all listed args must match |
| `result_regex`, `status` | Post only; `status` is `ok`, `error`, `blocked`, `cancelled`, or `timeout` |
| `tool_count_gte`, `consecutive_errors_gte` | Counter thresholds per child |
| `roles` | Filter on `child_role` |
| `max_fires` | Per-child cap |
| `action` | pre: `block`, `steer`, `interrupt`, `escalate`; post: `annotate`, `steer`, `interrupt`, `escalate` |

The shipped examples cover:
- force-push → block
- `rm -rf /` → interrupt (the child is then halted, so every later call is blocked)
- edits outside scope → block
- a failed test run → annotate
- 3 errors in a row → steer
- 6 errors in a row → escalate
- 80 tool calls → interrupt

`escalate` steers the parent orchestrator when the parent is itself a subagent. A top-level parent can't be steered from inside a plugin, so in that case the plugin tells the worker to put the issue in its final report.

### 5.3 LLM judge at intermediate boundaries (optional)

```yaml
judge:
  enabled: true
  base_url: http://127.0.0.1:8000/v1      # local vLLM / SGLang / llama.cpp server
  model: Qwen/Qwen3-30B-A3B-Instruct-2507
  on_tools: [write_file, patch]
  every_n_tools: 6
  gate_wait_seconds: 12                   # must stay below plugins.hook_callback_timeout
  on_revise: block
```

1. `post_tool_call` submits a review to a thread pool and returns at once, so the hook never times out. The review includes the goal and the last N calls with args and results.
2. The child's next `pre_tool_call` waits up to `gate_wait_seconds` for the verdict:
   - `pass`: the call proceeds.
   - `revise`: the call is blocked, and the message carries the judge's reason and one concrete next step. With `on_revise: steer`, the correction is queued instead.
   - `halt`: the child is interrupted.
3. If the judge has no verdict yet, the call proceeds and the check repeats at the next boundary. A verdict is timestamped by tool count: once the child has run more than `gate_max_stale_calls` (default 2) further tools since the review was submitted, the verdict is dropped as stale (audited as `judge_stale`) instead of blocking an unrelated fresh call.
4. If the judge is unreachable, it fails open to `pass`. The rule triggers remain the hard gate.

Trigger config is validated at plugin registration: bad regexes, unknown `when:`/`action:` values, or non-list `triggers:` are logged with the exact problem and the triggers block is ignored — a malformed list can never crash `evaluate()` inside `pre_tool_call` (which fails closed) and block every child call. Individual broken triggers are also skipped at evaluation time.

As an alternative to raw HTTP, plugins can call the host's model through `ctx.llm` (`agent/plugin_llm.py`).

## 6. Intercepting child outputs through the TUI gateway (JSON-RPC)

The Ink TUI and Desktop both talk to `python -m tui_gateway.entry`. It speaks newline-delimited JSON-RPC 2.0 over stdio (a WebSocket transport also exists in `tui_gateway/ws.py`). Events arrive as `{"method":"event","params":{"type":…,"session_id":…,"payload":…}}`, starting with `gateway.ready`. When the TUI is the client, the plugin already runs inside this process. Write your own client only if you want supervision outside Hermes.

### Supervision surface

| Method / event | Purpose |
|---|---|
| `session.create` → `session_id` | The session your transport owns. This is what grants steering authority |
| `prompt.submit {session_id, text}` | Start the parent turn |
| `subagent.spawn_requested` / `subagent.start` / `subagent.tool` / `subagent.progress` / `subagent.thinking` / `subagent.complete` | Child stream, emitted on the parent's session id. Payload fields include `subagent_id`, `parent_id`, `child_session_id`, `depth`, `tool_name`, `tool_preview`, `tool_count`, `status`, `summary`, `files_written`, and `output_tail` |
| `subagent.list {session_id}` | Roster: `status`, `tool_count`, `last_tool`, `accepting_steer` |
| `subagent.tail {session_id, subagent_id}` | Last 16 KB of the child's live transcript, for result-level review |
| `subagent.steer {session_id, subagent_id, text}` | Queue a correction into the child |
| `subagent.interrupt {session_id, subagent_id}` | Stop the child (`{found}`) |
| `session.steer {session_id, text}` | Steer the parent's running turn (`rejected` if the parent is idle) |
| `delegation.status` / `delegation.pause` | Global tree view and spawn pause |

Sources: `tui_gateway/methods_subagents.py`, `methods_session_interrupt.py`, `contracts/events.py`.

`subagent.tool` carries only the tool name and an argument preview, not results. To review results, `rpc/supervisor_rpc.py` pulls `subagent.tail` every N tools and matches against it.

```bash
~/.hermes/hermes-agent/venv/bin/python rpc/supervisor_rpc.py \
  --cwd ~/src/myproject --policy config/rpc-policy.json \
  --task "Refactor utils/ into a package; keep pytest green"
# audit: supervisor-rpc.audit.jsonl   gateway stderr: tui-gateway.stderr.log
```

The policy's `action` can be `steer`, `interrupt`, or `steer_parent`. `steer_parent` uses `session.steer` to alert the top-level orchestrator, which the in-process plugin can't do.

## 7. Shell-hook fallback

Use this instead of the plugin, not alongside it. Wire `child-gate.py` to `subagent_start`, `subagent_stop`, `pre_tool_call` (with `fail_closed: true`), and `post_tool_call`; the commented block in the config shows how.

- It keeps a registry of child sessions in `~/.hermes/supervisor/shell-state.json` and supervises only those sessions.
- It enforces the same verify-before-next-edit gate, plus block rules from `shell-policy.json`.
- The first run triggers a consent prompt per (event, command). For gateway or cron runs, set `hooks_auto_accept: true` or `HERMES_ACCEPT_HOOKS=1`.
- Test it with `hermes hooks test pre_tool_call`.

## 8. Caveats

- `role="orchestrator"`: the docs describe it, but in current source the per-task `role` is accepted and ignored. Capability is derived from depth (`delegation.max_spawn_depth`). Set `max_spawn_depth: 2` to get orchestrator children.
- Approvals in children: subagent threads resolve approvals without asking. With `subagent_auto_approve: false` they auto-deny, so a hook's `approve` escalation acts as a deny inside children. That's intentional; supervise with block instead.
- `plugins.isolation: host` runs the plugin in a separate process, where `tools.delegate_tool_registry` isn't reachable. Keep `in_process`.
- Gateway event hooks (`~/.hermes/hooks/*/HOOK.yaml`) only see messaging-platform lifecycle events. They can't intercept tool calls.
- A Hermes restart does not resume running children; they come back as `unknown`. For durable work, use `cronjob` or background terminal jobs ([delegation docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation)).
- Kill switches: `HERMES_SUPERVISOR_DISABLE=1`, `settings.enabled: false`, or remove the plugin from `plugins.enabled`.
- Testing status: the plugin and shell hook passed offline tests against a stubbed registry, and the shell hook passed a smoke test. Neither was run end to end against a live Hermes process. Before relying on it, run one supervised task and check `audit.jsonl`.
