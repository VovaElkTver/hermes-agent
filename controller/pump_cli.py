"""controller.pump_cli — one-shot queue pump in a detached process.

Run: python -m controller.pump_cli [--scope DIR] (default: every workdir that has
queued tasks). Invoked by the controller plugin after operator sessions (and safe
to cron). One worker at a time per scope by construction: the loop processes
ready->running claims serially and the subprocess dies when the queue empties.
"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="controller.pump_cli")
    ap.add_argument("--scope", action="append", default=None)
    ap.add_argument("--max-steps", type=int, default=50)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    from controller import model
    from controller.loop import run_cycle
    from controller.workers import make_review, make_worker

    conn = model.connect()
    scopes = a.scope
    if not scopes:
        scopes = [r["workdir"] for r in conn.execute(
            "SELECT DISTINCT workdir FROM ctl_task"
            " WHERE state IN ('ready','running')")]
    out = {}
    for scope in scopes:
        perm = model.get_permission(conn, scope)
        if not perm["enabled"]:
            out[scope] = [{"stop": "permission off"}]
            continue
        judge_cfg = None
        try:
            from hermes_cli.config import load_config_readonly
            cfg = load_config_readonly() or {}
            judge_cfg = (cfg.get("plugins", {}).get("entries", {})
                         .get("supervisor", {}).get("settings", {}).get("judge"))
        except Exception:
            pass
        out[scope] = run_cycle(
            conn, scope=scope, max_steps=a.max_steps,
            worker_fn=make_worker(conn),
            review_fn=make_review(conn, judge_cfg=judge_cfg))
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
