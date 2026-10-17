# Preemptive task switching — control-socket session verbs + ctl_step pause/resume (PR-A)

Line: autonomous-agent / controller. Parent spec: docs/controller.md. Dogfood:
fork-explore PR-2b left the scheduler able to soft pause/resume/close *fork* gates only;
a live agent turn could only be stopped or redirected by a chat platform message. PR-A
gives the scheduler the same two moves out-of-band, with system attribution and a
handoff note, and teaches ctl steps to be taken off the fire without finishing the turn.

## 1. Mechanism audit — what HEAD already had

The interrupt machinery is mature; PR-A deliberately adds no new interrupt primitive,
only an out-of-band *surface* onto it plus the step lifecycle the scheduler needs.

Already on HEAD (agent core, `agent/interrupt_control.py` + `tools/interrupt.py`):

1. **Soft vs hard interrupt with system attribution.** `interrupt(message,
   hard_cancel=, tool_reason=, require_generation=)`. `tool_reason` is the
   trusted fixed category for non-user producers (#130207: previously a system
   producer that had to stop a turn softly had no way to label itself — passing a
   message made it look like "user sent a new message" and the abort was rendered as
   a user-stop). `interrupt_issuer(agent)` returns that category;
   `USER_INTERRUPT_REASONS` (`explicit stop requested`, `user sent a new message`,
   `user interrupt`) stays attributed to the human.
2. **Abort in-flight HTTP = reasoning pause.** `_ic_abort_active_request` closes the
   live model socket (`_active_request_abort`), so a preempt costs the current API
   call, not the whole turn budget; `require_generation` claims let a liveness abort
   lose cleanly against a turn that resumed meanwhile.
3. **Cooperative tool pause.** Per-thread flags (`tools/interrupt.set_interrupt/
   is_interrupted`) + `_ic_signal_tool_workers` fan-out to concurrent-tool worker tids;
   tools observe the flag at their own checkpoints. A soft interrupt therefore stops
   the turn between iterations/tools without killing process state.
4. **Steer / redirect.** `steer(text)` buffers a user row delivered after the current
   tool batch (no abort); `redirect(text)` cancels only the in-flight model request and
   retries the same logical turn with the correction appended (degrades to steer while
   tools execute).
5. **Child propagation.** `interrupt()` walks `_active_children` (delegated subagents)
   with the same hard/soft/tool_reason shape; the gateway `/steer` path mirrors the
   fan-out for the other direction.
6. **Codex hook.** `_ic_codex_method(self, "request_interrupt")` /
   `"request_steer"` — app-server-backed turns get native interrupts/steer on the same
   call sites, so any surface that goes through `interrupt()/steer()` inherits them.

What HEAD did NOT have (all delivered by this PR):

* **No out-of-band surface.** All six mechanisms above fire only from inside the
  process or from an inbound chat message (`/stop`, busy-path redirect). A scheduler in
  another process had exactly one blunt tool: SIGUSR1-style process restart.
* **`ctl_step` had no preempt lifecycle.** Statuses were effectively pending/done
  (`add_step` → `finish_step`, an unconditional UPDATE); a running worker's step could
  not be taken off the fire, there was no CAS on the status column, no read-back, and
  no place to record "where the turn was standing" — a killed attempt restarted blind.
* **No resume handoff.** After any stop, the next attempt re-narrated from zero
  (re-read files, redone reasoning) because nothing recorded the cut point.

## 2. The two verbs

`gateway/run_session_verbs.py` (registered in `_start_gateway_start_control_socket`):

* **`interrupt-session`** — params `{session, reason?, message?, hard?}`.
  `session` resolves exact key first, else a *unique* substring over running keys
  (ambiguous → error, none → `interrupted: false`). Calls
  `agent.interrupt(message, hard_cancel=hard, tool_reason=reason)` — soft by default,
  which is a *pause* through mechanism 3 above, not a kill. `reason` defaults to
  `controller_preemption` and is slugged: it is a category, never free text — the
  #112647 invariant is that producers name themselves, so the turn record says
  "scheduler preempted", never "user stopped me" (`interrupt_issuer(agent) != None`,
  and not in `USER_INTERRUPT_REASONS`). Falls back to the legacy `interrupt(message)`
  ABI for third-party agents whose signature predates `tool_reason` (the preempt
  lands, only the label degrades).
* **`steer-session`** — params `{session, message}`. Calls `agent.steer()` with the
  same `_active_children` fan-out the chat `/steer` path uses; `accepted: false` +
  `reason: no-running-turn` when idle lets the scheduler queue its own follow-up.

