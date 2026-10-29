import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.config import Settings  # noqa: E402
from jarvis.manager import PositionManager  # noqa: E402
from jarvis.models import Analysis, Candidate  # noqa: E402


class FakeMT5:
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    TRADE_ACTION_SLTP, TRADE_ACTION_DEAL = 6, 1
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    TRADE_RETCODE_DONE = 10009
    ORDER_TIME_GTC = 0

    def __init__(self, pos):
        self.pos, self.sent = pos, []

    def positions_get(self):
        return [self.pos]

    def symbol_info(self, s):
        return SimpleNamespace(volume_min=0.01, volume_step=0.01, point=0.01, digits=2, trade_stops_level=0, spread=10,
                               trade_contract_size=100.0, filling_mode=2)

    def symbol_info_tick(self, s):
        return SimpleNamespace(bid=self.pos.price_current - 0.05, ask=self.pos.price_current + 0.05)

    def order_send(self, req):
        self.sent.append(req)
        return SimpleNamespace(retcode=10009, comment="")


def make(price_now, side=0, sl=95.0, volume=1.0, entry=100.0, tmp_path=None):
    pos = SimpleNamespace(ticket=1, symbol="X", type=side, volume=volume, price_open=entry, price_current=price_now,
                          sl=sl, tp=(300.0 if side == 0 else 1.0), magic=424242)
    mt5 = FakeMT5(pos)
    ex = SimpleNamespace(mt5=mt5, _filling=lambda info: 2)
    return pos, mt5, ex


def cand(atr=2.0, close=110.0, o=109.0, h=111.0, l=108.0, ema50=100.0):
    return Candidate(symbol="X", last_close=close, last_bar_time=datetime.now(timezone.utc), avg_volume=1e6, rel_volume=1.0,
                     atr=atr, ema20=105.0, ema50=ema50, rsi=55.0, volatility_pct=2.0, last_open=o, last_high=h, last_low=l)


def ana(trend="UP", decision="BUY"):
    return Analysis(symbol="X", trend=trend, volume_confirms=True, catalyst="", decision=decision, confidence="HIGH", reason="t")


def run(price_now, tmp_path, dry=False, trend="UP", decision="BUY", c=None, side=0, sl=95.0, volume=1.0, cfg=None):
    pos, mt5, ex = make(price_now, side=side, sl=sl, volume=volume)
    cfg = cfg or Settings()
    mgr = PositionManager(ex, cfg, dry_run=dry, state_path=str(tmp_path / "ps.json"))
    out = mgr.run({"X": c or cand(close=price_now)}, {"X": ana(trend, decision)}, [], 100_000)
    return out, mt5, pos


def kinds(mt5):
    return [("SLTP" if r["action"] == 6 else "DEAL", r.get("sl"), r.get("volume")) for r in mt5.sent]


def test_nothing_happens_below_one_r(tmp_path):
    out, mt5, _ = run(103.0, tmp_path)               # +0.6R
    assert mt5.sent == []


def test_breakeven_and_partial_at_one_r(tmp_path):
    out, mt5, _ = run(105.0, tmp_path)               # +1R: risk was 5
    acts = kinds(mt5)
    assert ("DEAL", None, 0.5) in acts                # half closed
    be = [a for a in acts if a[0] == "SLTP"]
    assert be and be[0][1] >= 100.0                   # stop moved to about entry (+ spread)


def test_trailing_ratchets_only_forward(tmp_path):
    out, mt5, _ = run(110.0, tmp_path, sl=104.0)      # +2R, trail = 110 - 2*2 = 106 > 104
    assert any(a[0] == "SLTP" and a[1] == 106.0 for a in kinds(mt5))
    out2, mt5b, _ = run(110.0, tmp_path, sl=108.0)    # existing stop already tighter: must not loosen
    assert not any(a[0] == "SLTP" and a[1] < 108.0 for a in kinds(mt5b))


