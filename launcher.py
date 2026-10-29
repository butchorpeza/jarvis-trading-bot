"""JARVIS app launcher: one double-click starts everything and opens the dashboard as an app window.
  1. start MetaTrader 5 if it is closed and wait until it is logged in to its saved account (demo only)
  2. on a new day (or a different broker) map the watchlist to that broker's symbol names
  3. start the live feed, dashboard API, dashboard UI and backtest page (skips anything already running)
  4. open the dashboard in its own window
It never types a password and never starts the autopilot: you press Start on the JARVIS page.
Run with --stop to shut the servers down (MT5 is left open)."""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
VAULT = os.path.dirname(ROOT)                                   # C:\Lourea
PY = os.environ.get("JARVIS_PYTHON") or sys.executable.replace("pythonw.exe", "python.exe")
API_PY = os.path.join(VAULT, "ai-trader", ".venv", "Scripts", "python.exe")
STATUS = os.path.join(ROOT, "data", "launcher_status.json")
STATE = os.path.join(ROOT, "data", "launcher_state.json")
LOG = os.path.join(ROOT, "logs", "launcher.log")
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
URL = "http://localhost:3000/jarvis"


def log(msg: str):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    open(LOG, "a", encoding="utf-8").write(line + "\n")
    print(line, flush=True)


def status(step: str, ok: bool | None, detail: str = ""):
    os.makedirs(os.path.dirname(STATUS), exist_ok=True)
    try:
        d = json.load(open(STATUS, encoding="utf-8"))
    except (OSError, ValueError):
        d = {"steps": {}}
    d["steps"][step] = {"ok": ok, "detail": detail}
    d["updated"] = datetime.now().isoformat(timespec="seconds")
    json.dump(d, open(STATUS, "w", encoding="utf-8"))
    log(f"[{step}] {'OK' if ok else 'WAIT' if ok is None else 'PROBLEM'} {detail}")


def popup(text: str, title: str = "JARVIS"):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)
    except Exception:  # noqa: BLE001
        pass


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_port(port: int, seconds: int) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if port_open(port):
            return True
        time.sleep(1)
    return False


def feed_fresh() -> bool:
    try:
        return time.time() - os.path.getmtime(os.path.join(ROOT, "data", "live_feed.json")) < 15
    except OSError:
        return False


def spawn(args, cwd, name):
    out = open(os.path.join(ROOT, "logs", f"{name}.log"), "a", encoding="utf-8")
    subprocess.Popen(args, cwd=cwd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=FLAGS)


def open_window():
    for exe in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
        if os.path.exists(exe):
            subprocess.Popen([exe, f"--app={URL}", "--window-size=1400,900"])        # a window without browser tabs: feels like an app
            return
    os.startfile(URL)


