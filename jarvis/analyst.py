"""Analyst: deterministic first version. Optional LLM summaries would sit behind a separate interface
and could never change the decision's risk approval."""
from .config import Settings
from .models import Analysis, Candidate


def analyze(c: Candidate, cfg: Settings, macro: dict | None = None) -> Analysis:
    missing = [] if macro and macro.get("verdict") != "unavailable" else ["market signal unavailable"]
    flags: list[str] = []
    if c.stale:
        flags.append(f"stale data: {c.stale_reason}")
    if c.avg_volume < cfg.min_avg_volume:
        flags.append(f"low average volume ({c.avg_volume:,.0f})")
    if c.rsi > 75:
        flags.append(f"overbought RSI {c.rsi:.0f}")

    if c.last_close > c.ema20 > c.ema50:
        trend = "UP"
    elif c.last_close < c.ema20 < c.ema50:
        trend = "DOWN"
    else:
        trend = "SIDEWAYS"
    vol_ok = c.rel_volume >= 1.0

    if trend == "DOWN":
        if cfg.allow_short and not flags and c.rsi >= 25:
            decision = "SELL"
            reason = f"Downtrend (price < EMA20 < EMA50), RSI {c.rsi:.0f}, volume {c.rel_volume:.1f}x average."
        elif cfg.allow_short and c.rsi < 25:
            decision, reason = "AVOID", f"Downtrend but oversold (RSI {c.rsi:.0f}): too late to sell."
        elif flags:
            decision, reason = "AVOID", "Downtrend with red flags: " + "; ".join(flags)
        else:
            decision, reason = "AVOID", "Downtrend: price below EMA20 and EMA50 (shorting is off)."
    elif trend == "UP" and not flags:
        decision = "BUY"
        reason = f"Uptrend (price > EMA20 > EMA50), RSI {c.rsi:.0f}, volume {c.rel_volume:.1f}x average."
    elif flags:
        decision, reason = "HOLD", "Red flags: " + "; ".join(flags)
    else:
        decision, reason = "HOLD", "No clear trend."

    if decision == "BUY":
        confidence = "HIGH" if vol_ok and 50 <= c.rsi <= 70 else "MEDIUM" if vol_ok else "LOW"
    elif decision == "SELL":
        confidence = "HIGH" if vol_ok and 30 <= c.rsi <= 50 else "MEDIUM" if vol_ok else "LOW"
    else:
        confidence = "LOW" if flags else "MEDIUM"

    catalyst = "none known (no news source connected)"
    if macro and macro.get("verdict") != "unavailable":
        catalyst = macro["note"]
        if macro["verdict"] == "defensive" and decision == "BUY":
            decision = "HOLD"
            flags.append("market signal is defensive")
            reason = "Market signal is defensive: BUY downgraded to HOLD. " + reason
        elif macro["verdict"] == "bullish" and decision == "SELL":
            decision = "HOLD"
            flags.append("market signal is bullish")
            reason = "Market signal is bullish: SELL downgraded to HOLD. " + reason
        elif macro["verdict"] == "neutral" and decision in ("BUY", "SELL") and confidence == "HIGH":
            confidence = "MEDIUM"

    a = Analysis(symbol=c.symbol, trend=trend, volume_confirms=vol_ok,
                 catalyst=catalyst, red_flags=flags, missing_data=missing,
                 decision=decision, confidence=confidence, reason=reason)
    if decision in ("BUY", "SELL"):
        sgn = 1 if decision == "BUY" else -1
        a.entry_price = c.last_close
        a.stop_price = round(c.last_close - sgn * cfg.stop_atr * c.atr, 2)
        a.target_price = round(c.last_close + sgn * cfg.target_atr * c.atr, 2)
    return a
