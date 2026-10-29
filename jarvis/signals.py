"""Market signal from the LOCAL AI-Trader server (127.0.0.1 only). Advisory input for the Analyst.
It can veto or downgrade a BUY, never create one, and never touches the Risk Manager."""
import json
import urllib.request

LOCAL_URL = "http://127.0.0.1:8000/api/market-intel/macro-signals"
VALID = {"bullish", "neutral", "defensive"}


def fetch_macro_signal(url: str = LOCAL_URL, timeout: float = 4.0) -> dict:
    """Returns {"verdict": bullish|neutral|defensive|unavailable, "bullish": n, "total": n, "note": str}."""
    if not url.startswith("http://127.0.0.1"):
        raise ValueError("Signal source must be the local AI-Trader server (127.0.0.1).")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # server down, bad JSON, etc.: fail open to 'unavailable', never to 'bullish'
        return {"verdict": "unavailable", "bullish": 0, "total": 0, "note": f"local signal server error: {type(e).__name__}"}
    verdict = data.get("verdict") if data.get("available") else "unavailable"
    if verdict not in VALID:
        return {"verdict": "unavailable", "bullish": 0, "total": 0,
                "note": "market signal not available yet (needs Alpha Vantage key / first refresh)"}
    return {"verdict": verdict, "bullish": int(data.get("bullish_count", 0)), "total": int(data.get("total_count", 0)),
            "note": f"macro verdict {verdict} ({data.get('bullish_count', 0)}/{data.get('total_count', 0)} bullish)"}
