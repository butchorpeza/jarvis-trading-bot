"""Risk Manager: hard-coded rules. The Analyst (or any LLM) cannot override these."""
import json
import math
import os

from .config import Settings
from .models import Analysis, Candidate, RiskDecision


class RiskManager:
    def __init__(self, cfg: Settings):
        self.cfg = cfg

    # --- drawdown state: peak equity persisted between runs ---
    def _update_peak(self, equity: float) -> float:
        path = self.cfg.state_path
        peak, day, day_start = equity, None, equity
        week, week_start, month, month_start = None, equity, None, equity
        if os.path.exists(path):
            try:
                st = json.load(open(path))
                peak = max(st["peak_equity"], equity)
                day, day_start = st.get("day"), st.get("day_start", equity)
                week, week_start = st.get("week"), st.get("week_start", equity)
                month, month_start = st.get("month"), st.get("month_start", equity)
            except (OSError, ValueError, KeyError):
                peak = equity
        today = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date().isoformat()
        if day != today:
            day_start = equity                                  # first look of a new day
            if self.cfg.drawdown_reset_daily:
                peak = equity                                   # ...and the drawdown peak restarts, so a pause cannot last forever
        self.day_start = day_start
        iso = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isocalendar()
        this_week, this_month = f"{iso[0]}-W{iso[1]:02d}", today[:7]
        if week != this_week:
            week_start = equity                                 # first look of a new week
        if month != this_month:
            month_start = equity                                # first look of a new month
        self.week_start, self.month_start = week_start, month_start
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        json.dump({"peak_equity": peak, "day": today, "day_start": day_start, "week": this_week, "week_start": week_start,
                   "month": this_month, "month_start": month_start}, open(path, "w"))
        return peak

    def check(self, a: Analysis, c: Candidate, equity: float, open_symbols: set[str],
              submitted_today: set[str], persist_state: bool = True, trades_today: dict | None = None,
              margin_per_unit: float | None = None, min_units: float | None = None,
              spread_price: float | None = None) -> RiskDecision:
        cfg, why = self.cfg, []
        if a.decision not in ("BUY", "SELL"):
            return RiskDecision(approved=False, reasons=[f"decision is {a.decision}, nothing to approve"])
        if c.stale:
            why.append(f"stale data blocks order ({c.stale_reason})")
        wrong_side = (a.entry_price is not None and a.stop_price is not None and
                      (a.stop_price >= a.entry_price if a.decision == "BUY" else a.stop_price <= a.entry_price))
        if a.entry_price is None or a.stop_price is None or wrong_side:
            why.append("missing or invalid invalidation (stop) level")
        if equity <= 0:
            why.append("invalid account equity")
        if a.symbol in open_symbols:
            why.append("already holding symbol: no averaging down / stacking")
        if a.symbol in submitted_today:
            why.append("cooldown: this symbol was traded very recently")
        if trades_today and trades_today.get(a.symbol, 0) >= cfg.max_trades_per_symbol_day:
            why.append(f"max {cfg.max_trades_per_symbol_day} trades per symbol per day reached")
        if len(open_symbols) >= cfg.max_open_positions:
            why.append(f"max {cfg.max_open_positions} open positions reached")
        if cfg.drop_guard and a.decision == "BUY":
            from .dropguard import halt_active
            halt = halt_active()
            if halt:
                why.append(f"market drop guard: new BUYs paused ({halt.get('reason', 'market falling')})")
        self.day_start = self.week_start = self.month_start = equity
        peak = self._update_peak(equity) if persist_state and equity > 0 else equity
        if cfg.bank_profit and self.day_start > 0 and equity >= self.day_start * (1 + cfg.daily_profit_target):
            why.append(f"daily profit target (+{cfg.daily_profit_target:.0%}) reached: banking the day, no new trades")
        if self.day_start > 0 and equity < self.day_start * (1 - cfg.max_daily_loss):
            why.append(f"daily loss limit: equity is >{cfg.max_daily_loss:.0%} below today's start")
        if self.week_start > 0 and equity < self.week_start * (1 - cfg.max_weekly_loss):
            why.append(f"weekly loss limit: equity is >{cfg.max_weekly_loss:.0%} below this week's start")
        if self.month_start > 0 and equity < self.month_start * (1 - cfg.max_monthly_loss):
            why.append(f"monthly loss limit: equity is >{cfg.max_monthly_loss:.0%} below this month's start")
        if peak > 0 and equity < peak * (1 - cfg.drawdown_pause):
            why.append(f"drawdown pause: equity {equity:,.0f} is >{cfg.drawdown_pause:.0%} below peak {peak:,.0f}")

        if (spread_price and a.entry_price is not None and a.stop_price is not None
                and spread_price > cfg.max_spread_frac * abs(a.entry_price - a.stop_price)):
            why.append(f"spread {spread_price:g} is more than {cfg.max_spread_frac:.0%} of the stop distance: trade would start too far behind")

        qty, risk_dollars = 0, 0.0
        if not why:
            risk_per_share = abs(a.entry_price - a.stop_price)
            by_risk = equity * cfg.max_risk_per_trade / risk_per_share
            if margin_per_unit:        # leveraged CFD: the limit is margin used, not the full contract value
                qty = round(min(by_risk, equity * cfg.max_margin_pct / margin_per_unit), 6)
                floor_units = min_units or 0
                if qty < floor_units:
                    min_risk = floor_units * risk_per_share
                    why.append(f"smallest allowed trade ({floor_units:g} units) would risk {min_risk / equity:.2%} of equity "
                               f"or need too much margin; limit is {cfg.max_risk_per_trade:.0%} risk / {cfg.max_margin_pct:.0%} margin")
                    qty = 0
            else:
                qty = math.floor(min(by_risk, equity * cfg.max_position_pct / a.entry_price))
                if qty < 1:
                    why.append("position size rounds to 0 shares under risk limits")
            if qty > 0:
                risk_dollars = round(qty * risk_per_share, 2)
        if why:
            return RiskDecision(approved=False, reasons=why)
        return RiskDecision(approved=True, quantity=qty, risk_dollars=risk_dollars, reasons=["all risk rules passed"])
