"""Live, READ-ONLY feed for the dashboard: account, open positions and watchlist quotes every few seconds.
Places no orders. Stop with Ctrl+C or by creating data/FEED_STOP.
  python live_feed.py                 (uses the .env watchlist, refreshes every 5 s)
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()
from jarvis.config import load_settings
from jarvis.mt5_backend import MT5Executor

OUT = "data/live_feed.json"
STOP = "data/FEED_STOP"


def write_atomic(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def performance(mt5, peak_path: str = "data/peak_equity.json") -> dict:
    """Closed-trade stats from MT5 deal history (last 90 days) + peak equity tracking. Read-only."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    deals = mt5.history_deals_get(now - timedelta(days=90), now + timedelta(days=1)) or []
    closed = []
    for d in deals:
        if d.entry != mt5.DEAL_ENTRY_OUT or d.type not in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL):
            continue
        closed.append({"symbol": d.symbol, "magic": d.magic, "time": datetime.fromtimestamp(d.time, timezone.utc).isoformat(timespec="seconds"),
                       "pnl": round(d.profit + d.commission + d.swap, 2), "volume": d.volume})
    by_symbol: dict = {}
    for c in closed:
        s = by_symbol.setdefault(c["symbol"], {"symbol": c["symbol"], "trades": 0, "wins": 0, "net": 0.0})
        s["trades"] += 1
        s["wins"] += 1 if c["pnl"] > 0 else 0
        s["net"] = round(s["net"] + c["pnl"], 2)
    board = sorted(by_symbol.values(), key=lambda x: x["net"], reverse=True)
    for b in board:
        b["win_rate"] = round(b["wins"] / b["trades"] * 100, 1) if b["trades"] else 0
    today = now.date().isoformat()
    today_pnl = round(sum(c["pnl"] for c in closed if c["time"].startswith(today)), 2)
    jarvis = [c for c in closed if c["magic"] == 424242]
    acct = mt5.account_info()
    peak = acct.equity
    try:
        peak = max(json.load(open(peak_path))["peak_equity"], acct.equity)
    except (OSError, ValueError, KeyError):
        pass
    write_atomic(peak_path, {"peak_equity": peak})
    return {"updated": now.isoformat(timespec="seconds"), "closed_trades": len(closed), "net_pnl_90d": round(sum(c["pnl"] for c in closed), 2),
            "today_closed_pnl": today_pnl, "jarvis_trades": len(jarvis), "jarvis_net": round(sum(c["pnl"] for c in jarvis), 2),
            "peak_equity": peak, "leaderboard": board, "recent": closed[-15:][::-1]}


def drop_actions(mt5, ex, cfg, state: dict, memo: dict, journal=None) -> list[str]:
    """While a drop halt is active: protect longs (winners to break-even) or close them, per cfg.drop_action.
    Runs at most every 20 s while active, and once more when the halt starts."""
    if not state.get("active") or cfg.drop_action == "pause":
        memo["last"] = 0.0
        return []
    if time.time() - memo.get("last", 0.0) < 20:
        return []
    memo["last"] = time.time()
    out = []
    for p in mt5.positions_get() or []:
        if p.type != mt5.POSITION_TYPE_BUY:
            continue                                            # shorts are left alone: a falling market suits them
        info, tick = mt5.symbol_info(p.symbol), mt5.symbol_info_tick(p.symbol)
        if info is None or tick is None:
            continue
        if cfg.drop_action == "close_longs":
            r = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": p.volume, "type": mt5.ORDER_TYPE_SELL,
                                "position": p.ticket, "price": tick.bid, "deviation": 20, "magic": 424242,
                                "comment": "drop-guard-close", "type_filling": ex._filling(info)})
            ok = r is not None and r.retcode == mt5.TRADE_RETCODE_DONE
            out.append(f"{'closed' if ok else 'FAILED to close'} long {p.symbol} {p.volume} lots, P&L {p.profit:+.2f} (market drop guard)")
        elif p.profit > 0:
            new_sl = round(p.price_open + info.spread * info.point, info.digits)
            min_dist = (info.trade_stops_level + info.spread) * info.point
            if (not p.sl or new_sl > p.sl) and new_sl < p.price_current - min_dist:
                r = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "position": p.ticket, "symbol": p.symbol, "sl": new_sl,
                                    "tp": p.tp, "magic": 424242})
                ok = r is not None and r.retcode == mt5.TRADE_RETCODE_DONE
                out.append(f"{'protected' if ok else 'FAILED to protect'} long {p.symbol}: stop to break-even {new_sl} (market drop guard)")
    for line in out:
        if journal is not None:
            journal.record("-", "MANAGE", "MANAGE", reason=line)
    return out


