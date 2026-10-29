"""Real economic calendar (this week) and a news blackout: no new BUY shortly before/after a high-impact event
for the symbol's currencies. Feed: faireconomy.media weekly JSON (unofficial, free, no key).
If the feed is unreachable the blackout is skipped and the Analyst notes it as missing data."""
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone

FEED = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CACHE = "data/events_cache.json"
TTL = 1800
CCY = {"USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "CNY"}


def currencies_for(symbol: str) -> set[str]:
    up = re.sub(r"[^A-Z]", "", symbol.upper())
    out: set[str] = set()
    if any(k in up for k in ("XAU", "GOLD", "XAG", "NAS", "USTEC", "US100", "DJ", "US30", "SPX", "US500", "DOW", "OIL", "WTI")):
        out.add("USD")
    for i in range(len(up) - 2):
        if up[i:i + 3] in CCY:
            out.add(up[i:i + 3])
    return out


def fetch_events(url: str = FEED, cache_path: str = CACHE, timeout: float = 8.0) -> list[dict]:
    """Returns events [{title, country, time (UTC datetime), impact, forecast, previous}] or [] on failure."""
    raw = None
    try:
        if os.path.exists(cache_path) and time.time() - os.path.getmtime(cache_path) < TTL:
            raw = json.load(open(cache_path, encoding="utf-8"))
        else:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = json.loads(r.read(2_000_000).decode("utf-8"))
            os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
            json.dump(raw, open(cache_path, "w", encoding="utf-8"))
    except Exception:
        try:
            raw = json.load(open(cache_path, encoding="utf-8"))
        except Exception:
            return []
    events = []
    for e in raw if isinstance(raw, list) else []:
        try:
            t = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
            events.append({"title": e.get("title", ""), "country": e.get("country", ""), "time": t,
                           "impact": e.get("impact", ""), "forecast": e.get("forecast", ""), "previous": e.get("previous", "")})
        except Exception:
            continue
    return sorted(events, key=lambda x: x["time"])


def blackout(symbol: str, events: list[dict], now: datetime | None = None,
             before_hours: float = 2.0, after_hours: float = 1.0) -> str | None:
    """Returns the reason string if a high-impact event for this symbol's currencies is within the window."""
    now = now or datetime.now(timezone.utc)
    ccys = currencies_for(symbol)
    for e in events:
        if e["impact"] != "High" or e["country"] not in ccys:
            continue
        if e["time"] - timedelta(hours=before_hours) <= now <= e["time"] + timedelta(hours=after_hours):
            return f"high-impact event {e['country']} {e['title']} at {e['time']:%a %H:%M} UTC"
    return None


def apply_blackout(a, reason: str):
    """Veto-only, like the other filters: turns a BUY into HOLD."""
    if a.decision in ("BUY", "SELL"):
        a.decision = "HOLD"
        a.red_flags.append("news blackout")
        a.reason = f"News blackout: {reason} | {a.reason}"
        a.entry_price = a.stop_price = a.target_price = None
    return a
