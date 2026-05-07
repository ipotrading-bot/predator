"""
scripts/weekly_audit.py — Audit hebdomadaire PAIM
Appelé par .github/workflows/weekly_audit.yml chaque dimanche à 20h UTC.

Actions :
  1. Récupère les signaux des 7 derniers jours depuis Supabase
  2. Calcule les métriques (win rate, CLV moyen, volume)
  3. Génère une analyse IA via Gemini
  4. Envoie le rapport sur Telegram
"""
from __future__ import annotations

import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("weekly_audit")


async def run_weekly_audit() -> int:
    """Exécute l'audit hebdomadaire. Retourne 0 (succès) ou 1 (erreur)."""
    try:
        from config import settings
        from core.paim_engine import PAIMEngine
        from core.notifications import TelegramNotifier
        from data.supabase_client import SupabaseClient

        db = SupabaseClient()
        notifier = TelegramNotifier()

        # 1. Récupérer les données de la semaine
        data = PAIMEngine.fetch_weekly_data(db._client)
        if not data:
            logger.info("Aucun signal cette semaine — rapport vide envoyé.")
            await notifier.send_audit_report(
                total_trades=0,
                avg_clv=0.0,
                win_rate=0.0,
                report_ai="Aucun signal traité cette semaine. Capital préservé ✅",
            )
            return 0

        # 2. Calculer les métriques
        metrics = PAIMEngine.run_weekly_audit(data)
        if not metrics:
            logger.error("Calcul des métriques échoué.")
            return 1

        total_trades = metrics["total_trades"]
        avg_clv = metrics["avg_clv"]
        win_rate = metrics["win_rate"]

        logger.info(
            f"📊 Métriques hebdo | trades={total_trades} "
            f"| CLV={avg_clv:.2%} | WR={win_rate:.1f}%"
        )

        # 3. Analyse IA (Gemini)
        sports_list = list({s.get("sport", "?") for s in data})
        try:
            report_ai = PAIMEngine.get_ai_analysis(
                total_trades, avg_clv, win_rate, sports_list
            )
        except Exception as e:
            logger.warning(f"Analyse Gemini échouée: {e} — rapport sans IA.")
            report_ai = (
                f"Analyse IA indisponible.\n"
                f"Résumé brut : {total_trades} signaux | "
                f"CLV {avg_clv:.2%} | WR {win_rate:.1f}%"
            )

        # 4. Envoyer le rapport Telegram
        await notifier.send_audit_report(
            total_trades=total_trades,
            avg_clv=avg_clv,
            win_rate=win_rate,
            report_ai=report_ai,
        )

        logger.info("✅ Audit hebdomadaire envoyé avec succès.")
        return 0

    except Exception as e:
        logger.critical(f"❌ Audit hebdomadaire échoué: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(run_weekly_audit())
    sys.exit(exit_code)
