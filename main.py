"""JARVIS entry point.
  python main.py --dry-run      analyze + risk-check + journal, submit ZERO orders (default)
  python main.py --sample       same pipeline on synthetic offline data (demo only, no keys needed)
  python main.py --paper-run    submit approved DEMO orders to MT5 (run this yourself, deliberately)
"""
import argparse
import dataclasses
import os
import sys

from jarvis.analyst import analyze
from jarvis.config import BROKERS, PaperModeError, load_settings
from jarvis.journal import Journal
from jarvis.models import TradePlan
from jarvis.sample_account import SampleAccount
from jarvis.risk_manager import RiskManager
from jarvis.scanner import SyntheticData, scan
from jarvis.signals import fetch_macro_signal
from jarvis import claude_advisor, events as events_mod, laya_filter
from jarvis.manager import PositionManager


_LAST_LOGGED: dict = {}


def should_log(symbol: str, decision: str, status: str, reason: str, notes: str, every: int = 900) -> bool:
    """Orders and errors are always logged. An unchanged HOLD / AVOID / REJECTED line is logged once per `every` seconds,
    so checking every second does not flood the journal."""
    import time as _t
    if status in ("SUBMITTED", "ERROR"):
        return True
    key = (decision, status, reason, notes)
    last = _LAST_LOGGED.get(symbol)
    if last and last[0] == key and _t.time() - last[1] < every:
        return False
    _LAST_LOGGED[symbol] = (key, _t.time())
    return True


