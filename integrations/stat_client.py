import os
import requests

class StatClient:
    """Gestionnaire pour API-Football."""
    def __init__(self):
        self.api_key = os.environ.get("API_FOOTBALL_KEY")
        self.base_url = "https://api-football-v1.p.rapidapi.com/v3"

    def get_match_stats(self, fixture_id: int):
        # Implementation de récupération des stats de match
        pass
