"""Download data, then: train period, unseen test period, and coin-flip test."""
import sys
import numpy as np
import yaml
from bot import data, backtest
from bot.research_log import log

cfg = yaml.safe_load(open("config.yaml"))
if "--download" in sys.argv:
    data.download(cfg)

SPLIT = "2025-06-01"   # before = build/tune, after = never touched
sb = cfg["risk"]["start_balance"]
n = 0
for name, kw in [("TRAIN", {"end": SPLIT}), ("TEST (unseen)", {"start": SPLIT})]:
    tr, eq = backtest.run(cfg, **kw)
    st = backtest.stats(tr, sb)
    print(name, st)
    n = log({"idea": "ema-trend breakout", "period": name, "stats": st})
real, _ = backtest.run(cfg, start=SPLIT)
real_net = real.pnl.sum() if len(real) else 0
beat = [backtest.run(cfg, start=SPLIT, random_entries=True, seed=i)[0].pnl.sum() for i in range(50)]
pct = np.mean([b >= real_net for b in beat]) * 100
print(f"Coin-flip: {pct:.0f}% of 50 random-entry versions did as well or better (want < 5%)")
print("Variants logged so far:", n)
