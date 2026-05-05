"""
api/supabase_client.py
CORRECTIF : st.secrets → os.environ
"""
import os
from supabase import create_client, Client


def get_supabase_client() -> Client:
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        raise ValueError("SUPABASE_URL et SUPABASE_KEY requis dans les env vars")
    return create_client(url, key)


def get_history():
    supabase = get_supabase_client()
    return (supabase.table("signals")
            .select("*")
            .order("created_at", desc=True)
            .limit(50)
            .execute())