Client wrappers `interrupt_gateway_session()` / `steer_gateway_session()` live in
`gateway/control_socket.py`; both return **None** when no gateway answers or the
gateway predates the verbs — the caller keeps its legacy path (same back-compat
contract as `pause-for-update`, #92091).

Handlers run on the socket executor thread: safe, because `interrupt()/steer()` are
lock-guarded and cross-thread by design (the mixin docstring's "call from another
thread" is exactly this seam).

## 3. ctl_step paused/resume

`controller/model.py` adds the step lifecycle as compare-and-set primitives
(`mark_step_in_progress`, `pause_step`, `resume_step`, `unstick_step`): each is
BEGIN IMMEDIATE, checks the stored status, writes only on match, reads back after
write, and returns `ok` + the STORED status — the preempt never trusts a worker's
exit claim for what the step's state actually became underneath it. `pause_step`
carries the handoff note into a `step_paused` ctl_event; `paused_handoff_note()`
reads it back (newest first).

`controller/loop.py`:
* dispatch **claims** the step (pending→in_progress CAS) before calling the worker —
  without the claim there is nothing for an out-of-band pause to CAS against;
* after the worker returns, the loop **re-reads the step**: `paused` (someone
  preempted mid-turn) wins over whatever the worker reported — the run is marked
  `interrupted`, the task transitions to `paused`, and the step keeps the handoff;
* worker-reported `interrupted` likewise parks the step `paused` with the worker's own
  handoff note instead of failing the task;
* normal fail/revise paths release the claim (`unstick_step`) so a later attempt is
  dispatchable again — the old code relied on the step still being `pending`.

Re-anchoring: `queue.next_step` sees only `pending`, so a paused step is invisible to
dispatch until `resume_step`; on the next dispatch the loop injects the stored
handoff into the step payload (`step["handoff"]`) and the worker brief is rebuilt from
the *current* contract revision — resume continues from the cut point against today's
contract, cheaply, without a re-narrated prefix.

A task holding paused steps parks as `paused: preempted step awaiting resume`
instead of marching into `verifying` with unfinished work.

## 4. Handoff note

Same shape on both halves: "was doing X; stopped on tool Y (iteration i/b)", built
from the agent's own `_last_activity_desc` / `_current_tool` spinner state (no extra
instrumentation). The gateway verb returns it, stages it into the session's pending
turn-sidecar notes (so the next chat-platform turn in that session opens with it), and
the controller stores it in the step's `step_paused` event for the resume dispatch.

## 5. Tests

* `tests/gateway/test_control_socket_session_verbs.py` — verbs dispatched through
  `handle_request_line` (real AIAgent stand-ins): system attribution
  (`tool_reason=controller_preemption`, `interrupt_issuer` not a user category),
  message-carrying preempt stays system-attributed (#130207 shape), hard variant,
  no-running-turn, unique-substring + ambiguity, steer fan-out to children,
  full client roundtrip over a REAL unix socket, and the **old-gateway fallback**:
  a server without the verbs answers `ok:false` → both clients return `None`.
* `tests/plugins/test_controller_step_pause.py` — CAS round-trip + read-back
  semantics (stale preempt lands as `ok:false` + stored), handoff persistence/reload,
  external preempt during worker (worker "finished" is overridden by the paused
  read-back), worker-reported interrupted → paused not failed, resume re-dispatches
  with the handoff re-anchored into the step, paused-step blocking of `verifying`,
  failed-worker claim release.
* `tests/plugins/test_controller_preempt.py` (PR-B) — `request_preempt` unstages the
  in_progress step + flags the run synchronously (db_flag path), picks the
  control-socket path when the gateway answers, degrades to the durable flag when the
  client returns None (old gateway), never raises for an idle task; `_preempt_signal`
  reads both durable signals and fails open; a REAL stub subprocess sitting on a tool
  is tree-killed mid-run (turn not finished) with the handoff carried back; the
  resumed brief carries the RESUME/handoff block, a fresh one does not.

## 6. PR-B — the scheduler-side preempt (workers cooperate, requests are one call)

PR-A gave the step a preemptable lifecycle and the gateway live verbs; PR-B closes the
loop for the DEFAULT worker (a detached `hermes chat --oneshot` subprocess that owns
no socket and whose session verbs do not apply):

* `controller/workers.py` — `_await_worker` replaces the single blocking
  `communicate(timeout=…)`: poll-interval waits (`preempt_poll_s`, default 2s) check
  `_preempt_signal` — the run flagged `interrupt_requested` OR the claimed step
  flipped `in_progress → paused`. Either lands: the detached group is tree-killed
  (same `_kill_tree` as the timeout arm) and the result is `status: "interrupted"`
  + the stored handoff, so the loop parks the step paused, never failed. Partial
  stdout survives (CPython keeps communicate buffers across calls since bpo-39531).
  The legacy hard-timeout arm is unchanged (`timeout_s + grace_s`, default 60s).
* `controller/preempt.py` — `request_preempt(conn, task_id, reason, handoff, session,
  home)`: tries the gateway verb when a session/home is given (client `None` →
  fall back), then ALWAYS does the durable half in-line: flag the open run
  `interrupt_requested`, CAS every in_progress step to paused with the handoff note
  (PR-A primitives), and emit `preempt_requested` with `{reason, via, handoff}` —
  the journal line that answers "who removed this turn and why" (system reason,
  never a user stop). Returns `via: control_socket | db_flag`.
* `workers._brief` — a step carrying `handoff` (set by the loop's re-anchoring) gets
  a `## RESUME` block: the cut-point note plus "do NOT redo finished work, re-anchor
  on the CURRENT acceptance/facts above" — the contract fields are re-read at brief
  build time, so a contract edit that landed during the pause re-anchors the resume.
