"""
data/supabase_client.py — Persistance et audit via Supabase (PostgreSQL)
Tables : signals, results, bankroll_snapshots, brier_scores
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from supabase import create_client, Client

from config import settings
from core.paim_engine import PAIMSignal

logger = logging.getLogger(__name__)


class SupabaseClient:
    """Wrapper Supabase pour l'audit complet du système Predator PAIM.
    
    Utilise la service_role_key (si disponible) pour contourner le RLS.
    Fallback sur anon key si service_key non configurée.
    """

    def __init__(self):
        # Service key prioritaire pour bypass RLS (lecture/écriture complète)
        _key = settings.supabase_service_key or settings.supabase_key
        if settings.supabase_service_key:
            logger.info("🔑 Supabase: service_role_key active (RLS bypass)")
        self._client: Client = create_client(
            settings.supabase_url, _key
        )
        self._use_service_key = bool(settings.supabase_service_key)

    # ── Signals ───────────────────────────────────────────────

    async def insert_signal(
        self,
        signal: PAIMSignal,
        event_name: str,
        sport: str,
        match_time_iso: str,
        ai_context: str = "",
    ) -> Optional[str]:
        """Insère un signal validé. Retourne l'UUID généré."""
        try:
            data = {
                "event_id": signal.event_id,
                "event_name": event_name,
                "sport": sport,
                "match_time": match_time_iso,
                "market_key": signal.market_key,
                "selection": signal.selection,
                "bookmaker_target": signal.bookmaker_target,
                "sharp_prob": round(signal.sharp_prob, 5),
                "implied_prob_soft": round(signal.implied_prob_soft, 5),
                "ev_plus": round(signal.ev_plus, 5),
                "snr_ratio": round(signal.snr_ratio, 4),
                "recommended_stake": signal.recommended_stake,
                "clv_estimate": round(signal.clv_estimate, 5),
                "ai_context": ai_context,
                "status": "pending",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            response = self._client.table("signals").insert(data).execute()
            record_id = response.data[0]["id"] if response.data else None
            logger.info(f"✅ Signal inséré: {record_id} | {event_name}")
            return record_id
        except Exception as e:
            logger.error(f"Erreur insert signal: {e}")
            return None

    # ── Results ───────────────────────────────────────────────

    async def update_result(
        self,
        signal_id: str,
        outcome: int,        # 1 = gagné, 0 = perdu, -1 = void
        profit_eur: float,
        closing_odds: Optional[float] = None,
    ) -> None:
        """Met à jour le résultat post-match et calcule le CLV réel."""
        try:
            update_data: dict = {
                "outcome": outcome,
                "profit_eur": round(profit_eur, 2),
                "status": "settled",
                "settled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            if closing_odds is not None:
                update_data["closing_odds"] = closing_odds

            self._client.table("signals").update(update_data).eq("id", signal_id).execute()
            logger.info(f"✅ Résultat mis à jour: {signal_id} | profit={profit_eur:.2f}€")
        except Exception as e:
            logger.error(f"Erreur update result: {e}")

    # ── Bankroll Snapshots ────────────────────────────────────

    async def insert_bankroll_snapshot(
        self, balance: float, drawdown: float, roi: float
    ) -> None:
        try:
            self._client.table("bankroll_snapshots").insert({
                "balance": round(balance, 2),
                "drawdown": round(drawdown, 5),
                "roi": round(roi, 5),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }).execute()
        except Exception as e:
            logger.error(f"Erreur snapshot: {e}")

    # ── Dashboard Queries ─────────────────────────────────────

    def get_performance_summary(self) -> dict:
        """Récupère les métriques globales pour le dashboard."""
        try:
            signals = self._client.table("signals").select("*").eq("status", "settled").execute()
            rows = signals.data or []

            if not rows:
                return {"total_bets": 0, "win_rate": 0, "total_profit": 0, "clv_avg": 0}

            total = len(rows)
            wins = sum(1 for r in rows if r.get("outcome") == 1)
            profit = sum(r.get("profit_eur", 0) for r in rows)
            clv_vals = [r["clv_estimate"] for r in rows if r.get("clv_estimate")]
            clv_avg = sum(clv_vals) / len(clv_vals) if clv_vals else 0

            return {
                "total_bets": total,
                "win_rate": wins / total if total else 0,
                "total_profit": round(profit, 2),
                "clv_avg": round(clv_avg, 4),
            }
        except Exception as e:
            logger.error(f"Erreur performance summary: {e}")
            return {}

    def get_equity_curve(self) -> list[dict]:
        """Retourne l'historique bankroll pour la courbe equity."""
        try:
            response = (
                self._client.table("bankroll_snapshots")
                .select("timestamp, balance")
                .order("timestamp")
                .execute()
            )
            return response.data or []
        except Exception as e:
            logger.error(f"Erreur equity curve: {e}")
            return []
