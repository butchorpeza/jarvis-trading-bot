import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import live_feed  # noqa: E402
from jarvis.config import Settings  # noqa: E402
from jarvis.dropguard import DropGuard  # noqa: E402
from jarvis.models import Analysis  # noqa: E402
from jarvis.risk_manager import RiskManager  # noqa: E402
from tests.test_jarvis import buy_analysis, cand  # noqa: E402


def feed(guard, series, start=1000.0, step=5, steps=70):
    """series: {symbol: function(i) -> price}; returns the last state."""
    st = None
    for i in range(steps):
        t = start + i * step
        quotes = [{"symbol": s, "bid": f(i), "ask": f(i) + 0.01} for s, f in series.items()]
        st = guard.update(quotes, now=t)
    return st


@pytest.fixture
def guard(tmp_path):
    return DropGuard(0.15, 0.30, 15, path=str(tmp_path / "halt.json"))


def test_calm_market_does_not_trigger(guard):
    st = feed(guard, {"A": lambda i: 100 + 0.001 * (i % 3), "B": lambda i: 200 + 0.002 * (i % 3), "C": lambda i: 50.0})
    assert not st["active"]


def test_broad_fall_triggers_halt(guard):
    def fall(p0):
        return lambda i: p0 * (1 - 0.0004 * i)             # about -2.7% over 70 steps
    st = feed(guard, {"A": fall(100), "B": fall(200), "C": lambda i: 50.0})
    assert st["active"] and st["level"] in ("broad", "severe") and len(st["symbols"]) >= 2


def test_single_market_small_fall_ignored_but_huge_one_triggers(guard):
    st = feed(guard, {"A": lambda i: 100 * (1 - 0.0016 * i / 14), "B": lambda i: 200.0, "C": lambda i: 50.0}, steps=14)
    assert not st["active"]
    g2 = DropGuard(0.15, 0.30, 15, path=guard.path)
    st2 = feed(g2, {"A": lambda i: 100 * (1 - 0.01 * i / 14), "B": lambda i: 200.0, "C": lambda i: 50.0}, steps=14)
    assert st2["active"] and st2["level"] == "severe"


def test_halt_expires(guard):
    def fall(p0):
        return lambda i: p0 * (1 - 0.0004 * i)
    feed(guard, {"A": fall(100), "B": fall(200)})
    later = 1000 + 70 * 5 + 16 * 60
    st = guard.update([{"symbol": "A", "bid": 90, "ask": 90.01}, {"symbol": "B", "bid": 180, "ask": 180.01}], now=later)
    assert st["active"] is False


def test_halt_blocks_buys_not_sells(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("data")
    json.dump({"active": True, "until": time.time() + 600, "reason": "test fall"}, open("data/market_halt.json", "w"))
    cfg = Settings(state_path=str(tmp_path / "s.json"))
    rm = RiskManager(cfg)
    buy = rm.check(buy_analysis(), cand(cfg), 100_000, set(), set(), persist_state=False)
    assert not buy.approved and any("drop guard" in r for r in buy.reasons)
    sell = Analysis(symbol="AAA", trend="DOWN", volume_confirms=True, catalyst="", decision="SELL", confidence="HIGH", reason="t",
                    entry_price=100.0, stop_price=102.0, target_price=96.0)
    assert rm.check(sell, cand(cfg), 100_000, set(), set(), persist_state=False).approved


class FakeMT5:
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
    ORDER_TYPE_SELL = 1
    TRADE_RETCODE_DONE = 10009

    def __init__(self):
        def mk(t, sym, profit, sl):
            return SimpleNamespace(ticket=t, symbol=sym, type=0, volume=0.1, price_open=100.0, price_current=103.0,
                                   sl=sl, tp=110.0, profit=profit, magic=424242)
        self.positions = [mk(1, "W", 30.0, 95.0), mk(2, "L", -20.0, 95.0),
                          SimpleNamespace(ticket=3, symbol="S", type=1, volume=0.1, price_open=100.0, price_current=97.0, sl=105.0,
                                          tp=90.0, profit=30.0, magic=424242)]
        self.sent = []

    def positions_get(self):
        return self.positions

    def symbol_info(self, s):
        return SimpleNamespace(digits=2, point=0.01, spread=10, trade_stops_level=0)

    def symbol_info_tick(self, s):
        return SimpleNamespace(bid=102.95, ask=103.05)

    def order_send(self, r):
        self.sent.append(r)
        return SimpleNamespace(retcode=10009, comment="")


def test_protect_moves_only_winning_longs_to_breakeven():
    mt5, ex = FakeMT5(), SimpleNamespace(_filling=lambda i: 2)
    out = live_feed.drop_actions(mt5, ex, Settings(drop_action="protect"), {"active": True}, {})
    assert len(mt5.sent) == 1 and mt5.sent[0]["action"] == 6 and mt5.sent[0]["position"] == 1 and mt5.sent[0]["sl"] == 100.1
    assert out and all(r["position"] != 3 for r in mt5.sent)               # the short is untouched


def test_close_longs_closes_longs_only_and_pause_does_nothing():
    mt5, ex = FakeMT5(), SimpleNamespace(_filling=lambda i: 2)
    live_feed.drop_actions(mt5, ex, Settings(drop_action="close_longs"), {"active": True}, {})
    assert sorted(r["position"] for r in mt5.sent) == [1, 2] and all(r["action"] == 1 for r in mt5.sent)
    mt5b = FakeMT5()
    assert live_feed.drop_actions(mt5b, ex, Settings(drop_action="pause"), {"active": True}, {}) == [] and mt5b.sent == []
    assert live_feed.drop_actions(mt5b, ex, Settings(drop_action="protect"), {"active": False}, {}) == []
