# tmux-session plugin

Persistent interactive terminal sessions for the Hermes agent via a **private
tmux server** (dedicated socket). Shell state — cwd, exports, activated venvs,
SSH logins, REPLs — lives in tmux panes and therefore survives between tool
calls AND gateway restarts.

## Install

1. tmux binary (user-level prerequisite): `sudo apt-get install tmux` / `brew install tmux`
2. Copy this directory into `$HERMES_HOME/plugins/tmux-session` or keep it bundled in the repo checkout.
3. `hermes plugins enable tmux-session`

The plugin degrades gracefully: without the tmux binary it registers nothing and logs why.

## Tools (toolset `tmux_session`)

    tmux_list_sessions  list sessions on our private socket
    tmux_ensure_session idempotent workspace session (+ returns its %N pane)
    tmux_list_panes     panes of a session (stable %N ids)
    tmux_create_window  add a window (logs/tests/server) → new %N pane id
    tmux_inspect        capture screen+scrollback snapshot (NOT an stdout stream)
    tmux_send_text      literal typing to the frontmost program (REPL/ssh/psql)
    tmux_send_keys      special keys (C-c, C-d, Enter, arrows...)
    tmux_execute_shell  run command in the pane's shell; real exit code via
                        completion-marker file; timeout leaves process alive
                        (state=running + marker for tmux_wait)
    tmux_wait           wait marker-completion OR output quiescence
    tmux_resize         resize the pane's window
    tmux_close          close window/session (explicit user approval required)

## Two input modes

- `execute_shell` runs inside the pane's *shell* context and tracks completion
  through a unique marker file under `$HERMES_HOME/tmux/done/`. Exit codes are
  real; "the screen shows $" is never treated as completion.
- `send_text`/`send_keys` type into whatever interactive program is frontmost.
  No marker is appended to REPL/ssh input.

## SSH notes

- Local pane running `ssh host`: follow-up `send_text` continues inside that
  SSH session; Ctrl-C stops remote foreground jobs without killing the pane.
- Full exit-code tracking works when the pane's shell filesystem matches ours
  (same host or loopback ssh). On truly remote filesystems the marker cannot
  reach us and execute_shell honestly reports `state=unknown`; observe with
  `tmux_wait(quiet)` + `tmux_inspect`.

## Manual attach

    tmux -S ~/.hermes/tmux/hermes.sock list-sessions
    tmux -S ~/.hermes/tmux/hermes.sock attach -t <session>

## Tests

    pytest tests/plugins/test_tmux_session.py

Requires the tmux binary; creates disposable sessions prefixed `tmuxsess-`.
