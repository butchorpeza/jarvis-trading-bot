"""Scanner: fetch recent daily bars for a small watchlist and turn them into Candidates."""
from datetime import datetime, timedelta, timezone
from typing import Protocol

import numpy as np
import pandas as pd

from .config import Settings
from .models import Candidate


class DataProvider(Protocol):
    def daily_bars(self, symbols: list[str], days: int) -> dict[str, pd.DataFrame]: ...


class SyntheticData:
    """Random-walk bars for pipeline demos/tests only. NOT real prices."""

    def __init__(self, seed: int = 1, end: datetime | None = None):
        self.seed, self.end = seed, end or datetime.now(timezone.utc)

    def daily_bars(self, symbols, days=120):
        out = {}
        for i, s in enumerate(symbols):
            rng = np.random.default_rng(self.seed + i)
            idx = pd.bdate_range(end=self.end.date(), periods=80, tz="UTC")
            close = 100 * np.exp(np.cumsum(rng.normal(0.001, 0.012, len(idx))))
            spread = close * 0.008
            out[s] = pd.DataFrame({"open": close, "high": close + spread, "low": close - spread,
                                   "close": close, "volume": rng.integers(2_000_000, 6_000_000, len(idx)).astype(float)},
                                  index=idx)
        return out


def _rsi(c: pd.Series, n: int = 14) -> float:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return float(100 - 100 / (1 + up.iloc[-1] / dn.iloc[-1])) if dn.iloc[-1] else 100.0


def build_candidate(symbol: str, df: pd.DataFrame, cfg: Settings, now: datetime | None = None) -> Candidate:
    now = now or df.attrs.get("server_now") or datetime.now(timezone.utc)
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = float(tr.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1])
    last_time = pd.Timestamp(df.index[-1]).to_pydatetime()
    if last_time.tzinfo is None:
        last_time = last_time.replace(tzinfo=timezone.utc)
    from .config import TF_SECONDS
    age_sec = (now - last_time).total_seconds()
    if cfg.timeframe == "D1":
        age = int(age_sec // 86400)
        stale = age > cfg.max_data_age_days
        age_txt = f"{age} days"
    else:   # intraday: the last closed candle must be recent (market closed / feed stalled => stale)
        stale = age_sec > 6 * TF_SECONDS[cfg.timeframe]
        age_txt = f"{int(age_sec // 60)} minutes"
    avg_vol = float(v.tail(20).mean())
    return Candidate(
        symbol=symbol, last_close=float(c.iloc[-1]), last_bar_time=last_time, avg_volume=avg_vol,
        rel_volume=float(v.iloc[-1] / avg_vol) if avg_vol else 0.0, atr=atr,
        ema20=float(c.ewm(span=20, adjust=False).mean().iloc[-1]),
        ema50=float(c.ewm(span=50, adjust=False).mean().iloc[-1]),
        rsi=_rsi(c), volatility_pct=atr / float(c.iloc[-1]) * 100,
        last_open=float(df["open"].iloc[-1]), last_high=float(h.iloc[-1]), last_low=float(l.iloc[-1]),
        stale=stale, stale_reason=f"last bar is {age_txt} old" if stale else "")


def scan(symbols: list[str], provider: DataProvider, cfg: Settings, now: datetime | None = None):
    """Returns (candidates, errors). Symbols with missing/short data go to errors, never silently dropped."""
    bars = provider.daily_bars(symbols, 120)
    cands, errors = [], {}
    for s in symbols:
        df = bars.get(s)
        if df is None or len(df) < 55:
            errors[s] = "missing or insufficient data (need >= 55 daily bars)"
            continue
        cands.append(build_candidate(s, df, cfg, now))
    return cands, errors
