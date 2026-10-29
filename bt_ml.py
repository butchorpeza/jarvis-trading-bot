"""Does the forex-agent repo's ML idea (momentum / MA ratio / RSI / volatility features -> sklearn ensemble) add edge
on top of JARVIS's rule signals? Walk-forward on YOUR MT5 15-minute history: train on the first 60% of trades,
test only on the last 40%. Compared with random filters of the same size, so luck is visible."""
import sys

import numpy as np
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier

import bt_mt5 as b

TF, SL, TP = "M15", 3.0, 4.5


def features(c: np.ndarray, atr: float, side: int, i: int):
    """Same family of features as the repo's MLPredictor, computed from closed candles up to bar i, sign-adjusted by side."""
    w = c[i - 20:i + 1]
    sma5, sma10, sma20 = w[-5:].mean(), w[-10:].mean(), w.mean()
    d = np.diff(w[-15:])
    gains, losses = np.maximum(d, 0).mean(), np.abs(np.minimum(d, 0)).mean()
    rsi = 100 - 100 / (1 + gains / (losses + 1e-12))
    rets = np.diff(w) / w[:-1]
    f = [w[-1] / w[-5] - 1, w[-1] / w[-10] - 1, w[-1] / w[-20] - 1, w[-1] / sma5 - 1, w[-1] / sma10 - 1, w[-1] / sma20 - 1, sma5 / sma20 - 1]
    f = [x * side for x in f]
    return f + [rsi / 100 if side == 1 else 1 - rsi / 100, rets.std(), atr / w[-1], side]


def build():
    X, y, r, idx = [], [], [], []
    for sym, sp in b.SYMS.items():
        df = b.load(sym, TF)
        cut = int(len(df) * 0.6)
        c, atr = df.close.values, df.atr.values
        e20, e50, rsi = df.ema20.values, df.ema50.values, df.rsi.values
        for i, rr in b.run(df, sp, SL, TP, False, ("BUY", "SELL")):
            side = 1 if (c[i] > e20[i] > e50[i]) else -1
            X.append(features(c, atr[i], side, i))
            y.append(1 if rr > 0 else 0)
            r.append(rr)
            idx.append(0 if i < cut else 1)       # 0 = build, 1 = unseen
    return np.array(X), np.array(y), np.array(r), np.array(idx)


def main():
    X, y, r, part = build()
    tr, te = part == 0, part == 1
    print(f"trades: build {tr.sum()}  unseen {te.sum()}   baseline avgR build {r[tr].mean():+.3f}  unseen {r[te].mean():+.3f}")
    rng = np.random.default_rng(7)
    models = {"GradientBoosting": GradientBoostingClassifier(n_estimators=150, max_depth=3, learning_rate=0.05, subsample=0.8, random_state=1),
              "RandomForest": RandomForestClassifier(n_estimators=300, min_samples_leaf=20, random_state=1, n_jobs=-1)}
    for name, m in models.items():
        m.fit(X[tr], y[tr])
        p_tr, p_te = m.predict_proba(X[tr])[:, 1], m.predict_proba(X[te])[:, 1]
        print(f"\n== {name}")
        print(f"   in-sample accuracy on build: {((p_tr > 0.5) == y[tr]).mean():.2f} (base rate {y[tr].mean():.2f})   unseen accuracy: {((p_te > 0.5) == y[te]).mean():.2f} (base rate {y[te].mean():.2f})")
        for keep in (0.7, 0.5, 0.3):
            thr = np.quantile(p_te, 1 - keep)
            sel = p_te >= thr
            rr = r[te][sel]
            # luck range: 500 random subsets of the same size
            sims = np.array([r[te][rng.random(te.sum()) < keep].mean() for _ in range(500)])
            pct = (sims >= rr.mean()).mean() * 100
            print(f"   keep top {int(keep * 100):2d}% by model: n={sel.sum():4d} avgR={rr.mean():+.3f} win={100 * (rr > 0).mean():4.1f}%  | random subsets avgR {sims.mean():+.3f} (5-95%: {np.quantile(sims, .05):+.3f}..{np.quantile(sims, .95):+.3f}) | {pct:.0f}% of random subsets did as well")


if __name__ == "__main__":
    sys.exit(main())
