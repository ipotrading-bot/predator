from .base_client import BaseClient
from config import settings

class OddsApiClient(BaseClient):
    """Client spécifique pour The-Odds-API."""
    def __init__(self):
        super().__init__(
            base_url="https://api.the-odds-api.com/v4",
            api_key=settings.odds_api_key
        )

    async def fetch_odds(self, sport: str, regions: str = "eu"):
        """Récupère les cotes pour un sport."""
        return await self._get(f"/sports/{sport}/odds/", params={"regions": regions, "oddsFormat": "decimal"})
