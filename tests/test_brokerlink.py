import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis import brokerlink as bl  # noqa: E402


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    SYMBOL_TRADE_MODE_FULL = 4

    def __init__(self, ready_after=0, demo=True, error=(-6, "Authorization failed"), symbols=()):
        self.calls, self.ready_after, self.demo, self.error, self._symbols = 0, ready_after, demo, error, symbols

    def initialize(self):
        self.calls += 1
        return self.calls > self.ready_after

    def last_error(self):
        return self.error

    def account_info(self):
        return SimpleNamespace(server="Broker-Demo", login=12345678, trade_mode=0 if self.demo else 2, currency="USD", balance=1000.0)

    def terminal_info(self):
        return SimpleNamespace(connected=True)

    def shutdown(self):
        pass

    def symbols_get(self):
        return [SimpleNamespace(name=n, trade_mode=4, path=p) for n, p in self._symbols]


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_waits_for_login_then_succeeds_on_demo():
    c, m = Clock(), FakeMT5(ready_after=3)
    ok, msg, info = bl.ensure_connected(m, timeout=60, sleep=c.sleep, now=c.now)
    assert ok and info["demo"] and info["login_masked"].endswith("678") and "12345" not in info["login_masked"]


def test_gives_up_with_a_helpful_message_when_never_logged_in():
    c, m = Clock(), FakeMT5(ready_after=10_000)
    ok, msg, _ = bl.ensure_connected(m, timeout=20, sleep=c.sleep, now=c.now)
    assert not ok and "not logged in" in msg and "Save account information" in msg


def test_refuses_a_live_account():
    c, m = Clock(), FakeMT5(demo=False)
    ok, msg, info = bl.ensure_connected(m, timeout=20, sleep=c.sleep, now=c.now)
    assert not ok and "NOT a demo account" in msg and info["demo"] is False


VT = [("XAUUSD-VIP", "VT\\Gold-VIP"), ("XAUUSD.crp", "Forex\\X"), ("NAS100.", "VT\\CFD-Index."), ("DJ30.", "VT\\CFD-Index."), ("EURUSD", "Forex\\Major")]
OTHER = [("XAUUSD", "Metals\\XAUUSD"), ("USTEC", "Indices\\USTEC"), ("US30", "Indices\\US30"), ("EURUSD", "Forex\\EURUSD"), ("AAPL", "Stocks\\US\\AAPL")]


def test_keeps_exact_names_on_the_same_broker():
    syms, notes = bl.resolve_watchlist(FakeMT5(symbols=VT), ["XAUUSD-VIP", "NAS100.", "DJ30."])
    assert syms == ["XAUUSD-VIP", "NAS100.", "DJ30."] and notes == []


def test_maps_to_another_brokers_names():
    syms, notes = bl.resolve_watchlist(FakeMT5(symbols=OTHER), ["XAUUSD-VIP", "NAS100.", "DJ30."])
    assert syms == ["XAUUSD", "USTEC", "US30"] and len(notes) == 3


def test_skips_what_the_broker_does_not_have_and_never_picks_stocks():
    syms, notes = bl.resolve_watchlist(FakeMT5(symbols=[("AAPL", "Stocks\\US\\AAPL"), ("XAUUSD", "Metals\\XAUUSD")]), ["XAUUSD-VIP", "NAS100."])
    assert syms == ["XAUUSD"] and any("NAS100." in n and "skipped" in n for n in notes)
