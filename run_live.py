"""Live loop for MT5. Dry run by default: no orders unless --go is passed."""
import sys
import time
from datetime import datetime
import yaml
from bot.indicators import add_indicators
from bot.decision import Decider
from bot.risk import RiskManager
from bot.broker import MT5Broker

cfg = yaml.safe_load(open("config.yaml"))
if cfg["broker"] != "mt5":
    sys.exit("Set broker: mt5 in config.yaml (DEMO account first). Use validate.py for backtests.")
DRY = "--go" not in sys.argv
b = MT5Broker(cfg)
dec = Decider(cfg["decision"], cfg["laya_min_confidence"])
risk = RiskManager(cfg["risk"])
r = cfg["risk"]
print("DRY RUN (no orders)" if DRY else "LIVE ORDERS ENABLED")
last = {}
while True:
    risk.new_bar(datetime.now(), b.equity())
    for s, sp in cfg["symbols"].items():
        df = add_indicators(b.candles(s, cfg["timeframe"], 300)).iloc[:-1]  # closed bars only
        row = df.iloc[-1]
        if last.get(s) == df.index[-1]:
            continue
        last[s] = df.index[-1]
        sig, conf = dec.decide(s, row)
        print(df.index[-1], s, sig, round(conf, 2))
        if sig != "flat" and risk.can_open(b.equity(), b.n_open()):
            sgn = 1 if sig == "buy" else -1
            px, atr = row["close"], row["atr"]
            lots = risk.lots(b.equity(), atr * r["sl_atr"], sp["contract"])
            sl, tp = px - sgn * atr * r["sl_atr"], px + sgn * atr * r["tp_atr"]
            if DRY:
                print("  would open", sig, lots, "SL", sl, "TP", tp)
            else:
                b.open(s, sig, lots, sl, tp)
    time.sleep(30)