def bank_if_target(mt5, ex, cfg, journal=None) -> list[str]:
    """Runs every second: when the day's profit target is reached, close the winning positions at market.
    The day's starting equity comes from the risk manager's state file (only trusted if it is from today)."""
    if not cfg.bank_profit:
        return []
    try:
        st = json.load(open(cfg.state_path, encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if st.get("day") != datetime.now(timezone.utc).date().isoformat() or not st.get("day_start"):
        return []
    acct = mt5.account_info()
    if acct is None or acct.equity < st["day_start"] * (1 + cfg.daily_profit_target):
        return []
    out = []
    for p in mt5.positions_get() or []:
        if p.profit <= 0:
            continue
        info = mt5.symbol_info(p.symbol)
        tick = mt5.symbol_info_tick(p.symbol)
        if info is None or tick is None:
            continue
        buy = p.type == mt5.POSITION_TYPE_BUY
        r = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": p.volume,
                            "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY, "position": p.ticket,
                            "price": tick.bid if buy else tick.ask, "deviation": 20, "magic": 424242,
                            "comment": "bank-profit", "type_filling": ex._filling(info)})
        ok = r is not None and r.retcode == mt5.TRADE_RETCODE_DONE
        line = f"{'banked' if ok else 'FAILED to bank'} {p.symbol} {p.volume} lots, profit {p.profit:+.2f} (daily target reached)"
        out.append(line)
        if journal is not None:
            journal.record("-", "MANAGE", "MANAGE" if ok else "ERROR", reason=line)
    return out


