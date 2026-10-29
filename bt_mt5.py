"""Honest backtest on YOUR MT5 history (data_tf/*.csv). Results are in R (1R = the stop distance), after spread.
Entry = next candle open; one trade at a time per symbol; 4-candle cooldown; SL checked before TP inside a candle.
First 60% of each history = build period, last 40% = unseen period."""
import itertools
import sys

import numpy as np
import pandas as pd

SYMS = {"XAUUSD-VIP": 0.28, "NAS100.": 0.9, "DJ30.": 1.2}      # full spread in price units (from MT5 right now)
TFS = ["M1", "M5", "M15", "H1"]
COOLDOWN = 4


def load(sym, tf):
    df = pd.read_csv(f"data_tf/{sym}_{tf}.csv", index_col=0, parse_dates=True)
    c, h, l = df.close, df.high, df.low
    df["ema20"], df["ema50"] = c.ewm(span=20, adjust=False).mean(), c.ewm(span=50, adjust=False).mean()
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    return df.dropna()


def run(df, spread, sl_m, tp_m, managed, sides, ltf_df=None):
    o, h, l, c = df.open.values, df.high.values, df.low.values, df.close.values
    e20, e50, rsi, atr = df.ema20.values, df.ema50.values, df.rsi.values, df.atr.values
    n, i, out = len(df), 60, []
    while i < n - 2:
        side = 0
        if "BUY" in sides and c[i] > e20[i] > e50[i] and rsi[i] <= 75:
            side = 1
        elif "SELL" in sides and c[i] < e20[i] < e50[i] and rsi[i] >= 25:
            side = -1
        if side == 0 or atr[i] <= 0:
            i += 1
            continue
        d = sl_m * atr[i]
        if spread > 0.5 * d:                       # same idea as the live spread guard (stricter here)
            i += 1
            continue
        entry = o[i + 1] + (spread if side == 1 else 0.0)
        sl, tp = entry - side * d, entry + side * tp_m * atr[i]
        realized, remaining, be, partial = 0.0, 1.0, False, False
        j, exit_t = i + 1, None
        while j < n:
            hi_b = h[j] if side == 1 else h[j] + spread
            lo_b = l[j] if side == 1 else l[j] + spread
            hit_sl = lo_b <= sl if side == 1 else hi_b >= sl
            hit_tp = hi_b >= tp if side == 1 else lo_b <= tp
            if hit_sl:
                realized += remaining * side * (sl - entry) / d
                exit_t = j
                break
            if hit_tp:
                realized += remaining * side * (tp - entry) / d
                exit_t = j
                break
            if managed:
                px = c[j] + (0 if side == 1 else spread)
                r = side * (px - entry) / d
                if r >= 1.0 and not partial:
                    realized += 0.5 * 1.0
                    remaining, partial = 0.5, True
                if r >= 1.0 and not be:
                    sl, be = (entry if side == 1 else entry), True
                if r >= 1.5:
                    trail = px - side * 2.0 * atr[j]
                    sl = max(sl, trail) if side == 1 else min(sl, trail)
            j += 1
        if exit_t is None:
            break
        out.append((i, realized))
        i = exit_t + COOLDOWN
    return out


def stats(trades):
    if not trades:
        return (0, 0.0, 0.0, 0.0, 0.0)
    r = np.array([t[1] for t in trades])
    wins, losses = r[r > 0].sum(), -r[r < 0].sum()
    return (len(r), 100 * (r > 0).mean(), r.mean(), r.sum(), wins / losses if losses else float("inf"))


def main():
    grid = [(1.5, 2.5), (2.0, 3.0), (3.0, 4.5), (1.0, 1.5)]
    rows = []
    for tf in TFS:
        for (sl_m, tp_m), managed, sides in itertools.product(grid, (False, True), (("BUY", "SELL"), ("BUY",), ("SELL",))):
            tr_all, te_all = [], []
            for sym, sp in SYMS.items():
                df = load(sym, tf)
                cut = int(len(df) * 0.6)
                t = run(df, sp, sl_m, tp_m, managed, sides)
                tr_all += [x for x in t if x[0] < cut]
                te_all += [x for x in t if x[0] >= cut]
            rows.append((tf, sl_m, tp_m, managed, "+".join(sides), stats(tr_all), stats(te_all)))
    print(f"{'TF':4s}{'SL/TP':>9s} {'mgd':>4s} {'sides':>9s} | BUILD trades avgR | UNSEEN trades win% avgR  totalR  PF")
    for tf, sl_m, tp_m, mg, sd, a, b in sorted(rows, key=lambda r: -r[6][2]):
        if b[0] >= 30:
            print(f"{tf:4s}{sl_m:>4}/{tp_m:<4} {str(mg)[0]:>4} {sd:>9s} | {a[0]:6d} {a[2]:+.3f}  | {b[0]:6d} {b[1]:5.1f} {b[2]:+.3f} {b[3]:+8.1f} {b[4]:5.2f}")
    print("\nWorst:")
    for tf, sl_m, tp_m, mg, sd, a, b in sorted(rows, key=lambda r: r[6][2])[:5]:
        if b[0] >= 30:
            print(f"{tf:4s}{sl_m:>4}/{tp_m:<4} {str(mg)[0]:>4} {sd:>9s} | {a[0]:6d} {a[2]:+.3f}  | {b[0]:6d} {b[1]:5.1f} {b[2]:+.3f} {b[3]:+8.1f} {b[4]:5.2f}")


if __name__ == "__main__":
    sys.exit(main())
