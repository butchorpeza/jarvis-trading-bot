"""Broker interface. MT5Broker connects a real/demo MT5 account."""
import os
from abc import ABC, abstractmethod
import pandas as pd


class Broker(ABC):
    @abstractmethod
    def candles(self, symbol: str, timeframe: str, n: int) -> pd.DataFrame: ...
    @abstractmethod
    def equity(self) -> float: ...
    @abstractmethod
    def n_open(self) -> int: ...
    @abstractmethod
    def open(self, symbol: str, side: str, lots: float, sl: float, tp: float) -> None: ...
    @abstractmethod
    def close_all(self) -> None: ...


class MT5Broker(Broker):
    TF = {"1m": "TIMEFRAME_M1", "5m": "TIMEFRAME_M5", "15m": "TIMEFRAME_M15",
          "1h": "TIMEFRAME_H1", "4h": "TIMEFRAME_H4", "1d": "TIMEFRAME_D1"}

    def __init__(self, cfg: dict):
        import MetaTrader5 as mt5
        self.mt5 = mt5
        m = cfg["mt5"]
        if m["login"]:
            ok = mt5.initialize(login=m["login"], password=os.environ[m["password_env"]], server=m["server"])
        else:
            ok = mt5.initialize()  # use the terminal's already logged-in account
        if not ok:
            raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")

    def candles(self, symbol, timeframe, n):
        mt5 = self.mt5
        r = mt5.copy_rates_from_pos(symbol, getattr(mt5, self.TF[timeframe]), 0, n)
        df = pd.DataFrame(r)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df.set_index("time")[["open", "high", "low", "close", "tick_volume"]]

    def equity(self):
        return self.mt5.account_info().equity

    def n_open(self):
        return len(self.mt5.positions_get() or [])

    def open(self, symbol, side, lots, sl, tp):
        mt5 = self.mt5
        t = mt5.symbol_info_tick(symbol)
        buy = side == "buy"
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": lots,
               "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
               "price": t.ask if buy else t.bid, "sl": sl, "tp": tp, "deviation": 20,
               "magic": 424242, "comment": "bot", "type_time": mt5.ORDER_TIME_GTC,
               "type_filling": mt5.ORDER_FILLING_IOC}
        res = mt5.order_send(req)
        if res.retcode != mt5.TRADE_RETCODE_DONE:
            print("order failed", res.retcode, res.comment)

    def close_all(self):
        mt5 = self.mt5
        for p in mt5.positions_get() or []:
            t = mt5.symbol_info_tick(p.symbol)
            buy = p.type == mt5.POSITION_TYPE_BUY
            mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": p.volume,
                            "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
                            "position": p.ticket, "price": t.bid if buy else t.ask,
                            "deviation": 20, "magic": 424242, "type_filling": mt5.ORDER_FILLING_IOC})
