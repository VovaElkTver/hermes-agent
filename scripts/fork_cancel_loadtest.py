#!/usr/bin/env python3
"""Cancel-behavior load test for fork_explore branches (SPEC 5.4 / 13.7:
"нагрузочно проверить отмену веток: после cancel слоты батча должны
освобождаться" — verification, not trust).

fork_explore cancels a doomed branch by abandoning the HTTP stream (client
disconnect). This test proves the SGLang server (the :8001/:8012 fleet hosts)
actually frees the batch slot on disconnect:

  1. baseline load from /get_load must be idle;
  2. fire K streaming generations long enough to hold slots;
  3. wait until the server reports them running (slots really taken);
  4. half of them are cancelled (connection closed mid-stream), half kept;
  5. PASS iff the server's running count drops to the keep-count within
     --cancel-sla seconds AND every kept stream finishes cleanly AND the
     queue drains to empty.

Usage:  python scripts/fork_cancel_loadtest.py [--base-url http://192.168.9.100:8012]
Exit code 0 = PASS (cancel frees slots), 1 = FAIL (fork early-stop is unsafe).
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.request

DEFAULT_BASE = "http://192.168.9.100:8012"


def _get_json(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _model(base: str) -> str:
    return _get_json(f"{base}/v1/models")["data"][0]["id"]


def _running(base: str) -> int:
    loads = _get_json(f"{base}/get_load")
    return sum(int(l.get("num_reqs", 0)) for l in loads)


def _waiting(base: str) -> int:
    loads = _get_json(f"{base}/get_load")
    return sum(int(l.get("num_waiting_reqs", 0)) for l in loads)


class Branch:
    """One streaming chat generation; close() = the exact cancel path a fork
    branch takes when the deadline fires or consensus stops it."""

    def __init__(self, base: str, model: str, max_tokens: int, prompt: str):
        self.url = f"{base}/v1/chat/completions"
        body = {"model": model, "max_tokens": max_tokens, "temperature": 0.0,
                "stream": True, "seed": 17,
                "messages": [{"role": "user",
                              "content": prompt + " Рассуждай подробно и по шагам."}]}
        self._req = urllib.request.Request(
            self.url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        self.resp = None
        self.chunks = 0
        self.done = False
        self.error: Exception | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def run(self):
        try:
            with self._lock:
                self.resp = urllib.request.urlopen(self._req, timeout=240)
                for raw in self.resp:
                    if self._stop.is_set():
                        break
                    if raw.strip().startswith(b"data:"):
                        self.chunks += 1
                self.done = True
        except Exception as exc:  # health: allow BLE001 -- reported via self.error, never raised across threads
            if not self._stop.is_set():
                self.error = exc

    def cancel(self):
        self._stop.set()
        with self._lock:
            if self.resp is not None:
                try:
                    self.resp.close()
                except OSError:
                    pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--k", type=int, default=6, help="concurrent branches")
    ap.add_argument("--max-tokens", type=int, default=1024)
    ap.add_argument("--cancel-sla", type=float, default=15.0,
                    help="seconds allowed for the server to free cancelled slots")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    model = _model(base)
    if _running(base) or _waiting(base):
        print(f"FAIL: server not idle before test (running={_running(base)})")
        return 1

    branches = [Branch(base, model, args.max_tokens,
                       f"Задача {i}: спроектируйdry-run маршрут развития системы.")
                for i in range(args.k)]
    half = max(1, args.k // 2)
    keep, doomed = branches[half:], branches[:half]

    threads = [threading.Thread(target=b.run, daemon=True) for b in branches]
    t0 = time.monotonic()
    for t in threads:
        t.start()

    # 1) prove the slots are really taken (or waiting) before cancelling
    occupied = 0
    while time.monotonic() - t0 < 60:
        occupied = _running(base) + _waiting(base)
        if occupied >= args.k:
            break
        time.sleep(0.3)
    print(f"[load] server reports {occupied} reqs (k={args.k}) after "
          f"{time.monotonic() - t0:.1f}s")
    if occupied < max(2, half + 1):
        print("FAIL: could not occupy the batch — nothing to cancel")
        return 1

    # 2) cancel the doomed half the way fork_explore does: client disconnect
    for b in doomed:
        b.cancel()
    t_cancel = time.monotonic()

    # 3) the freed-slot assertion: running drops to the keep-count inside SLA
    freed = False
    while time.monotonic() - t_cancel < args.cancel_sla:
        if _running(base) + _waiting(base) <= len(keep):
            freed = True
            break
        time.sleep(0.25)
    drop_ms = int((time.monotonic() - t_cancel) * 1000)
    print(f"[cancel] slots freed to keep-count in {drop_ms}ms "
          f"(SLA {int(args.cancel_sla * 1000)}ms): {'yes' if freed else 'NO'}")

    for t in threads:
        t.join(timeout=180)

    kept_err = [b.error for b in keep if b.error]
    kept_ok = sum(1 for b in keep if b.done and not b.error)
    drained = (_running(base) == 0 and _waiting(base) == 0)
    print(f"[keep]   {kept_ok}/{len(keep)} kept streams completed cleanly"
          + (f"; errors: {kept_err[:2]}" if kept_err else ""))
    print(f"[drain]  queue empty after test: {drained}")

    ok = freed and not kept_err and kept_ok == len(keep) and drained
    print("RESULT: " + ("PASS — cancel frees SGLang batch slots promptly"
                        if ok else "FAIL — early-stop cancel is NOT safe to rely on"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
