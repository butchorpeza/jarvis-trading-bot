"""Validated data contracts passed between JARVIS modules."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Candidate(BaseModel):
    symbol: str
    last_close: float
    last_bar_time: datetime
    avg_volume: float
    rel_volume: float          # last volume / average volume
    atr: float
    ema20: float
    ema50: float
    rsi: float
    volatility_pct: float      # ATR as % of price
    last_open: float = 0.0
    last_high: float = 0.0
    last_low: float = 0.0
    stale: bool = False
    stale_reason: str = ""


class Analysis(BaseModel):
    symbol: str
    trend: Literal["UP", "DOWN", "SIDEWAYS"]
    volume_confirms: bool
    catalyst: str
    red_flags: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    decision: Literal["BUY", "SELL", "HOLD", "AVOID"]
    confidence: Literal["LOW", "MEDIUM", "HIGH"]
    reason: str
    entry_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None


class RiskDecision(BaseModel):
    approved: bool
    quantity: float = 0
    risk_dollars: float = 0.0
    reasons: list[str] = Field(default_factory=list)


class TradePlan(BaseModel):
    symbol: str
    decision: Literal["BUY", "SELL", "HOLD", "AVOID"]
    entry_price: float | None = None
    quantity: float = 0
    stop_price: float | None = None
    target_price: float | None = None
    risk_dollars: float = 0.0
    confidence: Literal["LOW", "MEDIUM", "HIGH"]
    reason: str
    risk_approved: bool = False
