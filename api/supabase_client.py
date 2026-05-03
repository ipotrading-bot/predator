from supabase import create_client
import streamlit as st

def get_supabase_client():
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)

def get_history():
    supabase = get_supabase_client()
    # Assuming a table named 'signals_history'
    return supabase.table("signals_history").select("*").order("created_at", desc=True).limit(50).execute()
