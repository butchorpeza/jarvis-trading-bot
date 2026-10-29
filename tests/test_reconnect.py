import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_brokerlink import Clock, FakeMT5  # noqa: E402
from jarvis import brokerlink as bl  # noqa: E402


def test_reconnect_reopens_mt5_when_it_was_closed_and_logs_in():
    started, c, m = [], Clock(), FakeMT5(ready_after=2)
    ok, msg = bl.reconnect(m, timeout=60, running=lambda: False, start=lambda p: started.append(p), find=lambda: "C:/x/terminal64.exe",
                           sleep=c.sleep, now=c.now)
    assert ok and started == ["C:/x/terminal64.exe"]


def test_reconnect_does_not_start_a_second_terminal_if_one_is_running():
    started, c, m = [], Clock(), FakeMT5(ready_after=0)
    ok, _ = bl.reconnect(m, timeout=60, running=lambda: True, start=lambda p: started.append(p), find=lambda: "C:/x/terminal64.exe",
                         sleep=c.sleep, now=c.now)
    assert ok and started == []


def test_reconnect_reports_failure_with_a_reason():
    c, m = Clock(), FakeMT5(ready_after=10_000)
    ok, msg = bl.reconnect(m, timeout=15, running=lambda: True, start=lambda p: None, find=lambda: None, sleep=c.sleep, now=c.now)
    assert not ok and "not logged in" in msg
