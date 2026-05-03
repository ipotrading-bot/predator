from api.supabase_client import get_supabase_client

def test_signals_table():
    supabase = get_supabase_client()
    try:
        # Try to query the table
        supabase.table("signals").select("*").limit(1).execute()
        print("Successfully connected to 'signals' table.")
    except Exception as e:
        print(f"Could not connect to 'signals' table: {e}")
        # Note: Cannot automatically create tables via the client without admin access
        print("Please ensure the 'signals' table exists in your Supabase project.")

if __name__ == "__main__":
    test_signals_table()
