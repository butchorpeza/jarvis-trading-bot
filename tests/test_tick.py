import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tick_trader as tt  # noqa: E402
from jarvis.models import Analysis  # noqa: E402


class FakeMT5:
    COPY_TICKS_ALL = -1

    def __init__(self, ticks, now):
        self._t, self._now = ticks, now

    def symbol_info_tick(self, s):
        return SimpleNamespace(time=self._now)

    def copy_ticks_from(self, s, frm, n, flags):
        return self._t


def make_ticks(start, seconds, price=100.0):
    rows = []
    for k in range(seconds):
        for j in range(3):                                    # 3 ticks per second
            rows.append((start + k, price + 0.01 * ((k + j) % 5), 0.0, 0))
    return np.array(rows, dtype=[("time", "i8"), ("bid", "f8"), ("ask", "f8"), ("flags", "i8")])


def test_second_bars_are_closed_and_ohlc():
    start = 1_700_000_000
    mt5 = FakeMT5(make_ticks(start, 120), now=start + 119)
    df = tt.second_bars(mt5, "X")
    assert len(df) == 119                                      # last (in-progress) second dropped
    assert (df["high"] >= df["low"]).all() and (df["volume"] == 3).all()
    assert df.attrs["server_now"] == datetime.fromtimestamp(start + 119, tz=timezone.utc)


def test_second_bars_none_when_too_few_ticks():
    assert tt.second_bars(FakeMT5(make_ticks(1_700_000_000, 2)[:5], now=1_700_000_002), "X") is None


def test_floors_keep_the_stop_and_target_a_few_spreads_away():
    a = Analysis(symbol="X", trend="UP", volume_confirms=True, catalyst="", decision="BUY", confidence="LOW", reason="t",
                 entry_price=100.0, stop_price=99.99, target_price=100.02)
    a = tt.apply_floors(a, spread=0.5, cfg=None)
    assert round(100.0 - a.stop_price, 6) == 4.0 and round(a.target_price - 100.0, 6) == 6.0   # 8 and 12 spreads
    s = Analysis(symbol="X", trend="DOWN", volume_confirms=True, catalyst="", decision="SELL", confidence="LOW", reason="t",
                 entry_price=100.0, stop_price=100.01, target_price=99.99)
    s = tt.apply_floors(s, spread=0.5, cfg=None)
    assert s.stop_price > 100.0 > s.target_price and round(s.stop_price - 100.0, 6) == 4.0
