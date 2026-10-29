"""LAYA filter: a local classifier asked a yes/no question about a proposed BUY. Veto-only, like the Claude advisor.
Needs `pip install laya` (torch) and JARVIS_USE_LAYA=true. Runs on the GPU when CUDA is available.
LAYA is a general decision model, not trained on markets: treat it as an experiment and measure it."""
import os

from .models import Analysis, Candidate

MIN_P_TRUE = float(os.getenv("JARVIS_LAYA_MIN_P", "0.6"))


def enabled() -> bool:
    return os.getenv("JARVIS_USE_LAYA", "").strip().lower() == "true"


def _p_true(answer) -> float | None:
    """Pull P(true) out of LAYA's answer dict; shape verified against the installed version in tests."""
    if not isinstance(answer, dict):
        return None
    noul = answer.get("noul", answer)
    if isinstance(noul, dict):
        for k in ("true", "yes", "p_true", "True"):
            if k in noul:
                return float(noul[k])
    if isinstance(noul, (int, float)):
        return float(noul)
    return None


class LayaFilter:
    def __init__(self):
        import laya
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        try:
            self.agent = laya.load("convaiinnovations/laya", device=device)
        except TypeError:
            self.agent = laya.load("convaiinnovations/laya")
        self.device = device

    def check(self, a: Analysis, c: Candidate) -> tuple[bool, float | None, str]:
        """Returns (keep_trade, p_true, note). Any failure means keep_trade=False (fail closed)."""
        state = {"symbol": c.symbol, "proposed_trade": a.decision, "close": c.last_close, "ema20": c.ema20,
                 "ema50": c.ema50, "rsi": round(c.rsi, 1), "volatility_pct": round(c.volatility_pct, 2),
                 "relative_volume": round(c.rel_volume, 2), "rule_reason": a.reason}
        questions = {"take_trade": {
            "type": "noul",
            "instructions": "Is this a sound " + ("long" if a.decision == "BUY" else "short") + " entry: trend aligned, momentum not overextended, volume acceptable?"}}
        try:
            ans = self.agent.predict(state, questions)["answers"]["take_trade"]
            p = _p_true(ans)
        except Exception as e:  # noqa: BLE001
            return False, None, f"LAYA error, no trade: {type(e).__name__}"
        if p is None:
            return False, None, "LAYA returned no probability, no trade"
        return p >= MIN_P_TRUE, p, f"LAYA P(true)={p:.2f} (need {MIN_P_TRUE:.2f})"


def apply_filter(a: Analysis, keep: bool, note: str) -> Analysis:
    a.catalyst += f" | {note}"
    if not keep and a.decision in ("BUY", "SELL"):
        a.decision = "HOLD"
        a.red_flags.append("LAYA filter veto")
        a.reason = f"LAYA veto: {note} | {a.reason}"
        a.entry_price = a.stop_price = a.target_price = None
    return a
