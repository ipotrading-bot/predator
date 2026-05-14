"""
core/harvester.py — 1XBet Direct Feed Harvester v7.0
Moissonnage direct du flux JSON interne de 1XBet.

Stratégie "Guerrilla Data" (PhD MIT):
  Au lieu d'attendre que les données arrivent via une API payante,
  on intercepte le flux direct que 1XBet utilise pour afficher ses cotes.
  
  Avantages:
  - Gratuit et illimité (pas de quota)
  - Cotes en temps réel (avant même l'affichage site web)
  - Accès à TOUS les sports et marchés

Sécurité (Dakar Hub):
  - Rotation User-Agent
  - Scan toutes les 15 minutes max (pas toutes les minutes)
  - Jitter aléatoire entre les requêtes
"""
from __future__ import annotations

import logging
import random
import time
from typing import Optional

import requests as _req

logger = logging.getLogger(__name__)

# ── Sports IDs 1XBet ──────────────────────────────────────────────
# Identifiants internes du LineFeed 1XBet
SPORT_IDS = {
    "soccer":           1,
    "basketball":       4,
    "tennis":           5,
    "icehockey":        6,
    "baseball":         9,
    "esports_lol":     112,
    "esports_csgo":    117,
    "mma":             21,
}

# ── User-Agents pour obfuscation ─────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/119.0.6045.109 Mobile/15E148 Safari/604.1",
]

# ── Intervalles de scan (secondes) ────────────────────────────────
MIN_SCAN_INTERVAL = 900   # 15 minutes
JITTER_RANGE = 120        # ±2 minutes de jitter


class OneXBetHarvester:
    """
    Moissonneur de flux direct 1XBet.
    
    Récupère le fichier JSON interne que 1XBet utilise
    pour alimenter son interface de cotes en ligne.
    
    Le JSON est complexe et nécessite Gemini pour le décoder
    (via GeminiOracle.decode_harvester_feed()).
    """

    def __init__(self):
        self._last_scan: float = 0.0
        self._stats = {"total_raw_fetches": 0, "successful_decodes": 0}

    def _get_headers(self) -> dict:
        """Génère des headers HTTP avec rotation User-Agent."""
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://1xbet.com/en/line/",
            "Origin": "https://1xbet.com",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

    def fetch_raw_feed(self, sport_key: str = "soccer", count: int = 50) -> Optional[dict]:
        """
        Intercepte le flux JSON interne de 1XBet (LineFeed).
        
        Args:
            sport_key: Identifiant sport (clé interne Predator)
            count: Nombre de matchs à récupérer (max 50)
            
        Returns:
            dict: Flux JSON brut de 1XBet ou None si échec
            
        Note:
            Le JSON retourné est complexe et cryptique.
            Utiliser GeminiOracle.decode_harvester_feed() pour le décoder.
        """
        sport_id = SPORT_IDS.get(sport_key, 1)

        # URL du proxy de flux interne (Endpoint mondial 1XBet)
        url = (
            f"https://1xbet.com/LineFeed/Get1x2"
            f"?sport={sport_id}&count={count}&lng=en&mode=4&country=1"
        )

        try:
            response = _req.get(url, headers=self._get_headers(), timeout=15)

            if response.status_code == 200:
                self._stats["total_raw_fetches"] += 1
                logger.info(f"✅ Harvester: {sport_key} → {len(response.text)} bytes")
                return response.json()
            
            logger.warning(f"Harvester HTTP {response.status_code} pour {sport_key}")
            return None

        except Exception as e:
            logger.error(f"Harvester error {sport_key}: {e}")
            return None

    def fetch_multi_sport(self, sports: list[str] | None = None) -> dict[str, dict]:
        """
        Récupère les flux bruts pour plusieurs sports.
        
        Args:
            sports: Liste des sports à scanner (défaut: soccer + basketball + tennis)
            
        Returns:
            dict: {sport_key: raw_json_dict}
        """
        if sports is None:
            sports = ["soccer", "basketball", "tennis"]

        results = {}
        for sport in sports:
            raw = self.fetch_raw_feed(sport_key=sport, count=30)
            if raw:
                results[sport] = raw
            # Rate-limit: 2-4 secondes entre chaque sport
            time.sleep(random.uniform(2.0, 4.0))

        logger.info(f"Harvester multi-sport: {len(results)}/{len(sports)} récupérés")
        return results

    def can_scan(self) -> bool:
        """Vérifie si le délai minimum entre scans est respecté."""
        elapsed = time.time() - self._last_scan
        return elapsed >= MIN_SCAN_INTERVAL + random.uniform(-JITTER_RANGE, JITTER_RANGE)

    def mark_scanned(self):
        """Marque le dernier scan."""
        self._last_scan = time.time()

    def get_stats(self) -> dict:
        return dict(self._stats)


# Singleton
_harvester_instance: Optional[OneXBetHarvester] = None

def get_harvester() -> OneXBetHarvester:
    """Retourne l'instance singleton du Harvester."""
    global _harvester_instance
    if _harvester_instance is None:
        _harvester_instance = OneXBetHarvester()
    return _harvester_instance