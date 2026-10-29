"""Position manager: looks after trades JARVIS already opened (magic 424242 only) on every pass.
  - break-even stop, trailing stop (ratchet: a stop only ever moves in the trade's favour)
  - partial profit taking
  - scale-in on WINNERS only (never averaging down), capped by the risk limit
  - exit when the trend flips, and protection against news spikes
All actions are demo-account only and logged. In dry-run mode nothing is sent: it only reports what it WOULD do."""
import json
import math
import os
from datetime import datetime, timezone

from .config import Settings
from .events import blackout
from .models import Analysis, Candidate

MAGIC = 424242


class PositionManager:
    def __init__(self, ex, cfg: Settings, dry_run: bool, state_path: str = "data/position_state.json"):
        self.ex, self.cfg, self.dry_run, self.state_path = ex, cfg, dry_run, state_path
        self.mt5 = ex.mt5
        try:
            self.state = json.load(open(state_path, encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}

    # ---------- helpers ----------
    def _save(self):
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        json.dump(self.state, open(self.state_path, "w", encoding="utf-8"))

    def _round_vol(self, info, vol: float) -> float:
        step = info.volume_step or 0.01
        return round(math.floor(vol / step + 1e-9) * step, 8)

    def _min_stop_distance(self, info) -> float:
        return (info.trade_stops_level + info.spread) * info.point

    def _modify_sl(self, p, new_sl: float, info, why: str, out: list):
        if (p.type == self.mt5.POSITION_TYPE_BUY and new_sl <= p.sl > 0) or (p.type == self.mt5.POSITION_TYPE_SELL and p.sl > 0 and new_sl >= p.sl):
            return                                          # ratchet: never loosen a stop
        price = p.price_current
        if abs(price - new_sl) < self._min_stop_distance(info):
            return                                          # too close for the broker
        new_sl = round(new_sl, info.digits)
        if self.dry_run:
            out.append(f"WOULD move {p.symbol} stop to {new_sl} ({why})")
            return
        res = self.mt5.order_send({"action": self.mt5.TRADE_ACTION_SLTP, "position": p.ticket, "symbol": p.symbol,
                                   "sl": new_sl, "tp": p.tp, "magic": MAGIC})
        ok = res is not None and res.retcode == self.mt5.TRADE_RETCODE_DONE
        out.append(f"{'moved' if ok else 'FAILED to move'} {p.symbol} stop to {new_sl} ({why})")

    def _close(self, p, volume: float, info, why: str, out: list):
        if self.dry_run:
            out.append(f"WOULD close {volume} lots of {p.symbol} ({why})")
            return
        mt5 = self.mt5
        tick = mt5.symbol_info_tick(p.symbol)
        buy = p.type == mt5.POSITION_TYPE_BUY
        res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": volume,
                              "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY, "position": p.ticket,
                              "price": tick.bid if buy else tick.ask, "deviation": 20, "magic": MAGIC,
                              "comment": "jarvis-manage", "type_filling": self.ex._filling(info)})
        ok = res is not None and res.retcode == mt5.TRADE_RETCODE_DONE
        out.append(f"{'closed' if ok else 'FAILED to close'} {volume} lots of {p.symbol} ({why})")

    def _add(self, p, volume: float, info, why: str, out: list):
        if self.dry_run:
            out.append(f"WOULD add {volume} lots to {p.symbol} ({why})")
            return
        mt5 = self.mt5
        tick = mt5.symbol_info_tick(p.symbol)
        buy = p.type == mt5.POSITION_TYPE_BUY
        res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": volume,
                              "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
                              "price": tick.ask if buy else tick.bid, "sl": p.sl, "tp": p.tp, "deviation": 20, "magic": MAGIC,
                              "comment": "jarvis-add", "type_time": mt5.ORDER_TIME_GTC, "type_filling": self.ex._filling(info)})
        ok = res is not None and res.retcode == mt5.TRADE_RETCODE_DONE
        out.append(f"{'added' if ok else 'FAILED to add'} {volume} lots to {p.symbol} ({why})")

    # ---------- main ----------
    def run(self, candidates: dict[str, Candidate], analyses: dict[str, Analysis], events: list, equity: float,
            now: datetime | None = None, day_start: float | None = None) -> list[str]:
        cfg, mt5, out = self.cfg, self.mt5, []
        now = now or datetime.now(timezone.utc)
        all_positions = list(mt5.positions_get() or [])
        if cfg.protect_all:
            for p in all_positions:                       # any position, even one opened by hand
                if p.sl and p.tp:
                    continue
                c0, info0 = candidates.get(p.symbol), mt5.symbol_info(p.symbol)
                if c0 is None or info0 is None or c0.atr <= 0:
                    continue
                sgn0 = 1 if p.type == mt5.POSITION_TYPE_BUY else -1
                ref = p.price_current
                new_sl = p.sl or round(ref - sgn0 * cfg.stop_atr * c0.atr, info0.digits)
                new_tp = p.tp or round(ref + sgn0 * cfg.target_atr * c0.atr, info0.digits)
                if self.dry_run:
                    out.append(f"WOULD protect {p.symbol}: stop {new_sl}, target {new_tp}")
                else:
                    r0 = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "position": p.ticket, "symbol": p.symbol,
                                         "sl": new_sl, "tp": new_tp, "magic": p.magic})
                    ok0 = r0 is not None and r0.retcode == mt5.TRADE_RETCODE_DONE
                    out.append(f"{'protected' if ok0 else 'FAILED to protect'} {p.symbol}: stop {new_sl}, target {new_tp}")
        positions = [p for p in all_positions if p.magic == MAGIC]
        live_tickets = {str(p.ticket) for p in positions}
        for t in list(self.state):
            if t not in live_tickets:
                del self.state[t]                           # position closed: forget it

        banking = bool(cfg.bank_profit and day_start and equity >= day_start * (1 + cfg.daily_profit_target))
        if banking:
            out.append(f"DAILY PROFIT TARGET reached (+{cfg.daily_profit_target:.0%}): closing winners to bank the profit")

        for p in positions:
            if banking and p.profit > 0:
                info0 = mt5.symbol_info(p.symbol)
                if info0 is not None:
                    self._close(p, p.volume, info0, f"bank profit {p.profit:+.2f}", out)
                    continue
            c, a = candidates.get(p.symbol), analyses.get(p.symbol)
            info = mt5.symbol_info(p.symbol)
            if c is None or a is None or info is None:
                out.append(f"{p.symbol}: not in the watchlist or no data, left alone")
                continue
            buy = p.type == mt5.POSITION_TYPE_BUY
            sgn = 1 if buy else -1
            st = self.state.setdefault(str(p.ticket), {
                "entry": p.price_open,
                "risk": abs(p.price_open - p.sl) if p.sl else cfg.stop_atr * c.atr,
                "volume": p.volume, "adds": 0, "partial": False})
            risk = max(st["risk"], 1e-9)
            price = p.price_current
            r_now = sgn * (price - st["entry"]) / risk
            tag = f"{r_now:+.1f}R"

            # 1) the trend flipped against the trade: get out
            if cfg.trend_exit and ((buy and a.trend == "DOWN") or (not buy and a.trend == "UP")):
                self._close(p, p.volume, info, f"trend flipped against the trade, {tag}", out)
                continue

            # 2) news spike: a huge candle against us = exit, a huge candle for us = lock profit tightly
            bar_range = c.last_high - c.last_low
            if cfg.spike_guard and c.atr > 0 and bar_range >= cfg.spike_mult * c.atr:
                against = (c.last_close < c.last_open) if buy else (c.last_close > c.last_open)
                if against:
                    self._close(p, p.volume, info, f"spike candle against the trade ({bar_range / c.atr:.1f} x ATR), {tag}", out)
                    continue
                self._modify_sl(p, price - sgn * 1.0 * c.atr, info, "spike in our favour: tight stop", out)

            # 3) scheduled high-impact news within 30 min: lock profit at break-even
            if r_now > 0 and blackout(p.symbol, events, now, before_hours=cfg.news_lock_minutes / 60, after_hours=0):
                self._modify_sl(p, st["entry"] + sgn * info.spread * info.point, info, "high-impact news soon: lock break-even", out)

            # 4) partial profit
            if cfg.partial_exit and not st["partial"] and r_now >= cfg.partial_at_r:
                vol = self._round_vol(info, p.volume * cfg.partial_frac)
                if vol >= info.volume_min and p.volume - vol >= info.volume_min:
                    self._close(p, vol, info, f"partial profit at {tag}", out)
                    if not self.dry_run:
                        st["partial"] = True
                else:
                    st["partial"] = True                    # too small to split: do not keep trying

            # 5) break-even
            if cfg.breakeven and r_now >= cfg.be_at_r:
                self._modify_sl(p, st["entry"] + sgn * info.spread * info.point, info, f"break-even at {tag}", out)

            # 6) trailing stop
            if cfg.trailing and r_now >= cfg.trail_start_r and c.atr > 0:
                self._modify_sl(p, price - sgn * cfg.trail_atr * c.atr, info, f"trail {cfg.trail_atr} x ATR at {tag}", out)

            # 7) add to a WINNER (stop already at/beyond break-even, trend intact, never to a loser)
            protected = p.sl > 0 and ((buy and p.sl >= st["entry"]) or (not buy and p.sl <= st["entry"]))
            if (cfg.scale_in and protected and st["adds"] < cfg.max_adds and r_now >= cfg.scale_in_at_r * (st["adds"] + 1)
                    and a.decision == ("BUY" if buy else "SELL") and not blackout(p.symbol, events, now)):
                dist = abs(price - p.sl)
                if dist > 0:
                    units = min(cfg.add_frac * st["volume"] * info.trade_contract_size, equity * cfg.max_risk_per_trade / dist)
                    vol = self._round_vol(info, units / info.trade_contract_size)
                    if vol >= info.volume_min:
                        self._add(p, vol, info, f"scale-in #{st['adds'] + 1} at {tag}", out)
                        if not self.dry_run:
                            st["adds"] += 1
        self._save()
        return out
