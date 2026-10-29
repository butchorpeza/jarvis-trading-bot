import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.analyst import analyze  # noqa: E402
from jarvis.config import PaperModeError, Settings  # noqa: E402
from jarvis.models import Analysis, TradePlan  # noqa: E402
from jarvis.risk_manager import RiskManager  # noqa: E402
from jarvis.scanner import SyntheticData, build_candidate, scan  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    return Settings(state_path=str(tmp_path / "state.json"), log_path=str(tmp_path / "trades.csv"))


def buy_analysis(entry=100.0, stop=98.0, symbol="AAA"):
    return Analysis(symbol=symbol, trend="UP", volume_confirms=True, catalyst="none", decision="BUY",
                    confidence="HIGH", reason="t", entry_price=entry, stop_price=stop, target_price=105.0)


def cand(cfg, symbol="AAA", end=None):
    df = SyntheticData(end=end).daily_bars([symbol])[symbol]
    return build_candidate(symbol, df, cfg)


# --- risk limits block invalid plans ---
def test_position_sizing_respects_limits(cfg):
    rd = RiskManager(cfg).check(buy_analysis(), cand(cfg), 100_000, set(), set(), persist_state=False)
    assert rd.approved
    assert rd.risk_dollars <= 100_000 * cfg.max_risk_per_trade + 1e-6
    assert rd.quantity * 100.0 <= 100_000 * cfg.max_position_pct


@pytest.mark.parametrize("entry,stop", [(100.0, 100.0), (100.0, 101.0)])
def test_invalid_stop_blocked(cfg, entry, stop):
    rd = RiskManager(cfg).check(buy_analysis(entry, stop), cand(cfg), 100_000, set(), set(), persist_state=False)
    assert not rd.approved


def test_max_positions_and_no_averaging_down(cfg):
    rm = RiskManager(cfg)
    assert not rm.check(buy_analysis(), cand(cfg), 100_000, {"A", "B", "C", "D", "E"}, set(), persist_state=False).approved
    assert not rm.check(buy_analysis(), cand(cfg), 100_000, {"AAA"}, set(), persist_state=False).approved


def test_duplicate_blocked_by_risk(cfg):
    assert not RiskManager(cfg).check(buy_analysis(), cand(cfg), 100_000, set(), {"AAA"}, persist_state=False).approved


def test_drawdown_pause(cfg):
    rm = RiskManager(cfg)
    assert rm.check(buy_analysis(), cand(cfg), 100_000, set(), set()).approved   # sets peak 100k
    rd = rm.check(buy_analysis(), cand(cfg), 94_000, set(), set())               # -6% from peak
    assert not rd.approved and any("drawdown" in r for r in rd.reasons)


# --- stale data rejected ---
def test_stale_data_flagged_and_blocked(cfg):
    old = datetime.now(timezone.utc) - timedelta(days=30)
    c = cand(cfg, end=old)
    assert c.stale
    a = analyze(c, cfg)
    assert a.decision != "BUY"
    assert not RiskManager(cfg).check(buy_analysis(), c, 100_000, set(), set(), persist_state=False).approved


def test_missing_data_reported_not_dropped(cfg):
    class Empty:
        def daily_bars(self, symbols, days):
            return {}
    cands, errors = scan(["AAA"], Empty(), cfg)
    assert not cands and "AAA" in errors


def plan(approved=True):
    return TradePlan(symbol="AAA", decision="BUY", entry_price=100, quantity=10, stop_price=98,
                     target_price=105, confidence="HIGH", reason="t", risk_approved=approved)


# --- MT5 backend: demo only ---
class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_REAL = 0, 2
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    trade_mode = 0
    sent = []

    class _Info:
        trade_contract_size, volume_step, volume_min, volume_max, digits, filling_mode = 100000, 0.01, 0.01, 50, 5, 2

    def initialize(self): return True
    def shutdown(self): pass
    def last_error(self): return (0, "")
    def account_info(self):
        return type("A", (), {"trade_mode": self.trade_mode, "equity": 100000.0})()
    def symbol_info(self, s): return self._Info()
    def order_send(self, req):
        self.sent.append(req)


@pytest.fixture
def fake_mt5(monkeypatch):
    m = FakeMT5()
    m.sent = []
    monkeypatch.setitem(sys.modules, "MetaTrader5", m)
    monkeypatch.setenv("JARVIS_MT5_DEMO", "true")
    return m


