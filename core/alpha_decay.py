"""
core/alpha_decay.py — Alpha Decay Tracker (PAIM v5.0)
Surveille la dégradation des cotes dans le temps pour optimiser le timing.

Métriques:
- Alpha Decay Rate: Vitesse de chute de l'EV+ (%/minute)
- Urgency Score: 🔥 DÉGRADATION RAPIDE vs 🟢 ALPHA STABLE
- Half-Life: Temps où 50% de l'Alpha disparaît
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Optional, List
from collections import defaultdict

logger = logging.getLogger(__name__)


@dataclass
class OddsSnapshot:
    """Snapshot d'une cote à un instant T."""
    timestamp: float
    soft_odds: float
    ev_plus: float
    sharp_prob: float


@dataclass
class AlphaDecayMetrics:
    """Métriques de dégradation pour un signal."""
    signal_id: str
    current_ev: float
    initial_ev: float
    decay_rate: float  # % per hour
    half_life_minutes: Optional[float]
    urgency_status: str  # 🟢 STABLE | 🟡 MODÉRÉ | 🔥 CRITIQUE
    stability_score: float  # 0-1
    snapshots_count: int
    
    # Recommandation timing
    action_recommendation: str  # "WAIT" | "ACT NOW" | "EXPIRED"
    minutes_until_expiry: Optional[int]


class AlphaDecayTracker:
    """
    Tracker de dégradation des cotes en temps réel.
    
    Stockage: snapshots toutes les 5 minutes pour chaque signal actif.
    Calcule la vitesse de convergence vers la cote de fermeture.
    """
    
    # Seuils d'urgence (PhD MIT)
    DECAY_STABLE = 0.005      # < 0.5%/h = stable
    DECAY_MODERATE = 0.015    # 0.5-1.5%/h = modéré
    DECAY_CRITICAL = 0.03     # > 1.5%/h = critique
    
    def __init__(self, max_history_hours: float = 48):
        self.max_history_hours = max_history_hours
        # In-memory storage: {signal_id: [snapshots]}
        self._snapshots: Dict[str, List[OddsSnapshot]] = defaultdict(list)
        self._last_cleanup = time.time()
        
    def record_snapshot(
        self,
        signal_id: str,
        soft_odds: float,
        ev_plus: float,
        sharp_prob: float,
        timestamp: Optional[float] = None
    ) -> None:
        """
        Enregistre un nouveau snapshot de cote.
        
        Args:
            signal_id: ID unique du signal
            soft_odds: Cote soft actuelle
            ev_plus: EV+ actuel
            sharp_prob: Probabilité sharp
            timestamp: Timestamp Unix (default: now)
        """
        ts = timestamp or time.time()
        
        snapshot = OddsSnapshot(
            timestamp=ts,
            soft_odds=soft_odds,
            ev_plus=ev_plus,
            sharp_prob=sharp_prob
        )
        
        self._snapshots[signal_id].append(snapshot)
        
        # Cleanup toutes les heures
        if ts - self._last_cleanup > 3600:
            self._cleanup_old_snapshots()
            self._last_cleanup = ts
    
    def get_decay_metrics(self, signal_id: str) -> Optional[AlphaDecayMetrics]:
        """
        Calcule les métriques de dégradation pour un signal.
        
        Returns:
            AlphaDecayMetrics avec statut d'urgence et recommandation
        """
        snapshots = self._snapshots.get(signal_id, [])
        
        if len(snapshots) < 2:
            # Pas assez d'historique
            if snapshots:
                return AlphaDecayMetrics(
                    signal_id=signal_id,
                    current_ev=snapshots[-1].ev_plus,
                    initial_ev=snapshots[-1].ev_plus,
                    decay_rate=0.0,
                    half_life_minutes=None,
                    urgency_status="🟢 INSUFFISANT",
                    stability_score=0.5,
                    snapshots_count=1,
                    action_recommendation="WAIT",
                    minutes_until_expiry=None
                )
            return None
        
        # Analyse temporelle
        first = snapshots[0]
        last = snapshots[-1]
        
        time_span_hours = (last.timestamp - first.timestamp) / 3600
        
        if time_span_hours < 0.016:  # < 1 minute
            return None
        
        # Calcul du taux de dégradation (%/heure)
        ev_change = last.ev_plus - first.ev_plus
        decay_rate = abs(ev_change) / time_span_hours if time_span_hours > 0 else 0
        
        # Score de stabilité (1 = parfaitement stable)
        if len(snapshots) >= 3:
            # Variance des évolutions entre snapshots consécutifs
            changes = []
            for i in range(1, len(snapshots)):
                dt = (snapshots[i].timestamp - snapshots[i-1].timestamp) / 3600
                if dt > 0:
                    d_ev = snapshots[i].ev_plus - snapshots[i-1].ev_plus
                    changes.append(d_ev / dt)
            
            if changes:
                variance = sum((c - sum(changes)/len(changes))**2 for c in changes) / len(changes)
                stability_score = max(0, 1 - (variance ** 0.5) * 10)
            else:
                stability_score = 0.5
        else:
            stability_score = 0.5
        
        # Détermination du statut d'urgence
        if decay_rate < self.DECAY_STABLE:
            urgency_status = "🟢 ALPHA STABLE"
            action_rec = "OK TO WAIT"
            minutes_expiry = 60
        elif decay_rate < self.DECAY_MODERATE:
            urgency_status = "🟡 DÉGRADATION MODÉRÉE"
            action_rec = "CONSIDER NOW"
            minutes_expiry = 30
        elif decay_rate < self.DECAY_CRITICAL:
            urgency_status = "🔥 DÉGRADATION RAPIDE"
            action_rec = "ACT NOW"
            minutes_expiry = 10
        else:
            urgency_status = "⛔ ALPHA CRITIQUE"
            action_rec = "EXPIRED"
            minutes_expiry = 0
        
        # Calcul de la demi-vie (half-life)
        half_life = None
        if decay_rate > 0 and last.ev_plus > 0:
            # Combien de temps pour perdre 50% de l'EV actuel
            half_life = (last.ev_plus / 2) / decay_rate * 60  # en minutes
        
        return AlphaDecayMetrics(
            signal_id=signal_id,
            current_ev=last.ev_plus,
            initial_ev=first.ev_plus,
            decay_rate=round(decay_rate, 4),
            half_life_minutes=round(half_life, 1) if half_life else None,
            urgency_status=urgency_status,
            stability_score=round(stability_score, 2),
            snapshots_count=len(snapshots),
            action_recommendation=action_rec,
            minutes_until_expiry=minutes_expiry
        )
    
    def get_all_active_signals(self) -> List[str]:
        """Retourne les IDs des signaux avec historique actif."""
        return list(self._snapshots.keys())
    
    def remove_signal(self, signal_id: str) -> None:
        """Supprime un signal de la surveillance (après settlement)."""
        if signal_id in self._snapshots:
            del self._snapshots[signal_id]
    
    def _cleanup_old_snapshots(self) -> None:
        """Nettoie les snapshots plus vieux que max_history_hours."""
        cutoff = time.time() - (self.max_history_hours * 3600)
        
        for signal_id in list(self._snapshots.keys()):
            self._snapshots[signal_id] = [
                s for s in self._snapshots[signal_id]
                if s.timestamp > cutoff
            ]
            if not self._snapshots[signal_id]:
                del self._snapshots[signal_id]


