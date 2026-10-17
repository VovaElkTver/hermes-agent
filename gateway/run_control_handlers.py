"""Control-socket verb handlers that live outside the ``run.py`` facade (ratchet offset).

Factories take the runner (and the event loop where the handler must marshal onto it)
and return the bare ``() -> dict`` handlers registered in
``GatewayControlServer(verb_handlers=...)``. Behaviour is unchanged from when these
were inline closures in ``_start_gateway_start_control_socket``; they moved here so the
facade only grows by its registration lines.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)


def pause_for_update_verb(runner: Any, loop: asyncio.AbstractEventLoop) -> Callable[[], dict]:
    """``pause-for-update`` (#92091 step 2): the updater asks this gateway to drain in-flight
    turns and exit cleanly — releasing every venv file handle — instead of being tree-killed
    mid-turn. Same drain path as SIGUSR1/service restarts (request_restart(via_service=True));
    the updater (or the service manager) relaunches after the code swap. The handler runs on
    the socket executor thread, so marshal onto the loop.
    """

    def _pause_for_update_handler() -> dict:
        try:
            from hermes_cli.gateway import _get_restart_drain_timeout
            _drain = float(_get_restart_drain_timeout())
        except Exception:
            logger.debug("pause-for-update: drain timeout lookup failed", exc_info=True)
            _drain = 30.0
        accepted_box: list[bool] = []
        _done = threading.Event()

        def _request() -> None:
            try:
                accepted_box.append(runner.request_restart(detached=False, via_service=True))
            finally:
                _done.set()

        loop.call_soon_threadsafe(_request)
        _done.wait(timeout=5.0)
        accepted = bool(accepted_box and accepted_box[0])
        return {
            "pausing": accepted, "already_stopping": not accepted,
            "pid": os.getpid(), "drain_timeout": _drain}

    return _pause_for_update_handler


def rescan_profiles_verb(runner: Any, loop: asyncio.AbstractEventLoop) -> Callable[[], dict]:
    """``rescan-profiles``: ``hermes profile create/delete`` asks the multiplexer to reconcile
    ``profiles/`` now (the watcher also rescans periodically). Runs on the socket executor:
    marshal onto the loop and wait briefly so the caller learns whether the profile is served."""

    def _rescan_profiles_handler() -> dict:
        if not getattr(runner.config, "multiplex_profiles", False):
            return {"multiplex": False, "served_profiles": runner.served_profile_names()}
        future = asyncio.run_coroutine_threadsafe(
            runner.reconcile_served_profiles(reason="control-socket"), loop)
        try:
            # Bounded: a token-less create reconciles in milliseconds; a credential-add whose adapter
            # connect outlasts this keeps running and the caller sees ``pending`` (not an error).
            return {"multiplex": True, **future.result(timeout=5.0)}
        except concurrent.futures.TimeoutError:
            return {"multiplex": True, "pending": True, "served_profiles": runner.served_profile_names()}

    return _rescan_profiles_handler
