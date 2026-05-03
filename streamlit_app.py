import streamlit as st
from ui.dashboard import render_dashboard

st.set_page_config(page_title="PREDATOR PAIM", layout="wide")

# Running the dashboard
render_dashboard()
