import aiohttp
import logging
from typing import Optional
from dogpile.cache import make_region

logger = logging.getLogger(__name__)

# Cache region for API responses
region = make_region().configure(
    'dogpile.cache.memory',
    expiration_time=3600
)

class BaseClient:
    """
    Client de base pour les intégrations API sportives.
    Gère la configuration, le client HTTP async, l'erreur standard, et la mise en cache.
    """
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url
        self.api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            base_url=self.base_url,
            headers={"X-API-KEY": self.api_key, "User-Agent": "PredatorPAIM/1.0"},
            timeout=aiohttp.ClientTimeout(total=10.0),
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._session:
            await self._session.close()

    async def _get(self, endpoint: str, params: Optional[dict] = None) -> dict:
        """Méthode de requête GET avec gestion d'erreur."""
        if not self._session:
            raise RuntimeError("Le client n'est pas initialisé. Utilisez avec 'async with'.")
            
        try:
            async with self._session.get(endpoint, params=params) as response:
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientError as e:
            logger.error(f"Erreur API lors de l'appel à {endpoint}: {e}")
            raise
            
    async def _get_cached(self, endpoint: str, params: Optional[dict] = None) -> dict:
        """Version avec cache de la méthode GET."""
        key = f"{endpoint}:{str(params)}"
        data = region.get(key)
        if data is region.NO_VALUE:
            data = await self._get(endpoint, params)
            region.set(key, data)
        return data
