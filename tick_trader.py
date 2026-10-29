"""TICK MODE: look at the market every second, on 1-second bars built from live MT5 ticks. EXPERIMENTAL, opt-in.
  python tick_trader.py                dry run: shows what it would do, sends nothing
  python tick_trader.py --paper-run    sends orders to the DEMO account
Kept on purpose: demo-only lock, spread guard, stop-loss + take-profit on every order, 1% risk, max open positions,
daily loss limit, per-symbol cooldown, kill switch (create data/KILL or press Ctrl+C).
Honest warning: on a 1-second scale the spread dwarfs the moves; my backtests lose money at fast speeds."""
import argparse
import dataclasses
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from jarvis import events as events_mod
from jarvis.analyst import analyze
from jarvis.config import load_settings
from jarvis.journal import Journal
from jarvis.manager import PositionManager
from jarvis.mt5_backend import MT5Executor
from jarvis.models import TradePlan
from jarvis.risk_manager import RiskManager
from jarvis.scanner import build_candidate

KILL = "data/KILL"
STATUS = "data/autopilot_status.json"
COOLDOWN_SECONDS = 30
MAX_TRADES_PER_SYMBOL_DAY = 100          # still finite; the daily loss limit stops everything first if it goes badly
STOP_SPREADS, TARGET_SPREADS = 8, 12      # stop/target are never closer than this many spreads: keeps the cost to ~12%


