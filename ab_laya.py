"""A/B: same strategy with and without the LAYA veto, on train and unseen periods."""
import sys
import warnings
import yaml
warnings.filterwarnings("ignore")
from bot import backtest
from bot.decision import Decider

cfg = yaml.safe_load(open("config.yaml"))
sb = cfg["risk"]["start_balance"]
SPLIT = "2025-06-01"
thr = float(sys.argv[1]) if len(sys.argv) > 1 else cfg["laya_min_confidence"]
variants = {"rules only": Decider("rules"), f"rules + LAYA (P>={thr})": Decider("laya", thr)}
for name, dec in variants.items():
    for per, kw in [("TRAIN", {"end": SPLIT}), ("UNSEEN", {"start": SPLIT})]:
        tr, _ = backtest.run(cfg, decider=dec, **kw)
        print(f"{name:24s} {per:7s}", backtest.stats(tr, sb), flush=True)
