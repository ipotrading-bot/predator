import streamlit as st
import asyncio
import pandas as pd
from api.odds_client import fetch_odds
from core.paim_engine import process_market, select_top_signals
from api.supabase_client import get_history

st.set_page_config(page_title="PREDATOR PAIM", layout="wide")
st.title("PREDATOR PAIM - Dashboard")

async def main():
    if st.button("Refresh Signals"):
        with st.spinner("Fetching odds..."):
            # Example sport key
            data = await fetch_odds("soccer_epl")
            signals = []
            
            for match in data:
                # Basic parsing for Pinnacle/1XBet comparison
                # This depends heavily on API response structure
                pass
            
            # Filter and select
            top_signals = select_top_signals([s for s in signals if s['ev'] > 0.08])
            
            st.subheader("Top Signals (EV+ > 8%)")
            st.table(pd.DataFrame(top_signals))

    if st.checkbox("Show History"):
        history = get_history()
        st.subheader("Signal History")
        st.table(pd.DataFrame(history.data))

if __name__ == "__main__":
    asyncio.run(main())