def stop_all():
    for name in ("KILL", "FEED_STOP"):
        open(os.path.join(ROOT, "data", name), "w").write("stop")
    log("Stop requested: autopilot and live feed will stop within seconds.")
    for port in (3000, 8000, 8501):
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if f"127.0.0.1:{port} " in line and "LISTENING" in line:
                subprocess.run(["taskkill", "/PID", line.split()[-1], "/T", "/F"], capture_output=True)
                log(f"stopped server on port {port}")
    log("Done. MetaTrader 5 was left open.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--no-window", action="store_true")
    ap.add_argument("--timeout", type=int, default=120, help="seconds to wait for MT5 to log in")
    args = ap.parse_args()
    if args.stop:
        stop_all()
        return 0
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    try:
        os.remove(STATUS)
    except OSError:
        pass
    for name in ("KILL", "FEED_STOP"):                          # leftovers from the last stop must not block this start
        try:
            os.remove(os.path.join(ROOT, "data", name))
        except OSError:
            pass

    # ---- 1. MetaTrader 5 ----
    from dotenv import load_dotenv
    load_dotenv()
    from jarvis import brokerlink
    import MetaTrader5 as mt5
    path = brokerlink.find_terminal()
    if path is None:
        status("mt5", False, "MetaTrader 5 not found. Install it, or set JARVIS_MT5_PATH in .env")
        return 2
    if brokerlink.terminal_running():
        status("mt5", None, "MetaTrader 5 already open, checking login")
    else:
        brokerlink.start_terminal(path)
        status("mt5", None, "MetaTrader 5 started, waiting for it to log in")
    ok, msg, info = brokerlink.ensure_connected(mt5, timeout=args.timeout)
    status("mt5", ok, msg)

    # ---- 2. new day / new broker: match the watchlist to this broker ----
    if ok:
        from jarvis.config import load_settings
        today = datetime.now().date().isoformat()
        try:
            st = json.load(open(STATE, encoding="utf-8"))
        except (OSError, ValueError):
            st = {}
        new_day, new_broker = st.get("date") != today, st.get("server") != info["server"]
        wanted = list(load_settings().watchlist)
        syms, notes = brokerlink.resolve_watchlist(mt5, wanted)
        if syms and syms != wanted:
            json.dump({"symbols": syms[:5]}, open(os.path.join(ROOT, "data", "selected_markets.json"), "w"))
            status("markets", True, "; ".join(notes) or f"watchlist set to {', '.join(syms)}")
        else:
            status("markets", True, f"watchlist OK on {info['server']}: {', '.join(wanted)}" + ("" if not notes else " | " + "; ".join(notes)))
        if new_day:
            log(f"New day ({today}): connection re-checked on {info['server']}")
        if new_broker and st.get("server"):
            log(f"Broker/account changed: {st.get('server')} -> {info['server']}")
        json.dump({"date": today, "server": info["server"]}, open(STATE, "w"))
        mt5.shutdown()                                          # the feed opens its own connection

    # ---- 3. servers (only what is not already running) ----
    if not feed_fresh() and ok:
        spawn([PY, "-u", "live_feed.py"], ROOT, "live_feed")
        status("feed", None, "live feed starting")
    else:
        status("feed", ok, "live feed already running" if ok else "not started: MT5 is not connected")
    if not port_open(8000):
        spawn([API_PY, os.path.join(VAULT, "ai-trader", "service", "server", "main.py")], VAULT, "api")
    if not port_open(3000):
        spawn(["cmd", "/c", "npm", "--prefix", os.path.join(VAULT, "ai-trader", "service", "frontend"), "run", "dev", "--", "--host", "127.0.0.1"], VAULT, "ui")
    if not port_open(8501):
        spawn([PY, "-m", "streamlit", "run", "dashboard.py", "--server.port", "8501", "--server.headless", "true", "--server.address", "127.0.0.1"], ROOT, "backtest")
    api_ok, ui_ok = wait_port(8000, 40), wait_port(3000, 60)
    status("dashboard", api_ok and ui_ok, "dashboard ready" if api_ok and ui_ok else f"API {'up' if api_ok else 'DOWN'}, UI {'up' if ui_ok else 'DOWN'} (see logs/api.log, logs/ui.log)")

    # ---- 4. open the app window ----
    if api_ok and ui_ok and not args.no_window:
        open_window()
    return 0 if (ok and api_ok and ui_ok) else 1


if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:  # noqa: BLE001
        log(f"launcher crashed: {type(e).__name__}: {e}")
        popup(f"JARVIS could not start: {type(e).__name__}: {e}\n\nDetails: logs\\launcher.log")
        code = 3
    if code in (1, 2) and "--stop" not in sys.argv:
        try:
            steps = json.load(open(STATUS, encoding="utf-8"))["steps"]
            problems = [f"{k}: {v['detail']}" for k, v in steps.items() if v["ok"] is False]
        except (OSError, ValueError, KeyError):
            problems = []
        popup("The dashboard opened, but something needs you:\n\n" + "\n\n".join(problems or ["see logs\\launcher.log"]))
    sys.exit(code)
