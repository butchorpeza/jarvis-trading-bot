"""Market-drop guard: watches live prices and, when your markets fall fast, (1) pauses NEW BUYS and
(2) optionally protects or closes open longs. Shorts are not blocked (a falling market suits them).
It runs inside live_feed.py (one look per second) and writes data/market_halt.json, which the risk manager reads.
Honest limit: it reacts AFTER a fall has started. It cannot predict a drop, and a fast gap can still jump past a stop."""
import json
import math
import os
import time
from collections import deque

HALT_FILE = "data/market_halt.json"


def halt_active(path: str = HALT_FILE, now: float | None = None) -> dict | None:
    """Returns the halt record if a drop halt is currently in force, else None."""
    try:
        d = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if d.get("until", 0) > (now or time.time()) else None


class DropGuard:
    def __init__(self, drop_1m_pct: float = 0.15, drop_5m_pct: float = 0.30, halt_minutes: int = 15, path: str = HALT_FILE):
        self.p1, self.p5, self.minutes, self.path = drop_1m_pct, drop_5m_pct, halt_minutes, path
        self.hist: dict[str, deque] = {}
        self.until, self.reason, self.level, self.symbols, self.since = 0.0, "", "", [], 0.0

    def _pct(self, dq: deque, now: float, window: int) -> float | None:
        """% change over `window` seconds, only if we really have that much history."""
        if not dq or now - dq[0][0] < window * 0.75:
            return None
        base = next((p for t, p in dq if t >= now - window), dq[0][1])
        return (dq[-1][1] / base - 1) * 100 if base else None

    def update(self, quotes: list[dict], now: float | None = None) -> dict:
        now = now or time.time()
        flagged, worst = [], 0.0
        for q in quotes:
            mid = (q["bid"] + q["ask"]) / 2
            dq = self.hist.setdefault(q["symbol"], deque())
            dq.append((now, mid))
            while dq and now - dq[0][0] > 600:
                dq.popleft()
            c1, c5 = self._pct(dq, now, 60), self._pct(dq, now, 300)
            hit1 = c1 is not None and c1 <= -self.p1
            hit5 = c5 is not None and c5 <= -self.p5
            if hit1 or hit5:
                flagged.append(q["symbol"])
                worst = max(worst, (-c1 / self.p1) if hit1 else 0, (-c5 / self.p5) if hit5 else 0)
        n = max(len(quotes), 1)
        broad = len(flagged) >= max(2, math.ceil(n / 2)) if n >= 2 else bool(flagged)
        severe = worst >= 2.0
        if (broad or severe) and flagged:
            if now >= self.until:
                self.since = now
            self.until = now + self.minutes * 60
            self.level = "severe" if severe else "broad"
            self.symbols = flagged
            self.reason = f"{', '.join(flagged)} falling fast ({'one market fell more than twice the limit' if severe else 'several markets falling together'})"
        state = {"active": now < self.until, "level": self.level if now < self.until else "", "reason": self.reason if now < self.until else "",
                 "symbols": self.symbols if now < self.until else [], "since": self.since, "until": self.until if now < self.until else 0,
                 "updated": now, "limits": {"drop_1m_pct": self.p1, "drop_5m_pct": self.p5, "halt_minutes": self.minutes}}
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        json.dump(state, open(tmp, "w"))
        os.replace(tmp, self.path)
        return state
