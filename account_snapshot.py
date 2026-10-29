"""Refresh the dashboard's account view without running a trading pass. Read-only: places no orders."""
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()
from jarvis.config import load_settings
from jarvis.mt5_backend import MT5Executor

try:
    snap = MT5Executor(load_settings(), dry_run=True).account_snapshot()
except Exception as e:  # noqa: BLE001
    sys.exit(f"ERROR: {type(e).__name__}: {e}")
os.makedirs("data", exist_ok=True)
json.dump(snap, open("data/account_snapshot.json", "w"))
print(f"{snap['server']} demo={snap['demo']} balance={snap['balance']} equity={snap['equity']} open positions={len(snap['positions'])}")
