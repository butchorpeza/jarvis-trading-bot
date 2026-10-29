"""JARVIS autopilot: runs the pipeline on a schedule. Dry-run by default (zero orders).
  python autopilot.py                      dry-run every 15 min (MT5 demo data)
  python autopilot.py --sample             synthetic data, no MT5
  python autopilot.py --paper-run          submits approved DEMO orders each pass (deliberate choice)
Kill switch: create the file data/KILL (the dashboard button does this) or press Ctrl+C.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import main as jarvis_main

KILL_FILE = "data/KILL"
STATUS_FILE = "data/autopilot_status.json"


def write_status(**kw):
    os.makedirs("data", exist_ok=True)
    kw["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    json.dump(kw, open(STATUS_FILE, "w"))


def sleep_with_kill(seconds: int) -> bool:
    """Sleep in 1s steps so the kill switch reacts quickly. True if killed."""
    for _ in range(seconds):
        if os.path.exists(KILL_FILE):
            return True
        time.sleep(1)
    return os.path.exists(KILL_FILE)


def run(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=900, help="seconds between passes (default 900)")
    ap.add_argument("--max-passes", type=int, default=0, help="stop after N passes (0 = run until killed)")
    ap.add_argument("--max-errors", type=int, default=20, help="stop after N consecutive failed passes")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--sample", action="store_true")
    g.add_argument("--paper-run", action="store_true", help="submit approved DEMO orders")
    args = ap.parse_args(argv)

    mode = "sample" if args.sample else "demo-orders" if args.paper_run else "dry-run"
    flags = ["--sample"] if args.sample else ["--paper-run"] if args.paper_run else ["--dry-run"]
    os.makedirs("data", exist_ok=True)
    if os.path.exists(KILL_FILE):
        os.remove(KILL_FILE)   # a stale kill file from a previous run must not block a deliberate start
    print(f"JARVIS autopilot started: mode={mode}, every {args.interval}s. Kill: create {KILL_FILE} or Ctrl+C.")
    passes = errors = 0
    try:
        while True:
            started = datetime.now(timezone.utc).isoformat(timespec="seconds")
            print(f"\n=== pass {passes + 1} @ {started} ===")
            try:
                code = jarvis_main.main(flags)
            except Exception as e:  # never crash the loop silently
                print(f"pass crashed: {type(e).__name__}: {e}")
                code = 1
            passes += 1
            errors = errors + 1 if code != 0 else 0
            write_status(state="running", mode=mode, passes=passes, last_pass=started,
                         last_exit_code=code, consecutive_errors=errors, interval=args.interval)
            if errors >= 2 and not args.sample:
                try:
                    import MetaTrader5 as mt5
                    from jarvis import brokerlink
                    ok, msg = brokerlink.reconnect(mt5, timeout=45)
                    print(f"reconnect: {msg}", flush=True)
                    if ok:
                        errors = 0
                except Exception as e:  # noqa: BLE001
                    print(f"reconnect failed: {type(e).__name__}: {e}", flush=True)
            if errors >= args.max_errors:
                print(f"{errors} failed passes in a row: stopping. Fix the error, then restart.")
                write_status(state="stopped: errors", mode=mode, passes=passes, last_exit_code=code)
                return 1
            if args.max_passes and passes >= args.max_passes:
                break
            if sleep_with_kill(args.interval):
                print("KILL file found: stopping.")
                write_status(state="stopped: kill switch", mode=mode, passes=passes)
                return 0
    except KeyboardInterrupt:
        print("Stopped by user.")
    write_status(state="stopped", mode=mode, passes=passes)
    return 0


if __name__ == "__main__":
    sys.exit(run())
