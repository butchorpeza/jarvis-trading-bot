"""Settings. Demo mode is mandatory: there is no live-trading switch anywhere in JARVIS."""
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

MAX_WATCHLIST = 5


class PaperModeError(RuntimeError):
    pass


def require_demo_mode() -> None:
    """MT5 backend: demo accounts only (the executor also verifies the account type)."""
    if os.getenv("JARVIS_MT5_DEMO", "").strip().lower() != "true":
        raise PaperModeError("MT5 backend is locked to DEMO accounts. Set JARVIS_MT5_DEMO=true in .env.")


BROKERS = ("mt5",)
TF_SECONDS = {"S1": 1, "M1": 60, "M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}


@dataclass(frozen=True)
class Settings:
    watchlist: tuple[str, ...] = ("EURUSD", "XAUUSD", "US100", "US30")
    max_risk_per_trade: float = 0.01      # 1% of account
    max_position_pct: float = 0.10        # 10% of account in one position (cash instruments)
    max_margin_pct: float = 0.20          # leveraged CFDs: margin used by one position <= 20% of equity
    max_open_positions: int = 5
    drawdown_pause: float = 0.05          # 5% drawdown from the peak pauses new orders
    drawdown_reset_daily: bool = True     # the peak restarts each UTC day, so a pause ends the next day instead of lasting forever
    max_data_age_days: int = 4            # calendar days (covers weekends)
    min_avg_volume: float = 1_000_000
    # position manager (trades JARVIS already opened)
    breakeven: bool = True
    trailing: bool = True
    partial_exit: bool = True
    scale_in: bool = True
    spike_guard: bool = True
    trend_exit: bool = True
    protect_all: bool = True              # add a missing stop / target to ANY open position
    drop_guard: bool = True               # pause new BUYs and protect longs when the market falls fast
    drop_action: str = "protect"          # pause | protect (winners to break-even) | close_longs
    drop_1m_pct: float = 0.15             # a fall of 0.15% in 1 minute...
    drop_5m_pct: float = 0.30             # ...or 0.30% in 5 minutes counts as a drop
    halt_minutes: int = 15
    be_at_r: float = 1.0                  # move stop to break-even at +1R (R = initial risk)
    trail_start_r: float = 1.5            # start trailing at +1.5R
    trail_atr: float = 2.0                # trail 2 ATR behind price
    partial_at_r: float = 1.0
    partial_frac: float = 0.5
    scale_in_at_r: float = 1.5            # add at +1.5R, +3R ...
    max_adds: int = 2
    add_frac: float = 0.5
    spike_mult: float = 3.0               # candle range >= 3 ATR counts as a spike
    news_lock_minutes: int = 30
    max_trades_per_symbol_day: int = 3    # re-entry on the same symbol is allowed, up to this many times a day
    max_spread_frac: float = 0.25         # skip a trade if the spread is more than 25% of the stop distance
    daily_profit_target: float = 0.02     # bank the day: at +2% equity stop new trades and close the winners
    bank_profit: bool = True
    max_weekly_loss: float = 0.05         # weekly / monthly halts (idea from the K.I.T. risk docs and the drawdown-circuit-breaker skill)
    max_monthly_loss: float = 0.08
    max_daily_loss: float = 0.03          # stop opening trades for the day at -3% equity
    timeframe: str = "D1"                 # candle size the signals are built on: M5, M15, H1, H4, D1
    allow_short: bool = False             # OFF by default (backtest: shorts lost on unseen data). Dashboard toggle or JARVIS_ALLOW_SHORT=true
    stop_atr: float = 1.5
    target_atr: float = 2.5
    sim_equity: float = 100_000.0         # used only by --sample mode
    log_path: str = "logs/trades.csv"
    state_path: str = "data/jarvis_state.json"


SELECTED_FILE = "data/selected_markets.json"


def _selected_markets() -> tuple[str, ...] | None:
    """Markets chosen on the dashboard (overrides .env). Symbol names are broker case-sensitive, so no upper()."""
    try:
        import json
        syms = json.load(open(SELECTED_FILE, encoding="utf-8")).get("symbols", [])
        syms = tuple(str(s).strip() for s in syms if str(s).strip())
        return syms or None
    except (OSError, ValueError, AttributeError):
        return None


def load_settings() -> Settings:
    load_dotenv()
    raw = os.getenv("JARVIS_WATCHLIST", "")
    wl = _selected_markets() or tuple(s.strip() for s in raw.split(",") if s.strip()) or Settings().watchlist
    if len(wl) > MAX_WATCHLIST:
        raise ValueError(f"Watchlist has {len(wl)} symbols; version one allows at most {MAX_WATCHLIST}.")
    short = os.getenv("JARVIS_ALLOW_SHORT", "false").strip().lower() == "true"
    try:
        import json
        short = bool(json.load(open("data/trade_settings.json", encoding="utf-8")).get("allow_short", short))
    except (OSError, ValueError, AttributeError):
        pass
    tf = os.getenv("JARVIS_TIMEFRAME", "D1").strip().upper()
    try:
        import json as _json
        tf = str(_json.load(open("data/trade_settings.json", encoding="utf-8")).get("timeframe", tf)).upper()
    except (OSError, ValueError, AttributeError):
        pass
    if tf not in TF_SECONDS:
        tf = "D1"
    flags = {}
    try:
        import json as _j
        d = _j.load(open("data/trade_settings.json", encoding="utf-8"))
        for k in ("breakeven", "trailing", "partial_exit", "scale_in", "spike_guard", "trend_exit", "bank_profit"):
            if k in d:
                flags[k] = bool(d[k])
    except (OSError, ValueError, AttributeError):
        pass
    for k, lo, hi in (("stop_atr", 0.5, 8.0), ("target_atr", 0.5, 12.0)):
        try:
            import json as _j3
            v = float(_j3.load(open("data/trade_settings.json", encoding="utf-8")).get(k))
            if lo <= v <= hi:
                flags[k] = v
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    try:
        import json as _j4
        dd = _j4.load(open("data/trade_settings.json", encoding="utf-8"))
        if "drop_guard" in dd:
            flags["drop_guard"] = bool(dd["drop_guard"])
        if dd.get("drop_action") in ("pause", "protect", "close_longs"):
            flags["drop_action"] = dd["drop_action"]
        for k, lo, hi in (("drop_1m_pct", 0.02, 5.0), ("drop_5m_pct", 0.05, 10.0)):
            if lo <= float(dd.get(k, -1)) <= hi:
                flags[k] = float(dd[k])
        if 1 <= int(dd.get("halt_minutes", 0)) <= 240:
            flags["halt_minutes"] = int(dd["halt_minutes"])
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    target = None
    try:
        import json as _j2
        target = float(_j2.load(open("data/trade_settings.json", encoding="utf-8")).get("daily_profit_target"))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    if target is not None and 0.001 <= target <= 0.5:
        flags["daily_profit_target"] = target
    return Settings(watchlist=wl, allow_short=short, timeframe=tf, **flags)
