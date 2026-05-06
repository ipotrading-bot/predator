from .base_client import BaseClient
from config import settings

class ApiSportsClient(BaseClient):
    """Client spécifique pour API-Sports (via RapidAPI)."""
    def __init__(self):
        super().__init__(
            base_url="https://api-sports-football.p.rapidapi.com/v3",
            api_key=settings.rapidapi_key
        )

    async def get_leagues(self):
        """Exemple de méthode spécifique."""
        return await self._get("/leagues")
