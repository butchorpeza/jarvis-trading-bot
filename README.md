# Trading Bot

Two parts live in this folder:

1. **Backtester** (`validate.py`, `dashboard.py`, `bot/`): research only, uses free yfinance data.
2. **JARVIS** (`main.py`, `jarvis/`): beginner pipeline from the SebAI guide, now using **MetaTrader 5 (DEMO account only)**.

Educational and simulated. Not financial advice. Demo results differ from live results.

## JARVIS setup

Requires the MetaTrader 5 terminal (Windows), logged in to a **demo** account.

```
pip install -r requirements.txt
pip install MetaTrader5
```

`.env` (copy of `.env.example`) holds `JARVIS_MT5_DEMO=true` and your watchlist. Use your broker's exact symbol names (e.g. `EURUSD`, `XAUUSD`, `US100`, `US30`, or suffixed names like `XAUUSD.m`).

## Run

| Command | What it does |
|---|---|
| `python main.py --sample` | Demo on synthetic prices. No MT5, zero orders. |
| `python main.py --dry-run` | Real MT5 demo data, full analysis, zero orders (default). |
| `python main.py --paper-run` | Submits approved orders to the DEMO account. Run only on purpose. |
| `python -m pytest tests` | Safety tests. |

Pipeline: Scanner -> Analyst -> Risk Manager -> Executor -> Journal. Every decision (BUY/HOLD/AVOID, rejections, errors) goes to `logs/trades.csv`.

Position size is converted from units to lots using the symbol's contract size and volume step. Sizes below the broker minimum lot are rejected. Adding another broker means adding one backend with `daily_bars`, `equity`, `open_symbols`, `submit`.

## Hard rules (in code)

- `JARVIS_MT5_DEMO=true` required, and the logged-in MT5 account must report as DEMO; otherwise it refuses to run. No live switch exists.
- Max 1% risk per trade, 10% of account per position, 3 open positions, no averaging down.
- Stop level required; stale or missing data blocks orders; 5% drawdown from peak pauses new orders.
- Duplicate orders blocked (journal check + same-day symbol check).
- Watchlist limited to 5 symbols.

## Limitations

- The Analyst is rule-based (trend, volume, RSI). No news feed. MT5 volume is tick volume.
- Long-only (BUY); no short signals in version one.
- Two weeks of demo results is a software test, not proof of a profitable strategy.

## Troubleshooting

- `locked to DEMO accounts`: set `JARVIS_MT5_DEMO=true`.
- `MT5 terminal not reachable`: open MT5 and log in first.
- `not a DEMO account`: log in to a demo account.
- `unknown MT5 symbol` / no data: check the exact symbol name in MT5 Market Watch.
