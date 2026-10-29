"""Start MetaTrader 5 if needed, wait until it is logged in to its saved account, and map the watchlist to the
symbol names of whatever broker it connected to. Works with any MT5 broker; VT Markets is what it was tuned on.
It never types a password: MT5 logs in by itself with the account the terminal has saved
(File -> Login to Trade Account -> tick "Save account information", once)."""
import os
import subprocess
import time

DEFAULT_PATHS = [r"C:\Program Files\MetaTrader 5\terminal64.exe", r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe"]

# what a watchlist entry means, so a different broker's naming can be matched (VT Markets: XAUUSD-VIP, NAS100., DJ30.)
KINDS = {
    "gold": ("XAUUSD", "GOLD", "XAU"),
    "nasdaq": ("NAS100", "USTEC", "US100", "NDX", "NASDAQ"),
    "dow": ("DJ30", "US30", "DJI", "WS30", "DOW"),
    "sp500": ("US500", "SPX500", "SP500", "SPX"),
    "eurusd": ("EURUSD",),
    "gbpusd": ("GBPUSD",),
}

HINTS = {
    -6: "MT5 is open but not logged in to a trading account. In MT5 use File -> Login to Trade Account and tick 'Save account information'.",
    -10005: "Could not talk to MT5 (timeout). Close MT5 fully and run the launcher again.",
}


def find_terminal() -> str | None:
    env = os.getenv("JARVIS_MT5_PATH")
    for p in ([env] if env else []) + DEFAULT_PATHS:
        if p and os.path.exists(p):
            return p
    return None


def terminal_running() -> bool:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq terminal64.exe", "/NH"], capture_output=True, text=True).stdout
    return "terminal64.exe" in out


def start_terminal(path: str) -> None:
    """Normal privilege (not 'run as administrator'): the Python API refuses a terminal with different rights."""
    subprocess.Popen([path], cwd=os.path.dirname(path), creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))


def ensure_connected(mt5, timeout: float = 120.0, sleep=time.sleep, now=time.time) -> tuple[bool, str, dict]:
    """Returns (ok, message, info). ok only when the terminal is connected to a DEMO account."""
    end = now() + timeout
    last = "no answer yet"
    while True:
        if mt5.initialize():
            acct, term = mt5.account_info(), mt5.terminal_info()
            if acct is not None and term is not None and term.connected:
                demo = acct.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO
                info = {"server": acct.server, "login_masked": "*" * max(0, len(str(acct.login)) - 3) + str(acct.login)[-3:],
                        "demo": demo, "currency": acct.currency, "balance": acct.balance}
                if not demo:
                    mt5.shutdown()
                    return False, f"Connected to {acct.server}, but it is NOT a demo account. JARVIS refuses live accounts.", info
                return True, f"Connected to {acct.server} (demo)", info
            last = "terminal is up but not connected to the broker yet"
            mt5.shutdown()
        else:
            code, text = mt5.last_error()
            last = HINTS.get(code, f"MT5 says: {text} ({code})")
        if now() >= end:
            return False, last, {}
        sleep(3)


def resolve_watchlist(mt5, wanted: list[str]) -> tuple[list[str], list[str]]:
    """Maps each wanted symbol to one that exists AND allows full trading on the connected broker.
    Exact names are kept. Otherwise a symbol of the same kind (gold, nasdaq, ...) is chosen. Returns (symbols, notes)."""
    tradable = [s for s in (mt5.symbols_get() or []) if s.trade_mode == mt5.SYMBOL_TRADE_MODE_FULL and not s.path.startswith("Stocks")]
    names = [s.name for s in tradable]
    chosen, notes = [], []
    for w in wanted:
        if w in names:
            chosen.append(w)
            continue
        kind = next((k for k, keys in KINDS.items() if any(key in w.upper() for key in keys)), None)
        cands = [n for n in names if kind and any(key in n.upper() for key in KINDS[kind])]
        cands = [n for n in cands if not any(x in n.upper() for x in ("VIP", "FT", "FUT")) or w.upper() in n.upper()] or cands
        if cands:
            pick = sorted(cands, key=lambda n: (len(n), n))[0]
            chosen.append(pick)
            notes.append(f"{w} is not tradable here; using {pick}")
        else:
            notes.append(f"{w} is not available on this broker; skipped")
    return list(dict.fromkeys(chosen)), notes


def reconnect(mt5, timeout: float = 60.0, running=None, start=None, find=None, **kw) -> tuple[bool, str]:
    """Called after repeated errors: close the stale session, reopen MetaTrader 5 if it was closed, log back in.
    The arguments let tests replace the real process checks."""
    running, start, find = running or terminal_running, start or start_terminal, find or find_terminal
    try:
        mt5.shutdown()
    except Exception:  # noqa: BLE001
        pass
    path = find()
    if path and not running():
        start(path)
    ok, msg, _ = ensure_connected(mt5, timeout=timeout, **kw)
    return ok, msg
