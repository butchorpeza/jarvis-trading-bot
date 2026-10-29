"""backtest-expert method applied to JARVIS's 15-minute setup (stop 3 ATR, target 4.5 ATR, buy+sell, no manager).
Questions: does the edge survive (1) moving the stop/target, (2) higher costs, (3) other symbols and other time periods?
Results are in R after spread. 'Unseen' = last 40% of each history."""
import itertools
import math
import sys

import numpy as np

import bt_mt5 as b

TF, BASE_SL, BASE_TP = "M15", 3.0, 4.5
SIDES = ("BUY", "SELL")
DATA = {s: b.load(s, TF) for s in b.SYMS}


def collect(sl, tp, spread_mult=1.0, syms=None, sides=SIDES):
    """Returns list of (symbol, bar_index, cut_index, R)."""
    out = []
    for s, sp in b.SYMS.items():
        if syms and s not in syms:
            continue
        df = DATA[s]
        cut = int(len(df) * 0.6)
        for i, r in b.run(df, sp * spread_mult, sl, tp, False, sides):
            out.append((s, i, cut, r))
    return out


def summ(rows):
    r = np.array([x[3] for x in rows])
    if len(r) < 5:
        return None
    se = r.std(ddof=1) / math.sqrt(len(r))
    return dict(n=len(r), win=100 * (r > 0).mean(), avg=r.mean(), tot=r.sum(), t=(r.mean() / se if se else 0.0),
                pf=(r[r > 0].sum() / -r[r < 0].sum()) if (r < 0).any() else float("inf"))


def line(label, s):
    if not s:
        return f"{label:26s} too few trades"
    return f"{label:26s} n={s['n']:5d} win={s['win']:4.1f}% avgR={s['avg']:+.3f} totalR={s['tot']:+7.1f} PF={s['pf']:.2f} t={s['t']:+.2f}"


def main():
    base = collect(BASE_SL, BASE_TP)
    unseen = [x for x in base if x[1] >= x[2]]
    build = [x for x in base if x[1] < x[2]]
    print("== 0. Baseline (stop 3 / target 4.5 ATR, buy+sell, 15-minute)")
    print(line("build period (60%)", summ(build)))
    print(line("unseen period (40%)", summ(unseen)))

    print("\n== 1. Parameter sensitivity, UNSEEN avgR (stop % of baseline down, target % across)")
    sl_set = [round(BASE_SL * k, 2) for k in (0.5, 0.75, 1.0, 1.25, 1.5)]
    tp_set = [round(BASE_TP * k, 2) for k in (0.8, 0.9, 1.0, 1.1, 1.2)]
    print("stop\\target " + " ".join(f"{t:>7}" for t in tp_set))
    cells = []
    for sl in sl_set:
        row = []
        for tp in tp_set:
            s = summ([x for x in collect(sl, tp) if x[1] >= x[2]])
            row.append(s["avg"] if s else float("nan"))
        cells += row
        print(f"{sl:>10}  " + " ".join(f"{v:+7.3f}" for v in row))
    pos = sum(1 for v in cells if v > 0)
    print(f"-> {pos} of {len(cells)} neighbouring settings are positive; worst {min(cells):+.3f}, best {max(cells):+.3f}")

    print("\n== 2. Cost stress (UNSEEN, baseline): spread x1, x1.5, x2, x3")
    for m in (1.0, 1.5, 2.0, 3.0):
        print(line(f"spread x{m}", summ([x for x in collect(BASE_SL, BASE_TP, m) if x[1] >= x[2]])))

    print("\n== 3. By symbol (UNSEEN)")
    for sym in b.SYMS:
        print(line(sym, summ([x for x in unseen if x[0] == sym])))
    print("\n== 4. By side (UNSEEN)")
    for side in SIDES:
        print(line(side + " only", summ([x for x in collect(BASE_SL, BASE_TP, 1.0, None, (side,)) if x[1] >= x[2]])))

    print("\n== 5. Consistency over time: five equal slices of the whole history, avgR each")
    for sym in b.SYMS:
        rows = [x for x in base if x[0] == sym]
        n = len(DATA[sym])
        parts = []
        for k in range(5):
            seg = [x for x in rows if k * n // 5 <= x[1] < (k + 1) * n // 5]
            s = summ(seg)
            parts.append(f"{s['avg']:+.3f}" if s else "  n/a ")
        print(f"{sym:12s} " + "  ".join(parts))

    s = summ(unseen)
    print("\n== Verdict inputs")
    print(f"unseen t-stat {s['t']:+.2f} (about 2.0 is the usual bar for 'probably not luck'); "
          f"{pos}/{len(cells)} neighbours positive; spread x2 avgR "
          f"{summ([x for x in collect(BASE_SL, BASE_TP, 2.0) if x[1] >= x[2]])['avg']:+.3f}")


if __name__ == "__main__":
    sys.exit(main())
