"""Journal: append every decision, rejection, submission and error to logs/trades.csv."""
import csv
import os
from datetime import datetime, timezone

COLUMNS = ["timestamp", "symbol", "decision", "entry_price", "quantity", "stop_price", "target_price",
           "risk_dollars", "confidence", "reason", "status", "exit_price", "pnl", "notes"]


class Journal:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(COLUMNS)

    def record(self, symbol: str, decision: str, status: str, reason: str = "", confidence: str = "",
               entry_price=None, quantity=0, stop_price=None, target_price=None, risk_dollars=0.0, notes: str = ""):
        row = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"), "symbol": symbol,
               "decision": decision, "entry_price": entry_price, "quantity": quantity, "stop_price": stop_price,
               "target_price": target_price, "risk_dollars": risk_dollars, "confidence": confidence,
               "reason": reason, "status": status, "exit_price": "", "pnl": "", "notes": notes}
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=COLUMNS).writerow(row)

    def submitted_since(self, seconds: int) -> set[str]:
        """Symbols with a SUBMITTED order in the last `seconds` (cooldown / duplicate guard)."""
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        out = set()
        with open(self.path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["status"] == "SUBMITTED":
                    try:
                        if datetime.fromisoformat(r["timestamp"]) >= cutoff:
                            out.add(r["symbol"])
                    except ValueError:
                        continue
        return out

    def trades_today(self) -> dict[str, int]:
        today = datetime.now(timezone.utc).date().isoformat()
        out: dict[str, int] = {}
        with open(self.path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["status"] == "SUBMITTED" and r["timestamp"].startswith(today):
                    out[r["symbol"]] = out.get(r["symbol"], 0) + 1
        return out

    def submitted_today(self) -> set[str]:
        today = datetime.now(timezone.utc).date().isoformat()
        out = set()
        with open(self.path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["status"] == "SUBMITTED" and r["timestamp"].startswith(today):
                    out.add(r["symbol"])
        return out
