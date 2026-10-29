import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import live_feed  # noqa: E402


class FakeMT5:
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    TRADE_RETCODE_DONE = 10009

    def __init__(self):
        self.pos = SimpleNamespace(ticket=7, symbol="X", type=0, volume=0.5, price_open=100.0, price_current=104.0,
                                   sl=95.0, tp=110.0, profit=12.5, magic=424242)
        self.sent = []

    def positions_get(self):
        return [self.pos]

    def symbol_info(self, s):
        return SimpleNamespace(digits=2, point=0.01, spread=10, trade_stops_level=0)

    def symbol_info_tick(self, s):
        return SimpleNamespace(bid=103.95, ask=104.05)

    def order_send(self, req):
        self.sent.append(req)
        return SimpleNamespace(retcode=10009, comment="")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("data/commands")
    return FakeMT5(), SimpleNamespace(_filling=lambda info: 2)


def put(cid, **cmd):
    cmd.setdefault("ts", time.time())
    json.dump(cmd, open(f"data/commands/{cid}.json", "w"))


def result(cid):
    return json.load(open(f"data/command_results/{cid}.json"))


def test_close_sends_opposite_deal_for_that_position(env):
    mt5, ex = env
    put("a1", type="close", ticket=7)
    live_feed.handle_commands(mt5, ex)
    req = mt5.sent[0]
    assert req["action"] == 1 and req["type"] == 1 and req["position"] == 7 and req["volume"] == 0.5 and req["price"] == 103.95
    assert result("a1")["ok"] and not os.listdir("data/commands")


def test_unknown_ticket_and_stale_command_do_nothing(env):
    mt5, ex = env
    put("b1", type="close", ticket=999)
    put("b2", type="close", ticket=7, ts=time.time() - 600)
    live_feed.handle_commands(mt5, ex)
    assert mt5.sent == []
    assert not result("b1")["ok"] and not result("b2")["ok"]


def test_set_tp_validates_side_and_keeps_stop(env):
    mt5, ex = env
    put("c1", type="set_tp", ticket=7, price=103.0)      # below price: wrong side for a long
    put("c2", type="set_tp", ticket=7, price=115.0)
    live_feed.handle_commands(mt5, ex)
    assert not result("c1")["ok"]
    assert result("c2")["ok"]
    assert len(mt5.sent) == 1 and mt5.sent[0]["action"] == 6 and mt5.sent[0]["tp"] == 115.0 and mt5.sent[0]["sl"] == 95.0


def test_only_two_command_types_exist(env):
    mt5, ex = env
    put("d1", type="withdraw", ticket=7)
    live_feed.handle_commands(mt5, ex)
    assert mt5.sent == [] and not result("d1")["ok"]


def test_set_sl_validates_side(env):
    mt5, ex = env
    put("e1", type="set_sl", ticket=7, price=106.0)      # above price: wrong side for a long
    put("e2", type="set_sl", ticket=7, price=101.0)
    live_feed.handle_commands(mt5, ex)
    assert not result("e1")["ok"] and result("e2")["ok"]
    assert len(mt5.sent) == 1 and mt5.sent[0]["sl"] == 101.0 and mt5.sent[0]["tp"] == 110.0


def _cfg_with_state(tmp_path, day_start):
    from datetime import datetime, timezone
    from jarvis.config import Settings
    state = tmp_path / "state.json"
    json.dump({"day": datetime.now(timezone.utc).date().isoformat(), "day_start": day_start, "peak_equity": day_start}, open(state, "w"))
    return Settings(state_path=str(state))


def test_bank_if_target_closes_only_winners(env, tmp_path):
    mt5, ex = env
    cfg = _cfg_with_state(tmp_path, 1000.0)
    mt5.account_info = lambda: SimpleNamespace(equity=1021.0)
    out = live_feed.bank_if_target(mt5, ex, cfg)
    assert len(out) == 1 and mt5.sent[0]["position"] == 7 and mt5.sent[0]["comment"] == "bank-profit"
    mt5.sent.clear()
    mt5.pos.profit = -5.0
    assert live_feed.bank_if_target(mt5, ex, cfg) == [] and mt5.sent == []


def test_bank_if_target_waits_below_target_or_stale_state(env, tmp_path):
    mt5, ex = env
    mt5.account_info = lambda: SimpleNamespace(equity=1010.0)       # +1.0%, target is 2%
    assert live_feed.bank_if_target(mt5, ex, _cfg_with_state(tmp_path, 1000.0)) == []
    mt5.account_info = lambda: SimpleNamespace(equity=1100.0)
    stale = _cfg_with_state(tmp_path, 1000.0)
    json.dump({"day": "2000-01-01", "day_start": 1000.0}, open(stale.state_path, "w"))
    assert live_feed.bank_if_target(mt5, ex, stale) == [] and mt5.sent == []
