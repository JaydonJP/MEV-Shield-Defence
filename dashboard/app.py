"""MEV Shield dashboard — live mempool risk labels, block ordering, alerts and the benchmark charts."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mevshield import config, store  # noqa: E402
from mevshield.devnet import status as devnet_status  # noqa: E402

st.set_page_config(page_title="MEV Shield", page_icon="🛡️", layout="wide")

SEV_COLOR = {"critical": "#e03131", "warning": "#f08c00", "info": "#2f9e44"}
LABEL_COLOR = {"CRITICAL": "#e03131", "SUSPICIOUS": "#f08c00", "BENIGN": "#2f9e44"}


@st.cache_data(ttl=2)
def load(table: str, limit: int = 500) -> pd.DataFrame:
    try:
        rows = store.query(f"SELECT * FROM {table} ORDER BY id DESC LIMIT {limit}")
    except Exception:
        rows = []
    return pd.DataFrame(rows)


@st.cache_data(ttl=10)
def latest_experiment(name: str):
    rows = store.query("SELECT data FROM experiments WHERE name=? ORDER BY id DESC LIMIT 1", (name,))
    return json.loads(rows[0]["data"]) if rows else None


st.title("🛡️ MEV Shield")
st.caption("Predict & prevent flash-loan and MEV attacks — live, on an isolated local devnet.")

s = devnet_status()
c1, c2, c3, c4 = st.columns(4)
c1.metric("main devnet", "up" if s["main"]["alive"] else "down")
c2.metric("shadow fork", "up" if s["shadow"]["alive"] else "down")
alerts = load("alerts")
c3.metric("alerts", len(alerts), f"{(alerts['label'] == 'CRITICAL').sum() if not alerts.empty else 0} critical")
c4.metric("model", "trained" if config.MODEL_FILE.exists() else "not trained")

tabs = st.tabs(["Live mempool & blocks", "Alerts", "Predictions", "Benchmark evidence"])

# ------------------------------------------------------------------ live
with tabs[0]:
    st.subheader("Recent blocks (ordered by priority fee)")
    blocks = load("blocks", 20)
    if blocks.empty:
        st.info("No blocks yet. Run `mevshield swap 500 --public` or `mevshield attack oracle`.")
    else:
        for _, b in blocks.head(8).iterrows():
            txs = json.loads(b["txs"])
            with st.expander(f"Block {b['number']} — {len(txs)} tx", expanded=False):
                df = pd.DataFrame(txs)
                if not df.empty:
                    df["pos"] = range(len(df))
                    show = df[["pos", "sender_role", "fn", "tip_gwei", "status", "private", "labels"]].copy()
                    show["labels"] = show["labels"].apply(lambda x: ",".join(x) if isinstance(x, list) else x)
                    show["status"] = show["status"].map({1: "✓", 0: "✗ revert"})
                    st.dataframe(show, hide_index=True, use_container_width=True)

    st.subheader("Live mempool risk (ML fast predictor)")
    if config.MODEL_FILE.exists() and s["main"]["alive"]:
        try:
            from mevshield.chain import Chain
            from mevshield.predictor import HarmPredictor
            chain = Chain()
            model = HarmPredictor.load()
            r0, r1 = chain.reserves("ammA")
            liq_usd = r1 / config.E
            pending = chain.txpool()
            from mevshield.harness.sandwich_bot import decode_swap
            rows = []
            for tx in pending:
                swp = decode_swap(chain, tx)
                if swp:
                    amt = swp["amount_in"] / config.E * (1 if swp["token_in"].lower() ==
                                                         chain.deployments["contracts"]["USDC"].lower() else r1 / r0)
                    pred = model.predict(amt, liq_usd, config.DEFAULT_SLIPPAGE, config.POOL_FEE_BPS, 1.0)
                    rows.append({"tx": tx["hash"][:12], "trade_usd": round(amt, 2),
                                 "predicted_harm_usd": round(pred["harm_usd"], 2),
                                 "p_attack": round(pred["p_attack"], 2)})
            if rows:
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            else:
                st.caption("No pending swaps in the mempool right now.")
        except Exception as exc:
            st.caption(f"(mempool read unavailable: {exc})")
    else:
        st.caption("Train the model (`mevshield train`) and start the devnet to see live risk labels.")

# ----------------------------------------------------------------- alerts
with tabs[1]:
    st.subheader("Guardian alerts")
    if alerts.empty:
        st.info("No alerts yet.")
    else:
        view = alerts.copy()
        view["when"] = pd.to_datetime(view["ts"], unit="s").dt.strftime("%H:%M:%S")
        st.dataframe(
            view[["when", "label", "severity", "sender", "score", "action", "latency_ms"]],
            hide_index=True, use_container_width=True,
            column_config={"latency_ms": st.column_config.NumberColumn("latency (ms)", format="%.0f")})
        counts = view["label"].value_counts().reset_index()
        counts.columns = ["label", "count"]
        fig = px.bar(counts, x="label", y="count", color="label", color_discrete_map=LABEL_COLOR)
        st.plotly_chart(fig, use_container_width=True)
        st.caption("Latest alert features")
        st.json(json.loads(view.iloc[0]["features"]))

# ------------------------------------------------------------- predictions
with tabs[2]:
    st.subheader("User-side predictions")
    preds = load("predictions")
    if preds.empty:
        st.info("No predictions yet. Run `mevshield predict 500`.")
    else:
        v = preds.copy()
        v["when"] = pd.to_datetime(v["ts"], unit="s").dt.strftime("%H:%M:%S")
        st.dataframe(v[["when", "amount", "harm_public", "attacker_profit", "closed_form_harm", "recommendation"]],
                     hide_index=True, use_container_width=True)
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=v["amount"], y=v["harm_public"], mode="markers", name="simulator harm"))
        fig.add_trace(go.Scatter(x=v["amount"], y=v["closed_form_harm"], mode="markers", name="closed-form harm"))
        fig.update_layout(xaxis_title="trade size (USDC)", yaxis_title="predicted harm (USD)",
                          xaxis_type="log", title="Risk Engine vs closed-form formula")
        st.plotly_chart(fig, use_container_width=True)

# -------------------------------------------------------------- benchmark
with tabs[3]:
    st.subheader("Prevention — harm vs trade size, by defense")
    results_path = config.RESULTS_CSV
    if results_path.exists():
        df = pd.read_csv(results_path)
        pivot = df.pivot_table(index="trade_usd", columns="strategy", values="harm_usd")
        order = [c for c in ["none", "public", "adaptive", "private-relay", "adaptive-slippage",
                             "commit-reveal", "split"] if c in pivot.columns]
        fig = px.line(pivot[order].reset_index(), x="trade_usd", y=order, markers=True,
                      labels={"value": "harm (USD)", "trade_usd": "trade size (USDC)", "variable": "strategy"})
        fig.update_xaxes(type="log")
        st.plotly_chart(fig, use_container_width=True)

        st.subheader("Regret vs best-in-hindsight (lower is better)")
        reg = df.groupby("strategy")["regret"].sum().reset_index().sort_values("regret")
        st.plotly_chart(px.bar(reg, x="strategy", y="regret", color="strategy"), use_container_width=True)
    else:
        st.info("Run `mevshield benchmark` to generate the evidence.")

    g = latest_experiment("guardian_eval")
    if g:
        st.subheader("Detection — Guardian on a mixed workload")
        m = g["metrics"]
        a, b, c, d = st.columns(4)
        a.metric("precision", m["precision"]); b.metric("recall", m["recall"])
        c.metric("false-positive rate", m["false_positive_rate"])
        d.metric("arb false positives", m["arbitrage_false_positives"])
        cm = pd.DataFrame([[m["tp"], m["fn"]], [m["fp"], m["tn"]]],
                          index=["attack (truth)", "benign (truth)"], columns=["flagged", "allowed"])
        st.plotly_chart(px.imshow(cm, text_auto=True, color_continuous_scale="Blues",
                                  title="confusion matrix"), use_container_width=True)

    p = latest_experiment("predictor_eval")
    if p:
        st.subheader("ML fast predictor vs simulator")
        pm = p["metrics"]
        a, b, c = st.columns(3)
        a.metric("MAE (USD)", pm["mae"]); b.metric("R²", pm["r2"])
        c.metric("speedup", f"{pm['speedup']:.0f}×")
        sdf = pd.DataFrame(p["samples"])
        fig = px.scatter(sdf, x="sim_harm", y="ml_harm", labels={"sim_harm": "simulator harm (USD)",
                                                                 "ml_harm": "ML harm (USD)"})
        lim = max(sdf["sim_harm"].max(), sdf["ml_harm"].max()) if not sdf.empty else 1
        fig.add_trace(go.Scatter(x=[0, lim], y=[0, lim], mode="lines", name="perfect", line=dict(dash="dash")))
        st.plotly_chart(fig, use_container_width=True)

st.caption("MEV Shield · course project · isolated local devnet · attacker code is a test harness only.")
if st.button("Refresh"):
    st.cache_data.clear()
    st.rerun()
