"""Decision layer. Returns (action, confidence): action in {"buy","sell","flat"}.
Rules always run first; LAYA (optional) acts as a filter on the rule signal."""
import pandas as pd


def rule_signal(row: pd.Series) -> str:
    if pd.isna(row["ema200"]) or pd.isna(row["hh20"]):
        return "flat"
    up = row["ema20"] > row["ema50"] > row["ema200"]
    dn = row["ema20"] < row["ema50"] < row["ema200"]
    if up and row["close"] > row["hh20"] and row["rsi"] < 75:
        return "buy"
    if dn and row["close"] < row["ll20"] and row["rsi"] > 25:
        return "sell"
    return "flat"


class Decider:
    def __init__(self, mode: str = "rules", min_conf: float = 0.8):
        self.mode, self.min_conf, self.agent = mode, min_conf, None
        if mode == "laya":
            import laya  # pip install laya
            self.agent = laya.load("convaiinnovations/laya", device="cuda")

    def decide(self, symbol: str, row: pd.Series):
        sig = rule_signal(row)
        if sig == "flat" or self.agent is None:
            return sig, 1.0
        state = {
            "symbol": symbol, "signal": sig, "close": float(row["close"]),
            "ema20": float(row["ema20"]), "ema50": float(row["ema50"]),
            "ema200": float(row["ema200"]), "rsi": float(row["rsi"]),
            "atr": float(row["atr"]),
        }
        questions = {"take_trade": {
            "type": "noul",
            "instructions": f"Is this {sig} breakout in the direction of the trend, with acceptable momentum?",
        }}
        res = self.agent.predict(state, questions)["answers"]["take_trade"]
        n = res.get("noul") if isinstance(res, dict) else None
        p = float(n) if isinstance(n, (int, float)) else float(n.get("true", 0)) if isinstance(n, dict) else 0.0
        return (sig, p) if p >= self.min_conf else ("flat", p)
