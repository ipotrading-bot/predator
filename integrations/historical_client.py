import os
import requests

class HistoricalClient:
    """Gestionnaire pour données historiques de cotes."""
    def __init__(self):
        self.api_key = os.environ.get("HISTORICAL_ODDS_KEY")
        self.base_url = "https://api.historical-odds.com/v1"

    def get_closing_odds(self, match_id: str):
        # Implementation de récupération des closing odds
        pass
