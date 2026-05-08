"""
core/rotation_engine.py — Protocole de Rotation de Scan PAIM v3.6
PhD MIT Architecture — State-Based Scanning

Gestion du Rate Limiting (15 RPM max) via rotation circulaire des sports.
Une seule requête lourde toutes les 4-5 minutes = ~288 requêtes/jour
"""
from __future__ import annotations

import logging
import time
from typing import Optional
from dataclasses import dataclass

from config import settings
from data.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# ALPHA WATCHLIST — Gisements d'Alpha classés par potentiel
# Source: nomenclature officielle The-Odds-API
# ═══════════════════════════════════════════════════════════════════

ALPHA_WATCHLIST = [
    # ═══ BASKETBALL — Priorité Alpha MAXIMALE ═══
    "basketball_nba",           # Cible n°1 institutionnelle
    "basketball_euroleague",    # Final Four Europe
    "basketball_ncaab",         # March Madness
    
    # ═══ TENNIS — Binary Synthesis Sniper ═══
    "tennis_atp_masters_1000",  # Madrid, Rome, etc.
    "tennis_wta",               # Circuit féminin
    "tennis_atp_french_open",   # Roland Garros
    
    # ═══ SOCCER — Liquidité Haute ═══
    "soccer_epl",               # Premier League
    "soccer_uefa_champs_league", # UCL
    "soccer_germany_bundesliga", # Bundesliga
    
    # ═══ ESPORTS — Gisement de Latence MAXIMALE ═══
    "esports_lol_lck",          # Corée (latence extrême)
    "esports_lol_lpl",          # Chine
    "esports_csgo_esl_pro_league", # CS:GO
    
    # ═══ DIVERSIFICATION ═══
    "baseball_mlb",             # MLB — Très prévisible
    "icehockey_nhl",            # NHL
    "mma_mixed_martial_arts",   # UFC/MMA
]

# Total: 14 sports = cycle complet toutes les 70 minutes (14 × 5 min)


@dataclass
class RotationState:
    """État de la rotation de scan."""
    last_index: int
    current_sport: str
    scan_count: int
    last_scan_at: Optional[str]
    
    @property
    def next_index(self) -> int:
        """Calcule l'index suivant (rotation circulaire)."""
        return (self.last_index + 1) % len(ALPHA_WATCHLIST)
    
    @property
    def next_sport(self) -> str:
        """Retourne le prochain sport à scanner."""
        return ALPHA_WATCHLIST[self.next_index]
    
    @property
    def cycle_progress(self) -> str:
        """Pourcentage de complétion du cycle."""
        return f"{self.next_index}/{len(ALPHA_WATCHLIST)}"


class RotationEngine:
    """
    Moteur de rotation séquentielle pour respecter le Rate Limiting.
    
    Architecture:
    - GitHub Actions trigger toutes les 5 minutes
    - Un seul sport scanné par cycle
    - État persistant dans Supabase (scanner_state)
    - ~288 requêtes/jour (sous limite 500 The-Odds-API)
    """
    
    def __init__(self):
        self.db = SupabaseClient()
        self.watchlist = ALPHA_WATCHLIST
        self._state_cache: Optional[RotationState] = None
    
    def _get_state_from_db(self) -> RotationState:
        """Récupère l'état depuis Supabase."""
        try:
            response = self.db._client.table("scanner_state")\
                .select("last_index, current_sport, scan_count, last_scan_at")\
                .eq("id", 1)\
                .single()\
                .execute()
            
            if response.data:
                data = response.data
                return RotationState(
                    last_index=data.get("last_index", 0),
                    current_sport=data.get("current_sport", ""),
                    scan_count=data.get("scan_count", 0),
                    last_scan_at=data.get("last_scan_at"),
                )
        except Exception as e:
            logger.warning(f"État non trouvé, initialisation: {e}")
        
        # État par défaut si table vide
        return RotationState(0, "", 0, None)
    
    def _save_state_to_db(self, state: RotationState, sport_scanned: str) -> bool:
        """Sauvegarde le nouvel état dans Supabase."""
        try:
            self.db._client.table("scanner_state").upsert({
                "id": 1,
                "last_index": state.next_index,
                "current_sport": sport_scanned,
                "scan_count": state.scan_count + 1,
                "last_scan_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }).execute()
            return True
        except Exception as e:
            logger.error(f"Erreur sauvegarde état: {e}")
            return False
    
    def get_next_sport(self) -> tuple[str, RotationState]:
        """
        Détermine le prochain sport à scanner.
        
        Returns:
            (sport_key, state): Le sport cible et l'état actuel
        """
        state = self._get_state_from_db()
        next_sport = state.next_sport
        
        logger.info(
            f"🔄 ROTATION | Cycle {state.cycle_progress} | "
            f"Prochain: {next_sport} | Total scans: {state.scan_count}"
        )
        
        return next_sport, state
    
    def advance_rotation(self, sport_scanned: str, state: RotationState) -> bool:
        """
        Avance la rotation après un scan réussi.
        
        Args:
            sport_scanned: Le sport qui vient d'être scanné
            state: L'état actuel de rotation
            
        Returns:
            bool: True si la sauvegarde a réussi
        """
        success = self._save_state_to_db(state, sport_scanned)
        
        if success:
            logger.info(
                f"✅ ROTATION AVANCÉE | {sport_scanned} scanné | "
                f"Prochain: {state.next_sport}"
            )
        else:
            logger.error("❌ Échec sauvegarde rotation")
        
        return success
    
    def get_watchlist_summary(self) -> dict:
        """Retourne un résumé de la watchlist pour monitoring."""
        state = self._get_state_from_db()
        return {
            "total_sports": len(self.watchlist),
            "current_index": state.last_index,
            "next_sport": state.next_sport,
            "cycle_progress": state.cycle_progress,
            "scan_count": state.scan_count,
            "watchlist": self.watchlist,
            "estimated_cycle_time_min": len(self.watchlist) * 5,  # 5 min par sport
        }


# ═══════════════════════════════════════════════════════════════════
# Fonctions utilitaires pour l'API
# ═══════════════════════════════════════════════════════════════════

def get_rotation_summary() -> dict:
    """API helper pour le dashboard."""
    engine = RotationEngine()
    return engine.get_watchlist_summary()


def execute_rotation_scan() -> dict:
    """
    Exécute un scan rotationnel complet.
    Appelé par GitHub Actions toutes les 5 minutes.
    
    Returns:
        dict: Résultat du scan avec métadonnées
    """
    from signals.scanner import MarketScanner
    import asyncio
    
    engine = RotationEngine()
    sport, state = engine.get_next_sport()
    
    logger.info(f"🎯 CIBLE DU CYCLE: {sport}")
    
    # Scan spécifique au sport
    scanner = MarketScanner(bankroll=settings.starting_bankroll)
    
    # Override: scanner UNIQUEMENT ce sport
    scanner.target_sport_override = sport
    
    try:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(scanner.run_single_sport_scan(sport))
        loop.close()
        
        # Avancer la rotation
        engine.advance_rotation(sport, state)
        
        return {
            "status": "success",
            "sport_scanned": sport,
            "signals_found": result.signals_validated,
            "cycle": state.cycle_progress,
            "next_scan": state.next_sport,
        }
        
    except Exception as e:
        logger.error(f"❌ Erreur scan rotation {sport}: {e}")
        return {
            "status": "error",
            "sport_scanned": sport,
            "error": str(e),
        }