def second_bars(mt5, symbol: str, seconds: int = 900) -> pd.DataFrame | None:
    """1-second OHLC bars from bid ticks, CLOSED seconds only. Quiet seconds repeat the last price."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    now = tick.time
    ticks = mt5.copy_ticks_from(symbol, datetime.fromtimestamp(now - seconds, tz=timezone.utc), 100000, mt5.COPY_TICKS_ALL)
    if ticks is None or len(ticks) < 20:
        return None
    df = pd.DataFrame(ticks)
    df = df[df["bid"] > 0]
    df.index = pd.to_datetime(df["time"], unit="s", utc=True)
    o = df["bid"].resample("1s").ohlc().ffill()
    o["volume"] = df["bid"].resample("1s").count().reindex(o.index).fillna(0).astype(float)
    o = o.iloc[:-1]                                         # drop the second still in progress
    o.attrs["server_now"] = datetime.fromtimestamp(now, tz=timezone.utc)
    return o


def apply_floors(a, spread: float, cfg):
    """Never let the stop/target sit closer than a few spreads: otherwise the spread eats the whole trade."""
    if a.decision not in ("BUY", "SELL") or a.entry_price is None:
        return a
    sgn = 1 if a.decision == "BUY" else -1
    stop_d = max(abs(a.entry_price - a.stop_price), STOP_SPREADS * spread)
    tgt_d = max(abs(a.target_price - a.entry_price), TARGET_SPREADS * spread)
    a.stop_price = round(a.entry_price - sgn * stop_d, 5)
    a.target_price = round(a.entry_price + sgn * tgt_d, 5)
    return a


def write_status(**kw):
    os.makedirs("data", exist_ok=True)
    kw["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = STATUS + ".tmp"
    json.dump(kw, open(tmp, "w"))
    os.replace(tmp, STATUS)


def one_pass(ex, cfg, journal, risk, cal, equity, open_syms, state) -> list[str]:
    mt5, lines = ex.mt5, []
    cooldown_syms = journal.submitted_since(COOLDOWN_SECONDS)
    trades_today = journal.trades_today()
    cands, anas = {}, {}
    for sym in cfg.watchlist:
        df = second_bars(mt5, sym)
        if df is None:
            continue
        c = build_candidate(sym, df, cfg)
        a = analyze(c, cfg, None)
        cands[sym], anas[sym] = c, a
        if a.decision not in ("BUY", "SELL"):
            continue
        bo = events_mod.blackout(sym, cal)
        if bo:
            events_mod.apply_blackout(a, bo)
            continue
        spread = ex.spread_price(sym) or 0.0
        a = apply_floors(a, spread, cfg)
        rd = risk.check(a, c, equity, open_syms, cooldown_syms, trades_today=trades_today,
                        margin_per_unit=ex.margin_per_unit(sym, c.last_close, a.decision == "BUY"),
                        min_units=ex.min_units(sym), spread_price=spread)
        plan = TradePlan(symbol=sym, decision=a.decision, entry_price=a.entry_price, quantity=rd.quantity, stop_price=a.stop_price,
                         target_price=a.target_price, risk_dollars=rd.risk_dollars, confidence=a.confidence, reason=a.reason,
                         risk_approved=rd.approved)
        if ex.dry_run and rd.approved and time.time() - state["dry_cool"].get(sym, 0) < COOLDOWN_SECONDS:
            continue                                       # dry run: act as if the order went out, same cooldown as live
        status = "REJECTED"
        if rd.approved:
            try:
                status = ex.submit(plan, cooldown_syms)
                if status == "SUBMITTED":
                    open_syms.add(sym)
                    cooldown_syms.add(sym)
            except Exception as e:  # noqa: BLE001
                status = "ERROR"
                rd.reasons.append(f"{type(e).__name__}: {e}")
        if status != "REJECTED" or state["last_reject"].get(sym) != tuple(rd.reasons):   # do not log the same rejection every second
            journal.record(sym, a.decision, status, reason=f"[tick] {a.reason}", confidence=a.confidence, entry_price=a.entry_price,
                           quantity=rd.quantity, stop_price=a.stop_price, target_price=a.target_price, risk_dollars=rd.risk_dollars,
                           notes="; ".join(rd.reasons))
            state["last_reject"][sym] = tuple(rd.reasons)
            if ex.dry_run and rd.approved:
                state["dry_cool"][sym] = time.time()
            lines.append(f"{sym} {a.decision} -> {status} {'; '.join(rd.reasons)[:120]}")
    if open_syms:
        for line in PositionManager(ex, cfg, dry_run=ex.dry_run).run(cands, anas, cal, equity, day_start=getattr(risk, "day_start", None)):
            lines.append("MANAGE: " + line)
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper-run", action="store_true", help="send orders to the DEMO account")
    args = ap.parse_args()
    cfg0 = load_settings()
    cfg = dataclasses.replace(cfg0, timeframe="S1", min_avg_volume=0, max_trades_per_symbol_day=MAX_TRADES_PER_SYMBOL_DAY)
    try:
        ex = MT5Executor(cfg, dry_run=not args.paper_run)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {type(e).__name__}: {e}")
        return 2
    if os.path.exists(KILL):
        os.remove(KILL)
    mode = "tick-demo-orders" if args.paper_run else "tick-dry-run"
    journal, risk = Journal(cfg.log_path), RiskManager(cfg)
    print(f"TICK MODE ({mode}) on {', '.join(cfg.watchlist)}: one look per second. Kill: create {KILL} or Ctrl+C.")
    passes, errors, state, cal, cal_t = 0, 0, {"last_reject": {}, "dry_cool": {}}, [], 0.0
    try:
        while not os.path.exists(KILL):
            t0 = time.time()
            try:
                if time.time() - cal_t > 600:
                    cal, cal_t = events_mod.fetch_events(), time.time()
                    cfg0 = load_settings()
                    cfg = dataclasses.replace(cfg0, timeframe="S1", min_avg_volume=0, max_trades_per_symbol_day=MAX_TRADES_PER_SYMBOL_DAY)
                    risk.cfg = cfg
                equity = ex.equity()
                for line in one_pass(ex, cfg, journal, risk, cal, equity, ex.open_symbols(), state):
                    print(datetime.now().strftime("%H:%M:%S"), line, flush=True)
                errors = 0
            except Exception as e:  # noqa: BLE001
                errors += 1
                print(f"pass error {errors}: {type(e).__name__}: {e}", flush=True)
                if errors % 5 == 3:                    # MetaTrader 5 closed or logged out: reconnect instead of quitting
                    from jarvis import brokerlink
                    ok, msg = brokerlink.reconnect(ex.mt5, timeout=45)
                    print(f"reconnect: {msg}", flush=True)
                    if ok:
                        errors = 0
                        for s in cfg.watchlist:
                            ex.mt5.symbol_select(s, True)
                if errors >= 300:                      # about 5 minutes with nothing working
                    write_status(state="stopped: MT5 not reachable for 5 minutes", mode=mode, passes=passes, interval=1)
                    return 1
            passes += 1
            write_status(state="running", mode=mode, passes=passes, interval=1, last_pass=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         last_exit_code=0, consecutive_errors=errors)
            time.sleep(max(0.0, 1.0 - (time.time() - t0)))
    except KeyboardInterrupt:
        pass
    write_status(state="stopped: kill switch" if os.path.exists(KILL) else "stopped", mode=mode, passes=passes, interval=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