def test_scale_in_only_on_protected_winner(tmp_path):
    out, mt5, _ = run(108.0, tmp_path, sl=101.0)      # +1.6R, stop above entry (protected), trend intact
    assert any(a[0] == "DEAL" and a[2] and a[2] > 0 and r["type"] == 0 for a, r in zip(kinds(mt5), mt5.sent))
    out2, mt5b, _ = run(108.0, tmp_path, sl=95.0)     # stop still below entry: not protected, no add
    assert not any(r["action"] == 1 and r["comment"] == "jarvis-add" for r in mt5b.sent)


def test_never_adds_to_a_loser(tmp_path):
    out, mt5, _ = run(97.0, tmp_path, sl=95.0)        # -0.6R
    assert not any(r.get("comment") == "jarvis-add" for r in mt5.sent)


def test_trend_flip_closes_everything(tmp_path):
    out, mt5, pos = run(104.0, tmp_path, trend="DOWN", decision="AVOID")
    assert len(mt5.sent) == 1 and mt5.sent[0]["volume"] == pos.volume and mt5.sent[0]["type"] == 1   # sell to close a long


def test_adverse_spike_closes(tmp_path):
    spike = cand(atr=2.0, close=101.0, o=108.0, h=109.0, l=100.0)   # range 9 >= 3 x ATR, bar falling
    out, mt5, _ = run(101.0, tmp_path, c=spike)
    assert len(mt5.sent) == 1 and "spike" in out[0]


def test_dry_run_sends_nothing(tmp_path):
    out, mt5, _ = run(110.0, tmp_path, dry=True)
    assert mt5.sent == [] and any(line.startswith("WOULD") for line in out)


def test_short_position_trails_downward(tmp_path):
    pos, mt5, ex = make(90.0, side=1, sl=105.0, entry=100.0)         # short, risk 5, now +2R
    mgr = PositionManager(ex, Settings(), dry_run=False, state_path=str(tmp_path / "ps.json"))
    mgr.run({"X": cand(close=90.0, o=91.0, h=92.0, l=89.0)}, {"X": ana("DOWN", "SELL")}, [], 100_000)
    stops = [r["sl"] for r in mt5.sent if r["action"] == 6]
    assert stops and all(s < 105.0 for s in stops) and min(stops) == 94.0   # 90 + 2 x ATR


def test_bank_profit_closes_winners_at_daily_target(tmp_path):
    pos, mt5, ex = make(104.0, side=0, sl=95.0)
    pos.profit = 40.0
    mgr = PositionManager(ex, Settings(), dry_run=False, state_path=str(tmp_path / "ps.json"))
    out = mgr.run({"X": cand(close=104.0)}, {"X": ana()}, [], equity=1021.0, day_start=1000.0)   # +2.1% on the day
    assert any("DAILY PROFIT TARGET" in line for line in out)
    assert len(mt5.sent) == 1 and mt5.sent[0]["volume"] == pos.volume and mt5.sent[0]["type"] == 1   # closed the whole winner


def test_bank_profit_leaves_losers_alone(tmp_path):
    pos, mt5, ex = make(98.0, side=0, sl=95.0)
    pos.profit = -20.0
    mgr = PositionManager(ex, Settings(), dry_run=False, state_path=str(tmp_path / "ps.json"))
    mgr.run({"X": cand(close=98.0)}, {"X": ana()}, [], equity=1021.0, day_start=1000.0)
    assert not any(r["action"] == 1 and r.get("comment") == "jarvis-manage" for r in mt5.sent)


def test_protects_a_position_with_no_stop_or_target(tmp_path):
    pos, mt5, ex = make(100.0, side=0, sl=0.0)           # opened by hand: no stop, no target
    pos.tp, pos.magic = 0.0, 0                            # not a JARVIS trade
    mgr = PositionManager(ex, Settings(), dry_run=False, state_path=str(tmp_path / "ps.json"))
    out = mgr.run({"X": cand(close=100.0, atr=2.0)}, {"X": ana()}, [], 100_000)
    prot = [r for r in mt5.sent if r["action"] == 6]
    assert prot and prot[0]["sl"] == 97.0 and prot[0]["tp"] == 105.0   # 1.5 ATR below, 2.5 ATR above
    assert any("protected" in line for line in out)
