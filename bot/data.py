import pandas as pd
import yfinance as yf


def download(cfg: dict, period="730d"):
    """Free backtest data (yfinance caps 1h bars at ~730 days). Live data comes from MT5."""
    for sym, s in cfg["symbols"].items():
        df = yf.download(s["yf"], period=period, interval=cfg["timeframe"], auto_adjust=False, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns=str.lower)[["open", "high", "low", "close"]].dropna()
        if df.index.tz is not None:
            df.index = df.index.tz_convert(None)
        df.to_csv(f"data/{sym}.csv")
        print(sym, len(df), "bars")
