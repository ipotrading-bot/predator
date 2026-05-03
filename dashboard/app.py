"""
dashboard/app.py — Dashboard Streamlit d'audit PAIM
CLV Index · Brier Score · Equity Curve · Résultats temps réel
"""
import time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from config import settings
from data.supabase_client import SupabaseClient

# ── Page config ───────────────────────────────────────────────────

st.set_page_config(
    page_title="Predator PAIM — Dashboard",
    page_icon="🦅",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Auto-refresh toutes les 60 secondes
st_autorefresh(interval=60_000, key="auto_refresh")

# ── CSS custom ────────────────────────────────────────────────────
st.markdown("""
<style>
  body { background: #0a0e1a; }
  .metric-card {
    background: #111827;
    border: 1px solid #1f2937;
    border-radius: 12px;
    padding: 1.2rem 1.5rem;
    text-align: center;
  }
  .metric-value { font-size: 2rem; font-weight: 700; }
  .metric-label { font-size: 0.8rem; color: #6b7280; text-transform: uppercase; letter-spacing: .08em; }
  .positive { color: #10b981; }
  .negative { color: #ef4444; }
  .neutral  { color: #f59e0b; }
  .badge { display:inline-block; padding:2px 10px; border-radius:99px; font-size:0.75rem; font-weight:600; }
  .badge-active { background:#064e3b; color:#34d399; }
  .badge-killed { background:#7f1d1d; color:#fca5a5; }
</style>
""", unsafe_allow_html=True)

# ── Header ────────────────────────────────────────────────────────
col_logo, col_title, col_status = st.columns([1, 6, 2])
with col_title:
    st.markdown("## 🦅 PREDATOR PAIM")
    st.caption("Algorithmic Information Arbitrage — Dashboard d'Audit")

# ── Data loading ──────────────────────────────────────────────────
@st.cache_data(ttl=60)
def load_data():
    db = SupabaseClient()
    perf = db.get_performance_summary()
    equity = db.get_equity_curve()
    return perf, equity

perf, equity_data = load_data()

# ── KPI Row ───────────────────────────────────────────────────────
st.markdown("### 📊 Métriques Clés")
k1, k2, k3, k4, k5 = st.columns(5)

with k1:
    total_profit = perf.get("total_profit", 0)
    pclass = "positive" if total_profit >= 0 else "negative"
    st.markdown(f"""<div class="metric-card">
        <div class="metric-value {pclass}">{total_profit:+.0f}€</div>
        <div class="metric-label">Profit Net</div>
    </div>""", unsafe_allow_html=True)

with k2:
    roi = (total_profit / settings.starting_bankroll) * 100
    rclass = "positive" if roi >= 0 else "negative"
    st.markdown(f"""<div class="metric-card">
        <div class="metric-value {rclass}">{roi:+.1f}%</div>
        <div class="metric-label">ROI</div>
    </div>""", unsafe_allow_html=True)

with k3:
    clv = perf.get("clv_avg", 0) * 100
    cclass = "positive" if clv >= 5 else "neutral"
    st.markdown(f"""<div class="metric-card">
        <div class="metric-value {cclass}">{clv:.2f}%</div>
        <div class="metric-label">CLV Moyen</div>
    </div>""", unsafe_allow_html=True)

with k4:
    wr = perf.get("win_rate", 0) * 100
    wclass = "positive" if wr >= 55 else "neutral"
    st.markdown(f"""<div class="metric-card">
        <div class="metric-value {wclass}">{wr:.1f}%</div>
        <div class="metric-label">Win Rate</div>
    </div>""", unsafe_allow_html=True)

with k5:
    total_bets = perf.get("total_bets", 0)
    st.markdown(f"""<div class="metric-card">
        <div class="metric-value neutral">{total_bets}</div>
        <div class="metric-label">Paris Total</div>
    </div>""", unsafe_allow_html=True)

st.divider()

# ── Equity Curve ──────────────────────────────────────────────────
st.markdown("### 📈 Equity Curve")

if equity_data:
    df_eq = pd.DataFrame(equity_data)
    df_eq["timestamp"] = pd.to_datetime(df_eq["timestamp"])
    df_eq = df_eq.sort_values("timestamp")

    baseline = settings.starting_bankroll
    fig = go.Figure()

    fig.add_hline(y=baseline, line_dash="dot", line_color="#374151",
                  annotation_text=f"Capital initial: {baseline:,.0f}€",
                  annotation_position="bottom right")

    fig.add_trace(go.Scatter(
        x=df_eq["timestamp"],
        y=df_eq["balance"],
        mode="lines",
        name="Bankroll",
        line=dict(color="#10b981", width=2.5),
        fill="tozeroy",
        fillcolor="rgba(16, 185, 129, 0.07)",
    ))

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0a0e1a",
        plot_bgcolor="#111827",
        height=320,
        margin=dict(l=10, r=10, t=20, b=10),
        yaxis=dict(tickformat=",.0f", ticksuffix="€"),
        showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("En attente des premières données d'audit...")

st.divider()

# ── Tableau des signaux récents ───────────────────────────────────
st.markdown("### 📋 Derniers Signaux")

@st.cache_data(ttl=30)
def load_signals():
    db = SupabaseClient()
    try:
        response = (
            db._client.table("signals")
            .select("*")
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        )
        return response.data or []
    except Exception:
        return []

signals = load_signals()

if signals:
    df = pd.DataFrame(signals)
    display_cols = [
        "event_name", "sport", "selection", "ev_plus",
        "snr_ratio", "recommended_stake", "clv_estimate",
        "outcome", "profit_eur", "status"
    ]
    df = df[[c for c in display_cols if c in df.columns]]

    def color_outcome(val):
        if val == 1:
            return "color: #10b981"
        elif val == 0:
            return "color: #ef4444"
        return "color: #6b7280"

    def format_pct(val):
        if pd.isna(val):
            return "—"
        return f"{val:.2%}"

    fmt = {c: format_pct for c in ["ev_plus", "clv_estimate"]}
    st.dataframe(
        df.style.map(color_outcome, subset=["outcome"] if "outcome" in df.columns else [])
          .format(fmt),
        use_container_width=True,
        height=420,
    )
else:
    st.info("Aucun signal enregistré pour le moment.")

# ── Footer ────────────────────────────────────────────────────────
st.markdown("---")
st.caption(
    f"🔄 Actualisation auto: 60s · "
    f"⏱ {time.strftime('%H:%M:%S')} · "
    f"🦅 Predator PAIM v1.0"
)
