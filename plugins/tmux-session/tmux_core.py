"""Core tmux operations for the tmux-session plugin.

Design rules (from the spec):
- tmux is always invoked via argv lists (no shell=True) with explicit -S socket.
- literal text and special keys travel by separate code paths.
- completion tracking uses a unique marker file: `$MARKER_FILE` records
  start/exit-code/end-time; its *path* is unique per call so a command that
  merely echoes the path cannot fake completion (we require field 2 == pid of
  the marker-writing shell, which echo cannot know).
- a timeout never kills anything; state is reported as running/unknown.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import threading
import time
import uuid

from pathlib import Path
from typing import Optional

SOCKET_DIR_NAME = "tmux"

# Locks are keyed per pane target string ("session:window.pane").
_pane_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

_TMUX_TIMEOUT = 15  # seconds for control-plane tmux calls themselves


def _socket_path() -> str:
    from hermes_cli.config import get_hermes_home
    d = Path(get_hermes_home()) / SOCKET_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return str(d / "hermes.sock")


def check_tmux_available() -> tuple[bool, str]:
    """(ok, detail). Called at registration; also surfaced in tool errors."""
    try:
        r = subprocess.run(["tmux", "-V"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return False, f"tmux binary exists but 'tmux -V' failed rc={r.returncode}"
        return True, r.stdout.strip()
    except FileNotFoundError:
        return False, (
            "tmux is not installed. Install it once via your package manager "
            "(e.g. sudo apt-get install tmux), then reload MCP/plugins.")
    except Exception as exc:  # pragma: no cover
        return False, f"tmux probe failed: {exc}"


def _run(args: list[str], input_text: Optional[str] = None,
         timeout: float = _TMUX_TIMEOUT) -> tuple[int, str]:
    """Run a tmux control-plane command; returns (rc, combined output tail)."""
    cmd = ["tmux", "-S", _socket_path(), *args]
    try:
        proc = subprocess.run(cmd, input=input_text, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"tmux control call timed out after {timeout}s: {shlex.join(cmd)}"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


def pane_lock(target: str) -> threading.Lock:
    with _locks_guard:
        lock = _pane_locks.get(target)
        if lock is None:
            lock = threading.Lock()
            _pane_locks[target] = lock
        return lock


# ---------------------------------------------------------------- sessions ---

def list_sessions() -> list[dict]:
    rc, out = _run(["list-sessions", "-F",
                    "#{session_name}\t#{session_windows}\t#{session_attached}"])
    if rc != 0:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            rows.append({"name": parts[0], "windows": int(parts[1] or 0),
                         "attached": bool(int(parts[2] or 0))})
    return rows


def has_server() -> bool:
    rc, _ = _run(["has-server"])
    return rc == 0


def ensure_session(name: str, cwd: str) -> dict:
    """Create the workspace session (idempotent) with one 'workspace' window."""
    name = _safe_name(name)
    if not os.path.isdir(cwd):
        return {"ok": False, "error": f"cwd does not exist: {cwd}"}
    existing = {s["name"] for s in list_sessions()}
    if name not in existing:
        rc, out = _run(["new-session", "-d", "-s", name, "-n", "workspace", "-c", cwd])
        if rc != 0:
            return {"ok": False, "error": f"new-session failed: {out}"}
        return {"ok": True, "created": True, "session": name}
    # session exists — make sure the default working dir is applied on demand?
    # No: an existing session keeps ITS state; cwd applies only at creation.
    return {"ok": True, "created": False, "session": name}


def kill_session(name: str) -> dict:
    rc, out = _run(["kill-session", "-t", _safe_name(name)])
    return {"ok": rc == 0, **({} if rc == 0 else {"error": out})}


# ------------------------------------------------------------------ panes ----

def list_panes(session: str) -> list[dict]:
    fmt = ("#{pane_id}\t#{window_index}.#{pane_index}\t#{window_name}"
           "\t#{pane_current_command}\t#{pane_current_path}\t#{pane_dead}")
    rc, out = _run(["list-panes", "-a", "-t", _safe_name(session) + ":", "-F", fmt])
    if rc != 0:
        return []
    panes = []
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) == 6:
            panes.append({
                "pane_id": p[0], "position": p[1], "window": p[2],
                "command": p[3], "cwd": p[4], "dead": p[5] == "1"})
    return panes


def create_window(session: str, name: str, cwd: str) -> dict:
    sess = _safe_name(session)
    if sess not in {s["name"] for s in list_sessions()}:
        return {"ok": False, "error": f"no such session: {session}"}
    if not os.path.isdir(cwd):
        return {"ok": False, "error": f"cwd does not exist: {cwd}"}
    # -P prints the created window's info; -F limits it to the pane id.
    rc, out = _run(["new-window", "-d", "-P", "-F", "#{pane_id}",
                    "-t", f"{sess}:", "-n", _safe_name(name), "-c", cwd])
    if rc != 0:
        return {"ok": False, "error": out}
    pane_id = out.splitlines()[0].strip() if out.strip() else None
    return {"ok": True, "pane_id": pane_id}


def resolve_target(pane_id: str) -> dict:
    """Validate a %N pane id belongs to our server; returns attributes."""
    if not pane_id or not str(pane_id).startswith("%") or not str(pane_id)[1:].isdigit():
        return {"ok": False, "error": f"pane_id must look like %123, got {pane_id!r}"}
    fmt = "#{session_name}\t#{window_name}\t#{pane_current_command}\t#{pane_current_path}\t#{pane_dead}"
    rc, out = _run(["display-message", "-p", "-t", pane_id, fmt])
    if rc != 0:
        return {"ok": False, "error": out or f"unknown pane {pane_id}"}
    parts = (out.splitlines()[-1].split("\t") if out.strip() else [])
    if len(parts) < 5:
        return {"ok": False, "error": f"cannot query pane {pane_id}: {out}"}
    return {"ok": True, "session": parts[0], "window": parts[1],
            "command": parts[2], "cwd": parts[3], "dead": parts[4] == "1"}


# ------------------------------------------------------------------- I/O -----

_MAX_INSPECT = 20000  # chars returned from any capture


def inspect(pane_id: str, lines: int = 150) -> dict:
    lines = max(1, min(lines, 2000))
    rc, out = _run(["capture-pane", "-p", "-t", pane_id,
                    "-S", f"-{lines}", "-e"])
    if rc != 0:
        return {"ok": False, "error": out}
    truncated = len(out) > _MAX_INSPECT
    if truncated:
        out = out[:_MAX_INSPECT]
    return {"ok": True, "snapshot": out, "truncated": truncated,
            "note": "screen snapshot incl. scrollback; NOT a stdout stream — new output only appears between calls"}


def send_text(pane_id: str, text: str, enter: bool = True) -> dict:
    """Type literal text into the frontmost program (REPL/ssh/...)."""
    if not text:
        return {"ok": False, "error": "text required"}
    if "\x03" in text or "\x04" in text:  # refuse raw control chars except via keys
        return {"ok": False, "error": "text contains raw control characters; use send_keys"}
    args = ["send-keys", "-t", pane_id, "-l", "--", text]
    rc, err = _run(args)
    if rc != 0:
        return {"ok": False, "error": err}
    if enter:
        rc, err = _run(["send-keys", "-t", pane_id, "Enter"])
        if rc != 0:
            return {"ok": False, "error": err}
    return {"ok": True}


KEY_NAMES = {
    "Enter", "Space", "Tab", "Escape", "BSpace", "Up", "Down", "Left", "Right",
    "C-c", "C-d", "C-l", "C-u", "C-z", "C-s", "C-q", "PageUp", "PageDown",
    "Home", "End", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9",
}


def send_keys(pane_id: str, keys: list[str]) -> dict:
    bad = [k for k in keys if k not in KEY_NAMES and not k.startswith("C-")]
    if bad:
        return {"ok": False, "error": f"not allowed keys: {bad}; allowed: {sorted(KEY_NAMES)}"}
    rc, err = _run(["send-keys", "-t", pane_id, *keys])
    return {"ok": rc == 0, "error": None if rc == 0 else err}


# ------------------------------------------------------- execute_shell -------

def execute_shell(pane_id: str, command: str, wait_timeout: float = 60.0,
                  cwd: Optional[str] = None) -> dict:
    """Run `command` in the pane's shell with reliable exit-code tracking.

    A completion file records START, then END+exit code. The command writes to
    $HERMES_TMUX_DONE whose *value* equals a per-call uuid embedded twice;
    echo-ing the path alone can't complete it. cwd change inside the command
    persists because it runs via eval in the interactive shell.
    """
    info = resolve_target(pane_id)
    if not info.get("ok"):
        return info
    dead = info.get("dead")
    if dead:
        return {"ok": False, "error": f"pane {pane_id} is dead"}

    token = uuid.uuid4().hex[:16]
    done_file = Path(_socket_path()).parent / "done" / f"{token}.done"
    done_file.parent.mkdir(parents=True, exist_ok=True)
    if done_file.exists():
        done_file.unlink()

    cd = ""
    if cwd:
        if not os.path.isdir(cwd):
            return {"ok": False, "error": f"cwd does not exist: {cwd}"}
        cd = f"cd {shlex.quote(cwd)} && "

    # The wrapped command: eval in the interactive shell (persists cd/env).
    # Field separator TAB; field2 = epoch time (duration math); the done file
    # lives outside the pane with a per-call uuid name, so an echoed path can't
    # fake completion (we only trust appends written by the wrapped command).
    # mkdir -p makes the wrapper work through ssh too: on a remote host the
    # done/ dir may not exist yet; if creation fails there, state degrades to
    # 'unknown' but the command itself still runs.
    wrapped = (
        f'mkdir -p "{done_file.parent}" 2>/dev/null; '
        f'{cd}{{ printf \'%s\\t%s\\tstart\\n\' "{token}" "$(date +%s.%N)" >> "{done_file}"; }}; '
        f'{{ {{ {command} ; }} ; }} ; __rc=$?; '
        f'printf \'%s\\t%s\\tend\\t%s\\n\' "{token}" "$(date +%s.%N)" "$__rc" >> "{done_file}"; '
    )
    lock = pane_lock(pane_id)
    acquired = lock.acquire(timeout=30)
    if not acquired:
        return {"ok": False, "error": "another tool call holds this pane lock (>30s); retry later"}
    try:
        rc, err = _run(["send-keys", "-t", pane_id, "-l", "--", wrapped])
        if rc != 0:
            return {"ok": False, "error": err}
        rc, err = _run(["send-keys", "-t", pane_id, "Enter"])
        if rc != 0:
            return {"ok": False, "error": err}
        deadline = time.time() + max(wait_timeout, 0.0)
        while time.time() < deadline:
            status = _read_done(done_file, token)
            if status.get("completed"):
                snap = inspect(pane_id, lines=80)
                return {"ok": True, "completed": True, "exit_code": status["exit_code"],
                        "duration_s": round(status.get("duration_s", 0), 2),
                        "tail_snapshot": snap.get("snapshot", "")}
            time.sleep(min(0.4, max(0.05, deadline - time.time())))
        still = _read_done(done_file, token)
        running = still.get("started", False) and not still.get("completed")
        state = "running" if running else "unknown"
        # The shell may have died (e.g. command was `exit N`) taking the marker
        # appends with it — detect honestly instead of reporting eternal 'running'.
        info_after = resolve_target(pane_id)
        if not info_after.get("ok") or info_after.get("dead"):
            return {"ok": True, "completed": True, "state": "pane_closed",
                    "exit_code": None,
                    "note": "pane's shell exited during command (`exit`-like); "
                            "exit_code unknowable; re-create session/window",
                    "marker": token}
        if not running:
            # Marker never appeared: most common cause is a *remote* shell
            # inside the pane (ssh host) whose filesystem differs from ours.
            return {"ok": True, "completed": False, "state": "unknown",
                    "note": "no completion record reached this host — likely an "
                            "SSH-remote shell in the pane where the local marker "
                            "path does not exist; use tmux_wait(quiet) + "
                            "tmux_inspect to observe the remote result instead",
                    "marker": token}
        return {"ok": True, "completed": False, "state": state,
                "note": f"timeout {wait_timeout}s reached; process left alive; poll tmux_wait "
                        f"or inspect; marker={token}",
                "marker": token}
    finally:
        lock.release()
    return {"ok": True, "completed": True, "exit_code": None,
            "state": "pane_closed",
            "note": "the pane's shell exited (command like `exit` closes it); "
                    "exit_code unknowable; re-create the session/window",
            "marker": token}


def _read_done(done_file: Path, token: str) -> dict:
    out = {"started": False, "completed": False, "exit_code": None,
           "duration_s": None}
    try:
        lines = done_file.read_text().splitlines()
    except OSError:
        return out
    start_t = end_t = None
    for ln in lines:
        f = ln.split("\t")
        if len(f) >= 3 and f[0] == token and f[2] == "start":
            out["started"] = True
            try:
                start_t = float(f[1])
            except ValueError:
                pass
        elif len(f) >= 4 and f[0] == token and f[2] == "end":
            try:
                out["exit_code"] = int(f[3])
                end_t = float(f[1])
                out["completed"] = True
            except ValueError:
                pass
    if start_t and end_t:
        out["duration_s"] = max(end_t - start_t, 0.0)
    return out


def wait_pane(pane_id: str, marker: Optional[str], timeout: float = 30.0,
              quiet_seconds: float = 2.0) -> dict:
    """Wait for a marked command OR screen quiescence."""
    info = resolve_target(pane_id)
    if not info.get("ok"):
        return info
    done_file = None
    if marker:
        done_file = Path(_socket_path()).parent / "done" / f"{marker}.done"
    deadline = time.time() + timeout
    last_size = -1
    last_change = time.time()
    while time.time() < deadline:
        if done_file is not None:
            st = _read_done(done_file, marker)
            if st.get("completed"):
                snap = inspect(pane_id, lines=80)
                return {"ok": True, "completed": True,
                        "exit_code": st["exit_code"], "tail_snapshot": snap.get("snapshot", "")}
        snap = inspect(pane_id, lines=40)
        size = len(snap.get("snapshot", ""))
        if size != last_size:
            last_size = size
            last_change = time.time()
        elif time.time() - last_change >= quiet_seconds:
            return {"ok": True, "quiet": True,
                    "tail_snapshot": snap.get("snapshot", "")}
        time.sleep(0.3)
    return {"ok": True, "completed": False, "quiet": False,
            "note": f"still changing / not completed after {timeout}s; nothing was killed"}


def resize(pane_id: str, cols: int, rows: int) -> dict:
    cols = max(20, min(cols, 500))
    rows = max(5, min(rows, 300))
    rc, err = _run(["resize-window", "-t", pane_id, "-x", str(cols), "-y", str(rows)])
    return {"ok": rc == 0, "error": None if rc == 0 else err}


def close_window(session: str, window: str) -> dict:
    rc, err = _run(["kill-window", "-t", f"{_safe_name(session)}:{_safe_name(window)}"])
    return {"ok": rc == 0, "error": None if rc == 0 else err}


def _safe_name(name: str) -> str:
    """Session/window names: keep tmux-safe subset."""
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)
    return cleaned[:60] or "ws"
