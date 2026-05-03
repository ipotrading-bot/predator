import streamlit as st
from ui.dashboard import render_dashboard

st.set_page_config(page_title="PREDATOR PAIM", layout="wide")

st.markdown("""
<link rel="manifest" href="/manifest.json">
""", unsafe_allow_html=True)

# Running the dashboard
render_dashboard()
