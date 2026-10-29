"""MetaTrader 5 backend (data + executor). DEMO accounts only: refuses to run on a live account.
Needs: pip install MetaTrader5, the MT5 terminal installed and logged in (Windows)."""
import math
import os
from datetime import datetime, timezone

import pandas as pd

from .config import Settings, require_demo_mode
from .models import TradePlan


def _load_mt5():
    try:
        import MetaTrader5 as mt5
    except ImportError as e:
        raise RuntimeError("MetaTrader5 package missing. Run: pip install MetaTrader5") from e
    return mt5


def _connect(mt5):
    login = os.getenv("JARVIS_MT5_LOGIN", "").strip()
    if login:  # optional: let the script log in itself; password lives only in your local .env
        ok = mt5.initialize(login=int(login), password=os.getenv("JARVIS_MT5_PASSWORD", ""),
                            server=os.getenv("JARVIS_MT5_SERVER", ""))
    else:
        ok = mt5.initialize()
    if not ok:
        raise RuntimeError(f"MT5 terminal not reachable: {mt5.last_error()}. Open MT5 and log in first.")
    info = mt5.account_info()
    if info is None or info.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        mt5.shutdown()
        raise RuntimeError("Connected MT5 account is not a DEMO account. JARVIS refuses to run on it.")


class MT5Data:
    def __init__(self, timeframe: str = "D1"):
        require_demo_mode()
        self.mt5 = _load_mt5()
        _connect(self.mt5)
        self.timeframe = timeframe

    def daily_bars(self, symbols, days=120):
        mt5, out = self.mt5, {}
        for s in symbols:
            mt5.symbol_select(s, True)
            # start_pos=1 skips the candle that is still forming: signals use CLOSED candles only
            r = mt5.copy_rates_from_pos(s, getattr(mt5, f"TIMEFRAME_{self.timeframe}"), 1, 200)
            if r is None or len(r) == 0:
                continue
            df = pd.DataFrame(r)
            df.index = pd.to_datetime(df["time"], unit="s", utc=True)
            df = df.rename(columns={"tick_volume": "volume"})[["open", "high", "low", "close", "volume"]].astype(float)
            tick = mt5.symbol_info_tick(s)
            if tick is not None:
                df.attrs["server_now"] = pd.Timestamp(tick.time, unit="s", tz="UTC").to_pydatetime()  # broker clock, same basis as bar times
            out[s] = df
        return out


class MT5Executor:
    def __init__(self, cfg: Settings, dry_run: bool = True):
        require_demo_mode()
        self.cfg, self.dry_run = cfg, dry_run
        self.mt5 = _load_mt5()
        _connect(self.mt5)

    def equity(self) -> float:
        return float(self.mt5.account_info().equity)

    def open_symbols(self) -> set[str]:
        return {p.symbol for p in (self.mt5.positions_get() or [])}

    def account_snapshot(self) -> dict:
        """Read-only view of the demo account for the dashboard. The login number is masked."""
        mt5 = self.mt5
        a = mt5.account_info()
        positions = []
        for p in mt5.positions_get() or []:
            positions.append({
                "ticket": p.ticket, "symbol": p.symbol, "side": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL", "volume": p.volume,
                "price_open": p.price_open, "price_current": p.price_current, "sl": p.sl, "tp": p.tp,
                "profit": round(p.profit, 2), "opened": datetime.fromtimestamp(p.time, timezone.utc).isoformat(timespec="seconds")})
        return {
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "server": a.server,
            "login_masked": "*" * max(0, len(str(a.login)) - 3) + str(a.login)[-3:], "demo": a.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO,
            "currency": a.currency, "leverage": a.leverage, "balance": a.balance, "equity": a.equity, "profit": round(a.profit, 2),
            "margin": a.margin, "margin_free": a.margin_free, "algo_trading_allowed": bool(mt5.terminal_info().trade_allowed),
            "positions": positions}

    def _filling(self, info):
        mt5 = self.mt5
        if info.filling_mode & 1:
            return mt5.ORDER_FILLING_FOK
        if info.filling_mode & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def margin_per_unit(self, symbol: str, price: float, buy: bool = True) -> float | None:
        """Margin the broker asks for one unit (1 lot = contract_size units), or None if unavailable."""
        mt5 = self.mt5
        info = mt5.symbol_info(symbol)
        try:
            m = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL, symbol, 1.0, price)
        except Exception:  # noqa: BLE001
            return None
        return (m / info.trade_contract_size) if (m and info) else None

    def spread_price(self, symbol: str) -> float | None:
        info = self.mt5.symbol_info(symbol)
        return info.spread * info.point if info else None

    def min_units(self, symbol: str) -> float | None:
        info = self.mt5.symbol_info(symbol)
        return info.volume_min * info.trade_contract_size if info else None

    def lots_for(self, symbol: str, units: float) -> float:
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise ValueError(f"unknown MT5 symbol {symbol}")
        step = info.volume_step or 0.01
        lots = math.floor(units / info.trade_contract_size / step) * step
        lots = round(lots, 8)
        if lots < info.volume_min:
            raise ValueError(f"risk-approved size is below the broker minimum lot ({info.volume_min})")
        return min(lots, info.volume_max)

    def submit(self, plan: TradePlan, today_submitted: set[str]) -> str:
        require_demo_mode()
        if not plan.risk_approved:
            raise ValueError("Risk Manager did not approve this plan.")
        if plan.decision not in ("BUY", "SELL") or plan.quantity <= 0 or not plan.stop_price or not plan.target_price:
            raise ValueError("Plan is not a valid BUY/SELL with quantity, stop and target.")
        buy = plan.decision == "BUY"
        if (buy and plan.stop_price >= (plan.entry_price or 0)) or (not buy and plan.stop_price <= (plan.entry_price or 0)):
            raise ValueError("Stop is on the wrong side of the entry.")
        if plan.symbol in today_submitted:
            raise ValueError("Duplicate order blocked.")
        lots = self.lots_for(plan.symbol, plan.quantity)
        if self.dry_run:
            return "DRY_RUN"
        mt5 = self.mt5
        info = mt5.symbol_info(plan.symbol)
        tick = mt5.symbol_info_tick(plan.symbol)
        day = datetime.now(timezone.utc).date().isoformat()
        res = mt5.order_send({
            "action": mt5.TRADE_ACTION_DEAL, "symbol": plan.symbol, "volume": lots,
            "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL, "price": tick.ask if buy else tick.bid,
            "sl": round(plan.stop_price, info.digits), "tp": round(plan.target_price, info.digits),
            "deviation": 20, "magic": 424242, "comment": f"jarvis-{day}"[:31],
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling(info)})
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            raise RuntimeError(f"MT5 rejected order: {getattr(res, 'retcode', None)} {getattr(res, 'comment', '')}")
        return "SUBMITTED"
