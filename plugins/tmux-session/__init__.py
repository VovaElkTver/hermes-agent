"""Persistent tmux terminal sessions for Hermes.

A private tmux server (dedicated socket under $HERMES_HOME/tmux/) hosts one
session per workspace with windows: workspace, logs, tests, server. Shell state
— cwd, exports, activated venvs, running SSH sessions and REPLs — lives in the
panes and therefore survives between tool calls AND gateway restarts.

Two distinct input modes (the whole point of this toolset):
  execute_shell  — run a command in the pane's *shell* context; tracked via a
                   unique completion marker written to a temp file, so exit
                   codes are real, never guessed from "the screen shows $" and
                   marker text echoed by the command itself can't fake it.
  send_text / send_keys — type into whatever interactive program is frontmost
                   (python REPL, ssh, psql, a pager). No marker is appended;
                   use inspect/wait to observe effects.

The adapter never kills processes or sessions on timeout. tmux binary must be
installed by the user (apt/brew); we only surface a clear error when missing.

Config ($HERMES_HOME/config.yaml): plugins: tmux-session: {socket_name, ...}
Defaults are sane; nothing needs configuring.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Register the tmux toolset (called once by the plugin loader)."""
    try:
        from .tmux_core import check_tmux_available
        ok, detail = check_tmux_available()
    except Exception as exc:  # pragma: no cover - defensive
        ok, detail = False, f"tmux-session import failed: {exc}"
    if not ok:
        logger.warning("tmux-session plugin inactive: %s", detail)
        return

    from .tools import TOOLS
    from .tmux_core import check_tmux_available as _unused_check  # noqa: F401

    for name, schema, handler, emoji in TOOLS:
        ctx.register_tool(name=name, toolset="tmux_session", schema=schema,
                          handler=handler, emoji=emoji)
    logger.info("tmux-session: registered %d tools on socket %s",
                len(TOOLS), __name__)