def handle_commands(mt5, ex) -> None:
    """Dashboard buttons drop small JSON files in data/commands. Only two actions exist: close one position, or set its
    take-profit. The account is already verified as DEMO by the connection. Commands older than 2 minutes are ignored."""
    cdir, rdir = "data/commands", "data/command_results"
    if not os.path.isdir(cdir):
        return
    os.makedirs(rdir, exist_ok=True)
    for name in sorted(os.listdir(cdir)):
        path = os.path.join(cdir, name)
        try:
            cmd = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            cmd = None
        try:
            os.remove(path)
        except OSError:
            pass
        cid = name.replace(".json", "")
        res = {"ok": False, "message": "invalid command"}
        try:
            if cmd and time.time() - float(cmd.get("ts", 0)) < 120 and cmd.get("type") in ("close", "set_tp", "set_sl"):
                pos = next((p for p in (mt5.positions_get() or []) if p.ticket == int(cmd["ticket"])), None)
                info = mt5.symbol_info(pos.symbol) if pos else None
                if pos is None or info is None:
                    res = {"ok": False, "message": "position not found (already closed?)"}
                elif cmd["type"] == "close":
                    tick = mt5.symbol_info_tick(pos.symbol)
                    buy = pos.type == mt5.POSITION_TYPE_BUY
                    r = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol, "volume": pos.volume,
                                        "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY, "position": pos.ticket,
                                        "price": tick.bid if buy else tick.ask, "deviation": 20, "magic": 424242,
                                        "comment": "dashboard-take-profit", "type_filling": ex._filling(info)})
                    ok = r is not None and r.retcode == mt5.TRADE_RETCODE_DONE
                    res = {"ok": ok, "message": f"closed {pos.symbol} {pos.volume} lots, P&L {pos.profit:+.2f}" if ok
                           else f"broker refused: {getattr(r, 'retcode', None)} {getattr(r, 'comment', '')}"}
                elif cmd["type"] == "set_sl":
                    price = round(float(cmd["price"]), info.digits)
                    buy = pos.type == mt5.POSITION_TYPE_BUY
                    min_dist = (info.trade_stops_level + info.spread) * info.point
                    if (buy and price >= pos.price_current - min_dist) or (not buy and price <= pos.price_current + min_dist):
                        res = {"ok": False, "message": f"stop-loss must be {'below' if buy else 'above'} the current price {pos.price_current}"}
                    else:
                        r = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "position": pos.ticket, "symbol": pos.symbol,
                                            "sl": price, "tp": pos.tp, "magic": 424242})
                        ok = r is not None and r.retcode == mt5.TRADE_RETCODE_DONE
                        res = {"ok": ok, "message": f"{pos.symbol} stop-loss set to {price}" if ok
                               else f"broker refused: {getattr(r, 'retcode', None)} {getattr(r, 'comment', '')}"}
                else:
                    price = round(float(cmd["price"]), info.digits)
                    buy = pos.type == mt5.POSITION_TYPE_BUY
                    min_dist = (info.trade_stops_level + info.spread) * info.point
                    if (buy and price <= pos.price_current + min_dist) or (not buy and price >= pos.price_current - min_dist):
                        res = {"ok": False, "message": f"take-profit must be {'above' if buy else 'below'} the current price {pos.price_current}"}
                    else:
                        r = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "position": pos.ticket, "symbol": pos.symbol,
                                            "sl": pos.sl, "tp": price, "magic": 424242})
                        ok = r is not None and r.retcode == mt5.TRADE_RETCODE_DONE
                        res = {"ok": ok, "message": f"{pos.symbol} take-profit set to {price}" if ok
                               else f"broker refused: {getattr(r, 'retcode', None)} {getattr(r, 'comment', '')}"}
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "message": f"{type(e).__name__}: {e}"}
        write_atomic(os.path.join(rdir, cid + ".json"), {**res, "ts": time.time()})
        print("command", cid, res["message"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between looks at the account (default 1)")
    args = ap.parse_args()
    cfg = load_settings()
    os.makedirs("data", exist_ok=True)
    if os.path.exists(STOP):
        os.remove(STOP)
    try:
        ex = MT5Executor(cfg, dry_run=True)   # dry_run: this process can never send an order
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {type(e).__name__}: {e}")
        return 2
    mt5 = ex.mt5
    # tradable symbols for the dashboard's market picker (full trading allowed, grouped by MT5 path)
    tradable = [{"name": s.name, "group": s.path.split("\\")[0]} for s in (mt5.symbols_get() or [])
                if s.trade_mode == mt5.SYMBOL_TRADE_MODE_FULL and not s.path.startswith("Stocks")]
    write_atomic("data/symbols.json", {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                       "server": mt5.account_info().server, "symbols": tradable})
    for s in cfg.watchlist:
        mt5.symbol_select(s, True)
    print(f"Live feed started for {', '.join(cfg.watchlist)} every {args.interval}s. Stop: Ctrl+C or create {STOP}.")
    errors, last_perf = 0, 0.0
    from jarvis.dropguard import DropGuard
    guard, drop_memo, was_active = None, {}, False
    from jarvis.journal import Journal
    journal = Journal(cfg.log_path)
    try:
        while not os.path.exists(STOP):
            try:
                handle_commands(mt5, ex)
                quotes = []
                cfg = load_settings()          # picks up markets chosen on the dashboard
                for s in cfg.watchlist:
                    mt5.symbol_select(s, True)
                    t = mt5.symbol_info_tick(s)
                    info = mt5.symbol_info(s)
                    if t is None or info is None:
                        continue
                    quotes.append({"symbol": s, "bid": t.bid, "ask": t.ask, "spread_points": info.spread,
                                   "tick_time": datetime.fromtimestamp(t.time, timezone.utc).isoformat(timespec="seconds"),
                                   "digits": info.digits})
                write_atomic(OUT, {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                   "account": ex.account_snapshot(), "quotes": quotes})
                write_atomic("data/account_snapshot.json", ex.account_snapshot())
                if time.time() - last_perf >= 10:
                    write_atomic("data/performance.json", performance(mt5))
                    last_perf = time.time()
                for line in bank_if_target(mt5, ex, cfg, journal):
                    print(line)
                if cfg.drop_guard:
                    if guard is None or (guard.p1, guard.p5, guard.minutes) != (cfg.drop_1m_pct, cfg.drop_5m_pct, cfg.halt_minutes):
                        guard = DropGuard(cfg.drop_1m_pct, cfg.drop_5m_pct, cfg.halt_minutes)
                    st = guard.update([{"symbol": q["symbol"], "bid": q["bid"], "ask": q["ask"]} for q in quotes])
                    if st["active"] and not was_active:
                        print(f"MARKET DROP GUARD ON: {st['reason']}; new BUYs paused, action = {cfg.drop_action}")
                    was_active = st["active"]
                    for line in drop_actions(mt5, ex, cfg, st, drop_memo, journal):
                        print(line)
                errors = 0
            except Exception as e:  # noqa: BLE001
                errors += 1
                print(f"feed error {errors}: {type(e).__name__}: {e}", flush=True)
                if errors >= 3:                       # MT5 closed, restarted, or a new day logged it out: reconnect and carry on
                    from jarvis import brokerlink
                    try:
                        mt5.shutdown()
                        path = brokerlink.find_terminal()
                        if path and not brokerlink.terminal_running():
                            print("MT5 is closed: starting it again", flush=True)
                            brokerlink.start_terminal(path)
                        ok, msg, _ = brokerlink.ensure_connected(mt5, timeout=60)
                        print(f"reconnect: {msg}", flush=True)
                        if ok:
                            for s in cfg.watchlist:
                                mt5.symbol_select(s, True)
                            errors = 0
                    except Exception as e2:  # noqa: BLE001
                        print(f"reconnect failed: {type(e2).__name__}: {e2}", flush=True)
                    time.sleep(5)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    print("Live feed stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
