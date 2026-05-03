import requests
from api.rate_limiter import rate_limiter

@rate_limiter
async def fetch_odds(sport_key):
    api_key = st.secrets["ODDS_API_KEY"]
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
    params = {
        "apiKey": api_key,
        "regions": "eu",
        "markets": "h2h",
        "bookmakers": "pinnacle,1xbet"
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json()
