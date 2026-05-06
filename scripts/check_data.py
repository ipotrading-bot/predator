from data.supabase_client import SupabaseClient
import asyncio

async def test_query():
    db = SupabaseClient()
    # Query signals table
    try:
        response = db._client.table("signals").select("*").limit(5).execute()
        print("Data retrieved:")
        for row in response.data:
            print(row)
    except Exception as e:
        print(f"Error querying signals: {e}")

if __name__ == "__main__":
    asyncio.run(test_query())
