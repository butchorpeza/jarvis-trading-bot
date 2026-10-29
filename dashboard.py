"""Run: streamlit run dashboard.py"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yaml
from bot import backtest
from bot.indicators import add_indicators

st.set_page_config(page_title="Trading Bot", layout="wide")
cfg = yaml.safe_load(open("config.yaml"))
sb = cfg["risk"]["start_balance"]

st.sidebar.title("Trading Bot")
st.sidebar.caption(f"broker: {cfg['broker']} | decision: {cfg['decision']} | tf: {cfg['timeframe']}")
sym = st.sidebar.selectbox("Symbol", list(cfg["symbols"]))
split = st.sidebar.text_input("Unseen-period start", "2025-06-01")
if st.sidebar.button("Run backtest"):
    with st.spinner("Running..."):
        st.session_state["train"], _ = backtest.run(cfg, end=split)
        st.session_state["test"], _ = backtest.run(cfg, start=split)

tab1, tab2, tab3 = st.tabs(["Backtest", "Chart", "Research log"])

with tab1:
    if "test" not in st.session_state:
        st.info("Click 'Run backtest' in the sidebar.")
    else:
        c1, c2 = st.columns(2)
        for col, key, label in [(c1, "train", "Train"), (c2, "test", "Unseen test")]:
            tr = st.session_state[key]
            col.subheader(label)
            col.json(json.loads(json.dumps(backtest.stats(tr, sb), default=float)))
            if not tr.empty:
                curve = tr.sort_values("t").assign(equity=lambda d: sb + d.pnl.cumsum())
                fig = go.Figure(go.Scatter(x=curve.t, y=curve.equity, mode="lines"))
                fig.update_layout(height=280, margin=dict(l=0, r=0, t=10, b=0))
                col.plotly_chart(fig, use_container_width=True)
                col.dataframe(tr.groupby("sym").pnl.agg(["count", "sum"]).round(2))
        st.caption("Result must hold on the unseen test and beat random entries (run validate.py) before any live use.")

with tab2:
    path = f"data/{sym}.csv"
    if os.path.exists(path):
        df = add_indicators(pd.read_csv(path, index_col=0, parse_dates=True)).tail(300)
        fig = go.Figure(go.Candlestick(x=df.index, open=df.open, high=df.high, low=df.low, close=df.close, name=sym))
        for ma in ("ema20", "ema50", "ema200"):
            fig.add_trace(go.Scatter(x=df.index, y=df[ma], name=ma, line=dict(width=1)))
        fig.update_layout(height=600, xaxis_rangeslider_visible=False, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.warning("No data. Run: python validate.py --download")

with tab3:
    p = "logs/research_log.jsonl"
    if os.path.exists(p):
        rows = [json.loads(line) for line in open(p)]
        st.metric("Variants tested", len(rows))
        st.dataframe(pd.json_normalize(rows), use_container_width=True)
    else:
        st.write("Empty.")