def test_mt5_requires_demo_flag(monkeypatch, cfg):
    from jarvis.mt5_backend import MT5Executor
    monkeypatch.delenv("JARVIS_MT5_DEMO", raising=False)
    with pytest.raises(PaperModeError):
        MT5Executor(cfg)


def test_mt5_refuses_live_account(fake_mt5, cfg):
    from jarvis.mt5_backend import MT5Executor
    fake_mt5.trade_mode = FakeMT5.ACCOUNT_TRADE_MODE_REAL
    with pytest.raises(RuntimeError, match="not a DEMO"):
        MT5Executor(cfg)


def test_mt5_dry_run_sends_nothing_and_sizes_lots(fake_mt5, cfg):
    from jarvis.mt5_backend import MT5Executor
    ex = MT5Executor(cfg, dry_run=True)
    assert ex.lots_for("EURUSD", 25_000) == 0.25
    big = plan().model_copy(update={"quantity": 25_000})
    assert ex.submit(big, set()) == "DRY_RUN"
    assert fake_mt5.sent == []
    with pytest.raises(ValueError):
        ex.lots_for("EURUSD", 100)   # below broker minimum lot


def test_mt5_unapproved_plan_rejected(fake_mt5, cfg):
    from jarvis.mt5_backend import MT5Executor
    with pytest.raises(ValueError):
        MT5Executor(cfg).submit(plan(approved=False).model_copy(update={"quantity": 25_000}), set())


def test_mt5_duplicate_order_rejected(fake_mt5, cfg):
    from jarvis.mt5_backend import MT5Executor
    with pytest.raises(ValueError):
        MT5Executor(cfg).submit(plan().model_copy(update={"quantity": 25_000, "symbol": "EURUSD"}), {"EURUSD"})


# --- local market signal ---
def _up_candidate(cfg):
    for seed in range(1, 60):
        c = build_candidate("AAA", SyntheticData(seed=seed).daily_bars(["AAA"])["AAA"], cfg)
        if analyze(c, cfg).decision == "BUY":
            return c
    pytest.skip("no BUY candidate found")


def test_defensive_signal_downgrades_buy(cfg):
    c = _up_candidate(cfg)
    a = analyze(c, cfg, {"verdict": "defensive", "note": "x"})
    assert a.decision == "HOLD" and "market signal is defensive" in a.red_flags


def test_bullish_or_missing_signal_never_creates_buy(cfg):
    c = _up_candidate(cfg)
    assert analyze(c, cfg, {"verdict": "bullish", "note": "x"}).decision == "BUY"
    down = build_candidate("B", SyntheticData(seed=2).daily_bars(["B"])["B"], cfg)
    base = analyze(down, cfg).decision
    assert analyze(down, cfg, {"verdict": "bullish", "note": "x"}).decision == base


def test_signal_source_must_be_local():
    from jarvis.signals import fetch_macro_signal
    with pytest.raises(ValueError):
        fetch_macro_signal("https://example.com/x")


def test_signal_server_down_is_unavailable():
    from jarvis.signals import fetch_macro_signal
    assert fetch_macro_signal("http://127.0.0.1:1/x", timeout=1)["verdict"] == "unavailable"


# --- LAYA / Claude: veto-only ---
def test_claude_veto_blocks_and_approve_changes_nothing(cfg):
    from jarvis.claude_advisor import Verdict, apply_verdict
    c = _up_candidate(cfg)
    a = analyze(c, cfg)
    a = apply_verdict(a, Verdict(action="APPROVE", confidence="HIGH", reason="ok"))
    assert a.decision == "BUY"
    a = apply_verdict(a, Verdict(action="VETO", confidence="HIGH", reason="weak"))
    assert a.decision == "HOLD" and a.entry_price is None


def test_advisors_cannot_create_a_buy(cfg):
    from jarvis.claude_advisor import Verdict, apply_verdict
    from jarvis.laya_filter import apply_filter
    down = next(c for c in (build_candidate("B", SyntheticData(seed=s).daily_bars(["B"])["B"], cfg) for s in range(1, 60))
                if analyze(c, cfg).decision != "BUY")
    a = analyze(down, cfg)
    base = a.decision
    a = apply_verdict(a, Verdict(action="APPROVE", confidence="HIGH", reason="x"))
    a = apply_filter(a, True, "LAYA P(true)=0.99")
    assert a.decision == base and base != "BUY"


def test_laya_low_probability_vetoes_and_parser(cfg):
    from jarvis.laya_filter import _p_true, apply_filter
    assert _p_true({"noul": {"true": 0.9, "false": 0.1}}) == 0.9
    assert _p_true("garbage") is None
    a = apply_filter(analyze(_up_candidate(cfg), cfg), False, "LAYA P(true)=0.20")
    assert a.decision == "HOLD"


