import os
import sys
# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supabase import create_client
from config import settings

def test_supabase():
    print("Testing Supabase connection...")
    try:
        url = settings.supabase_url
        key = settings.supabase_key
        
        if not url or not key:
            print("Error: SUPABASE_URL or SUPABASE_KEY missing.")
            return

        db = create_client(url, key)
        
        # Test signals table
        print("Testing 'signals' table query...")
        signals = db.table("signals").select("*").limit(1).execute()
        print("Successfully queried 'signals' table.")
        
        # Test bankroll_snapshots table
        print("Testing 'bankroll_snapshots' table query...")
        snapshots = db.table("bankroll_snapshots").select("*").limit(1).execute()
        print("Successfully queried 'bankroll_snapshots' table.")
        
        print("Supabase connection and table access validated successfully.")
        
    except Exception as e:
        print(f"Supabase connection test failed: {e}")

if __name__ == "__main__":
    test_supabase()
