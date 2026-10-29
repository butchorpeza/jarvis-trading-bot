"""Bar-by-bar backtest with spread, commission, SL/TP. Entry at next bar open (no lookahead).
If SL and TP both hit in one bar, SL is assumed first (pessimistic)."""
import numpy as np
import pandas as pd
from .indicators import add_indicators
from .decision import Decider
from .risk import RiskManager


def run(cfg, symbols=None, start=None, end=None, decider=None, random_entries=False, seed=0):
    rk = cfg["risk"]
    decider = decider or Decider(cfg["decision"], cfg["laya_min_confidence"])
    rng = np.random.default_rng(seed)
    data = {s: add_indicators(pd.read_csv(f"data/{s}.csv", index_col=0, parse_dates=True))
            for s in (symbols or cfg["symbols"])}
    times = sorted(set().union(*[d.index for d in data.values()]))
    if start:
        times = [t for t in times if t >= pd.Timestamp(start)]
    if end:
        times = [t for t in times if t < pd.Timestamp(end)]
    risk = RiskManager(rk)
    eq, open_pos, trades, pending = rk["start_balance"], {}, [], {}
    for t in times:
        risk.new_bar(t, eq)
        for s, df in data.items():
            if t not in df.index:
                continue
            row = df.loc[t]
            sp = cfg["symbols"][s]
            k = sp["contract"]
            p = open_pos.get(s)
            if p:
                buy = p["side"] == "buy"
                hit_sl = row["low"] <= p["sl"] if buy else row["high"] >= p["sl"]
                hit_tp = row["high"] >= p["tp"] if buy else row["low"] <= p["tp"]
                if hit_sl or hit_tp:
                    px = p["sl"] if hit_sl else p["tp"]
                    d = (px - p["entry"]) if buy else (p["entry"] - px)
                    pnl = d * p["lots"] * k - rk["commission_per_lot"] * p["lots"]
                    eq += pnl
                    trades.append({"sym": s, "t": t, "side": p["side"], "pnl": pnl})
                    del open_pos[s]
            if s in pending and s not in open_pos:
                side, atr = pending.pop(s)
                if risk.can_open(eq, len(open_pos)):
                    half = sp["spread"] / 2
                    entry = row["open"] + (half if side == "buy" else -half)
                    sl_d, tp_d = atr * rk["sl_atr"], atr * rk["tp_atr"]
                    sgn = 1 if side == "buy" else -1
                    open_pos[s] = {"side": side, "entry": entry, "sl": entry - sgn * sl_d,
                                   "tp": entry + sgn * tp_d, "lots": risk.lots(eq, sl_d, k)}
            if s not in open_pos and s not in pending:
                if random_entries:
                    sig = "flat" if np.isnan(row["atr"]) else rng.choice(["buy", "sell", "flat"], p=[0.02, 0.02, 0.96])
                else:
                    sig, _ = decider.decide(s, row)
                if sig != "flat":
                    pending[s] = (sig, row["atr"])
    return pd.DataFrame(trades), eq


def stats(tr, start_bal):
    if tr.empty:
        return {"trades": 0}
    c = tr["pnl"].cumsum() + start_bal
    return {"trades": len(tr), "win%": round((tr.pnl > 0).mean() * 100, 1),
            "net": round(tr.pnl.sum(), 2), "max_dd%": round(((c.cummax() - c) / c.cummax()).max() * 100, 1),
            "profit_factor": round(tr.pnl[tr.pnl > 0].sum() / max(1e-9, -tr.pnl[tr.pnl < 0].sum()), 2)}