def test_claude_advisor_fails_closed(cfg, monkeypatch):
    from jarvis.claude_advisor import ClaudeAdvisor
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    adv = ClaudeAdvisor()

    class Boom:
        def create(self, **k):
            raise RuntimeError("network down")
    adv.client = type("C", (), {"messages": Boom()})()
    c = _up_candidate(cfg)
    v = adv.review(analyze(c, cfg), c, None)
    assert v.action == "VETO"


# --- real events / blackout ---
def _ev(country, impact, minutes_from_now):
    from datetime import datetime, timedelta, timezone
    return {"title": "CPI", "country": country, "impact": impact,
            "time": datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now), "forecast": "", "previous": ""}


def test_currency_mapping():
    from jarvis.events import currencies_for
    assert currencies_for("XAUUSD-VIP") == {"USD"}
    assert currencies_for("NAS100.") == {"USD"}
    assert currencies_for("EURUSD") == {"EUR", "USD"}


def test_blackout_window_and_veto(cfg):
    from jarvis.events import apply_blackout, blackout
    assert blackout("NAS100.", [_ev("USD", "High", 60)])
    assert blackout("NAS100.", [_ev("USD", "High", -30)])
    assert not blackout("NAS100.", [_ev("USD", "High", 600)])      # too far ahead
    assert not blackout("NAS100.", [_ev("USD", "Low", 30)])        # not high impact
    assert not blackout("NAS100.", [_ev("EUR", "High", 30)])       # other currency
    a = apply_blackout(analyze(_up_candidate(cfg), cfg), "test")
    assert a.decision == "HOLD" and a.entry_price is None


def test_events_feed_down_returns_empty(tmp_path):
    from jarvis.events import fetch_events
    assert fetch_events("http://127.0.0.1:1/x", str(tmp_path / "c.json"), timeout=1) == []


# --- sell side ---
def _down_candidate(cfg_):
    from dataclasses import replace
    for seed in range(1, 200):
        c = build_candidate("D", SyntheticData(seed=seed).daily_bars(["D"])["D"], cfg_)
        if c.last_close < c.ema20 < c.ema50 and 25 <= c.rsi:
            return c
    pytest.skip("no downtrend candidate")


def test_short_off_by_default_and_sell_when_enabled(cfg):
    from dataclasses import replace
    c = _down_candidate(cfg)
    assert analyze(c, cfg).decision == "AVOID"                       # default: shorting is off
    a = analyze(c, replace(cfg, allow_short=True, min_avg_volume=0))
    assert a.decision == "SELL"
    assert a.stop_price > a.entry_price > a.target_price             # stop above, target below


def test_risk_manager_checks_stop_side_for_sell(cfg):
    from dataclasses import replace
    c = _down_candidate(cfg)
    a = analyze(c, replace(cfg, allow_short=True, min_avg_volume=0))
    ok = RiskManager(cfg).check(a, c, 1_000_000, set(), set(), persist_state=False)
    assert ok.approved and ok.quantity >= 1
    bad = a.model_copy(update={"stop_price": a.entry_price - 1})      # stop below entry is wrong for a short
    assert not RiskManager(cfg).check(bad, c, 1_000_000, set(), set(), persist_state=False).approved


def test_mt5_sell_uses_bid_and_stop_above(fake_mt5, cfg):
    from jarvis.mt5_backend import MT5Executor
    fake_mt5.ORDER_TYPE_BUY, fake_mt5.ORDER_TYPE_SELL = 0, 1
    fake_mt5.TRADE_ACTION_DEAL, fake_mt5.ORDER_TIME_GTC, fake_mt5.TRADE_RETCODE_DONE = 1, 0, 10009
    fake_mt5.symbol_info_tick = lambda s: type("T", (), {"bid": 1.0990, "ask": 1.1000})()
    fake_mt5.order_send = lambda req: (fake_mt5.sent.append(req), type("R", (), {"retcode": 10009, "comment": ""})())[1]
    ex = MT5Executor(cfg, dry_run=False)
    sell = TradePlan(symbol="EURUSD", decision="SELL", entry_price=1.0990, quantity=25_000, stop_price=1.1050,
                     target_price=1.0900, confidence="HIGH", reason="t", risk_approved=True)
    assert ex.submit(sell, set()) == "SUBMITTED"
    req = fake_mt5.sent[-1]
    assert req["type"] == 1 and req["price"] == 1.0990 and req["sl"] > req["price"] > req["tp"]
    wrong = sell.model_copy(update={"stop_price": 1.0900})
    with pytest.raises(ValueError):
        ex.submit(wrong, set())