def write_snapshot(snap: list[dict], source: str) -> None:
    """Own market view for the dashboard: trend breadth and regime across the watchlist (no external key needed)."""
    import json
    from datetime import datetime, timezone
    n = len(snap)
    up = sum(1 for s in snap if s["trend"] == "UP")
    down = sum(1 for s in snap if s["trend"] == "DOWN")
    regime = "unavailable" if n == 0 else "bullish" if up >= max(2, n * 2 / 3) else "defensive" if down >= max(2, n * 2 / 3) else "neutral"
    os.makedirs("data", exist_ok=True)
    json.dump({"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": source, "regime": regime,
               "up": up, "down": down, "total": n,
               "avg_volatility_pct": round(sum(s["volatility_pct"] for s in snap) / n, 2) if n else None,
               "symbols": snap}, open("data/market_snapshot.json", "w"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="JARVIS paper-trading research pipeline")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="submit no orders (default)")
    g.add_argument("--paper-run", action="store_true", help="submit approved DEMO orders")
    g.add_argument("--sample", action="store_true", help="synthetic offline data, never submits")
    ap.add_argument("--broker", choices=BROKERS, default=None,
                    help="mt5 (forex/indices/gold, DEMO account only)")
    ap.add_argument("--no-signal", action="store_true", help="ignore the local AI-Trader market signal")
    args = ap.parse_args(argv)
    paper_run = args.paper_run

    cfg = load_settings()
    broker = args.broker or os.getenv("JARVIS_BROKER", "mt5").strip().lower()
    if broker not in BROKERS:
        print(f"ERROR: unknown broker '{broker}'. Choose from {BROKERS}.")
        return 2
    if broker == "mt5":
        cfg = dataclasses.replace(cfg, min_avg_volume=0)  # MT5 tick volume is not comparable to share volume
    journal = Journal(cfg.log_path)
    risk = RiskManager(cfg)

    try:
        if args.sample:
            provider, ex = SyntheticData(), SampleAccount(cfg)
            print("SAMPLE MODE: synthetic prices, not real. Zero orders.")
        elif broker == "mt5":
            from jarvis.mt5_backend import MT5Data, MT5Executor
            ex = MT5Executor(cfg, dry_run=not paper_run)
            provider = MT5Data(cfg.timeframe)
            print("MT5 DEMO account confirmed.", "DEMO ORDERS ENABLED" if paper_run else "DRY RUN: zero orders.")
        equity, open_syms = ex.equity(), ex.open_symbols()
        if hasattr(ex, "account_snapshot"):
            import json
            os.makedirs("data", exist_ok=True)
            json.dump(ex.account_snapshot(), open("data/account_snapshot.json", "w"))
    except PaperModeError as e:
        print(f"ERROR: {e}")
        return 2
    except KeyError as e:
        print(f"ERROR: missing {e} in .env. Copy .env.example to .env and check the JARVIS_* settings.")
        return 2
    except Exception as e:  # API/auth failure blocks everything
        print(f"ERROR: could not reach {broker} ({type(e).__name__}: {e}). No orders placed.")
        return 2

    laya = claude = None
    try:
        if laya_filter.enabled():
            laya = laya_filter.LayaFilter()
            print(f"LAYA filter on ({laya.device}).")
        if claude_advisor.enabled():
            claude = claude_advisor.ClaudeAdvisor()
            print(f"Claude advisor on ({claude_advisor.MODEL}).")
    except Exception as e:  # an enabled-but-broken filter must stop the run, not be skipped silently
        print(f"ERROR: could not start an enabled filter ({type(e).__name__}: {e}). No orders placed.")
        return 2
    cal = events_mod.fetch_events()
    print(f"Economic calendar: {len(cal)} events this week" if cal else "Economic calendar unavailable (blackout skipped)")
    macro = fetch_macro_signal() if not args.no_signal else None
    print("Market signal:", macro["note"] if macro and macro["verdict"] != "unavailable" else "unavailable (not used)")
    cands, errors = scan(list(cfg.watchlist), provider, cfg)
    for s, msg in errors.items():
        journal.record(s, "AVOID", "ERROR", reason=msg)
    from jarvis.config import TF_SECONDS
    cooldown = 86400 if cfg.timeframe == "D1" else 4 * TF_SECONDS[cfg.timeframe]   # no second order on a symbol within 4 candles
    submitted = journal.submitted_since(cooldown)
    print(f"Timeframe {cfg.timeframe} (closed candles only); cooldown {cooldown // 60} min per symbol.")
    rows, snap = [], []
    cand_by, ana_by = {}, {}
    trades_today = journal.trades_today()
    for c in cands:
        a = analyze(c, cfg, macro)
        cand_by[c.symbol] = c
        bo = events_mod.blackout(c.symbol, cal)
        if bo:
            a = events_mod.apply_blackout(a, bo)
        snap.append({"symbol": c.symbol, "trend": a.trend, "rsi": round(c.rsi, 1), "volatility_pct": round(c.volatility_pct, 2),
                     "rel_volume": round(c.rel_volume, 2), "vs_ema50_pct": round((c.last_close / c.ema50 - 1) * 100, 2),
                     "stale": c.stale})
        if a.decision in ("BUY", "SELL") and laya:
            keep, _p, note = laya.check(a, c)
            a = laya_filter.apply_filter(a, keep, note)
        if a.decision in ("BUY", "SELL") and claude:
            a = claude_advisor.apply_verdict(a, claude.review(a, c, macro))
        ana_by[c.symbol] = a
        mpu = mn_units = spr = None
        if a.decision in ("BUY", "SELL") and hasattr(ex, "margin_per_unit"):
            mpu = ex.margin_per_unit(c.symbol, c.last_close, a.decision == "BUY")
            mn_units = ex.min_units(c.symbol)
            spr = ex.spread_price(c.symbol)
        rd = risk.check(a, c, equity, open_syms, submitted, persist_state=not args.sample, trades_today=trades_today,
                        margin_per_unit=mpu, min_units=mn_units, spread_price=spr)
        plan = TradePlan(symbol=a.symbol, decision=a.decision, entry_price=a.entry_price, quantity=rd.quantity,
                         stop_price=a.stop_price, target_price=a.target_price, risk_dollars=rd.risk_dollars,
                         confidence=a.confidence, reason=a.reason, risk_approved=rd.approved)
        status = "NO_ORDER"
        if a.decision in ("BUY", "SELL"):
            if rd.approved:
                try:
                    status = ex.submit(plan, submitted)
                    if status == "SUBMITTED":
                        submitted.add(a.symbol)
                except Exception as e:
                    status = "ERROR"
                    rd.reasons.append(f"{type(e).__name__}: {e}")
            else:
                status = "REJECTED"
        note_txt = "; ".join(rd.reasons + a.red_flags)
        if should_log(plan.symbol, plan.decision, status, plan.reason, note_txt):
            journal.record(plan.symbol, plan.decision, status, reason=plan.reason, confidence=plan.confidence,
                           entry_price=plan.entry_price, quantity=plan.quantity, stop_price=plan.stop_price,
                           target_price=plan.target_price, risk_dollars=plan.risk_dollars, notes=note_txt)
        rows.append((a.symbol, a.decision, a.confidence, "APPROVED" if rd.approved else ("n/a" if a.decision not in ("BUY", "SELL") else "BLOCKED"),
                     status, a.reason if rd.approved or a.decision not in ("BUY", "SELL") else f"{a.reason} | {'; '.join(rd.reasons)}"))

    if hasattr(ex, "mt5") and not args.sample:
        try:
            for line in PositionManager(ex, cfg, dry_run=ex.dry_run).run(cand_by, ana_by, cal, equity, day_start=getattr(risk, "day_start", None)):
                print("MANAGE:", line)
                journal.record("-", "MANAGE", "MANAGE" if not ex.dry_run else "MANAGE_DRY", reason=line)
        except Exception as e:  # noqa: BLE001  never let position care crash the pass
            print(f"MANAGE error: {type(e).__name__}: {e}")
    write_snapshot(snap, "sample" if args.sample else broker)
    print(f"\n{'SYMBOL':<12}{'DECISION':<9}{'CONF':<8}{'RISK':<9}{'STATUS':<9}REASON")
    for r in rows:
        print(f"{r[0]:<12}{r[1]:<9}{r[2]:<8}{r[3]:<9}{r[4]:<9}{r[5]}")
    for s, msg in errors.items():
        print(f"{s:<12}ERROR    {msg}")
    print(f"\nJournal: {cfg.log_path} | simulated/educational only, not financial advice.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