# ═══════════════════════════════════════════════════════════════════
# API PERSISTENCE — Stockage Supabase des métriques Decay
# ═══════════════════════════════════════════════════════════════════

class AlphaDecayPersistence:
    """Persiste les métriques Alpha Decay dans Supabase."""
    
    def __init__(self, supabase_client):
        self.db = supabase_client
    
    def save_snapshot(self, signal_id: str, snapshot: OddsSnapshot) -> bool:
        """Sauvegarde un snapshot dans la table alpha_decay_history."""
        try:
            data = {
                "signal_id": signal_id,
                "timestamp": datetime.fromtimestamp(snapshot.timestamp).isoformat(),
                "soft_odds": snapshot.soft_odds,
                "ev_plus": snapshot.ev_plus,
                "sharp_prob": snapshot.sharp_prob,
            }
            self.db.table("alpha_decay_history").insert(data).execute()
            return True
        except Exception as e:
            logger.error(f"Erreur save decay snapshot: {e}")
            return False
    
    def load_snapshots(self, signal_id: str, hours: int = 24) -> List[OddsSnapshot]:
        """Charge l'historique des snapshots pour un signal."""
        try:
            from_time = (datetime.now() - timedelta(hours=hours)).isoformat()
            
            response = self.db.table("alpha_decay_history")\
                .select("*")\
                .eq("signal_id", signal_id)\
                .gte("timestamp", from_time)\
                .order("timestamp")\
                .execute()
            
            snapshots = []
            for row in response.data:
                ts = datetime.fromisoformat(row["timestamp"].replace('Z', '+00:00'))
                snapshots.append(OddsSnapshot(
                    timestamp=ts.timestamp(),
                    soft_odds=row["soft_odds"],
                    ev_plus=row["ev_plus"],
                    sharp_prob=row["sharp_prob"]
                ))
            return snapshots
        except Exception as e:
            logger.error(f"Erreur load decay snapshots: {e}")
            return []


# Singleton global
the_decay_tracker: Optional[AlphaDecayTracker] = None

def get_decay_tracker() -> AlphaDecayTracker:
    """Retourne l'instance singleton du tracker."""
    global the_decay_tracker
    if the_decay_tracker is None:
        the_decay_tracker = AlphaDecayTracker()
    return the_decay_tracker