def test_spread_guard_blocks_expensive_trade(cfg):
    a = buy_analysis(entry=100.0, stop=99.0)
    c = cand(cfg)
    ok = RiskManager(cfg).check(a, c, 100_000, set(), set(), persist_state=False, spread_price=0.1)
    assert ok.approved
    bad = RiskManager(cfg).check(a, c, 100_000, set(), set(), persist_state=False, spread_price=0.5)   # 50% of stop distance
    assert not bad.approved and any("spread" in r for r in bad.reasons)


def test_no_new_trades_after_daily_profit_target(cfg, tmp_path):
    import json
    from dataclasses import replace
    json.dump({"peak_equity": 1020, "day": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date().isoformat(),
               "day_start": 1000.0}, open(cfg.state_path, "w"))
    a = buy_analysis()
    rd_ = RiskManager(cfg).check(a, cand(cfg), 1021.0, set(), set())      # +2.1% today, target is 2%
    assert not rd_.approved and any("profit target" in r for r in rd_.reasons)
    assert RiskManager(replace(cfg, bank_profit=False)).check(a, cand(cfg), 1021.0, set(), set()).approved


def test_journal_dedupe_logs_changes_not_repeats():
    import main as m
    m._LAST_LOGGED.clear()
    assert m.should_log("S", "HOLD", "NO_ORDER", "r", "n")
    assert not m.should_log("S", "HOLD", "NO_ORDER", "r", "n")         # same line a second later: skipped
    assert m.should_log("S", "BUY", "REJECTED", "r2", "blocked")        # changed: logged
    assert m.should_log("S", "BUY", "SUBMITTED", "r2", "")              # orders are always logged
    assert m.should_log("S", "BUY", "SUBMITTED", "r2", "")


def test_drawdown_pause_ends_on_a_new_day(cfg):
    import json
    json.dump({"peak_equity": 1015000.0, "day": "2000-01-01", "day_start": 1015000.0}, open(cfg.state_path, "w"))   # old peak from another day
    rd_ = RiskManager(cfg).check(buy_analysis(), cand(cfg), 950000.0, set(), set())
    assert rd_.approved                                                    # 950 is >5% under 1015, but the peak restarted today
    json.dump({"peak_equity": 1015000.0, "day": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date().isoformat(),
               "day_start": 1015000.0}, open(cfg.state_path, "w"))            # same day: the pause still holds
    assert not RiskManager(cfg).check(buy_analysis(), cand(cfg), 950000.0, set(), set()).approved


def test_weekly_and_monthly_loss_halts(cfg):
    import json
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    iso = now.isocalendar()
    base = {"peak_equity": 1_000_000.0, "day": now.date().isoformat(), "day_start": 1_000_000.0,
            "week": f"{iso[0]}-W{iso[1]:02d}", "month": now.date().isoformat()[:7]}
    # down 6% on the week (limit 5%) but flat on the day: weekly halt
    json.dump({**base, "week_start": 1_000_000.0, "month_start": 1_000_000.0, "day_start": 940_000.0}, open(cfg.state_path, "w"))
    rd_ = RiskManager(cfg).check(buy_analysis(), cand(cfg), 940_000.0, set(), set())
    assert not rd_.approved and any("weekly loss limit" in x for x in rd_.reasons)
    # down 3% on the week, 9% on the month: monthly halt only
    json.dump({**base, "week_start": 970_000.0, "month_start": 1_030_000.0, "day_start": 940_000.0, "peak_equity": 940_000.0}, open(cfg.state_path, "w"))
    rd2 = RiskManager(cfg).check(buy_analysis(), cand(cfg), 940_000.0, set(), set())
    assert not rd2.approved and any("monthly loss limit" in x for x in rd2.reasons) and not any("weekly" in x for x in rd2.reasons)
    # fresh week and month markers: both reset to today's equity, nothing blocks
    json.dump({**base, "week": "2000-W01", "month": "2000-01", "week_start": 2_000_000.0, "month_start": 2_000_000.0,
               "day_start": 940_000.0, "peak_equity": 940_000.0}, open(cfg.state_path, "w"))
    assert RiskManager(cfg).check(buy_analysis(), cand(cfg), 940_000.0, set(), set()).approved
