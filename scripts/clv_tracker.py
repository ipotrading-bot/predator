"""
scripts/clv_tracker.py — Calcul automatique de la CLV réelle (Phase Shadow)
À lancer le lendemain matin après une nuit de signaux en mode shadow.

Logique :
  1. Récupère les signaux "pending" dont le match_time est passé
  2. Pour chaque signal, fetch la closing line Pinnacle via The-Odds-API /scores
  3. Calcule la CLV réelle = (cote_bot - cote_closing) / cote_closing
  4. Met à jour la colonne clv_estimate dans Supabase
  5. Affiche un rapport terminal + envoie un résumé Telegram

Verdict :
  CLV > 0 sur la majorité des signaux → bot mathématiquement gagnant
  CLV ≤ 0 → ajuster les seuils EV+ / SNR dans config.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("clv_tracker")


# ─────────────────────────────────────────────────────────────────
# Fetch closing line Pinnacle
# ─────────────────────────────────────────────────────────────────

async def fetch_closing_line(
    sport: str,
    event_id: str,
    selection: str,
    api_key: str,
) -> float | None:
    """
    Récupère la dernière cote Pinnacle pour un événement terminé.
    Retourne la cote décimale ou None si introuvable.
    """
    import httpx

    url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds/"
    params = {
        "apiKey": api_key,
        "regions": "eu",
        "markets": "h2h",
        "bookmakers": "pinnacle",
        "oddsFormat": "decimal",
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                return None

            events = resp.json()
            for event in events:
                if event.get("id") != event_id:
                    continue
                for bm in event.get("bookmakers", []):
                    if bm.get("key") != "pinnacle":
                        continue
                    for market in bm.get("markets", []):
                        if market.get("key") != "h2h":
                            continue
                        for outcome in market.get("outcomes", []):
                            if selection.lower() in outcome.get("name", "").lower():
                                return float(outcome["price"])
    except Exception as e:
        logger.warning(f"Erreur fetch closing line {event_id}: {e}")

    return None


# ─────────────────────────────────────────────────────────────────
# Calcul CLV
# ─────────────────────────────────────────────────────────────────

def compute_real_clv(bot_odds: float, closing_odds: float) -> float:
    """
    CLV réelle = (cote_bot - cote_closing) / cote_closing
    Positif = le bot a trouvé une meilleure cote que la fermeture → edge réel.
    """
    if closing_odds <= 0:
        return 0.0
    return (bot_odds - closing_odds) / closing_odds


# ─────────────────────────────────────────────────────────────────
# Runner principal
# ─────────────────────────────────────────────────────────────────

async def run_clv_tracker() -> int:
    try:
        from config import settings
        from data.supabase_client import SupabaseClient
        from core.notifications import TelegramNotifier

        db = SupabaseClient()
        notifier = TelegramNotifier()

        # 1. Récupérer les signaux pending dont le match est passé
        now_iso = datetime.now(timezone.utc).isoformat()
        res = (
            db._client.table("signals")
            .select("*")
            .eq("status", "pending")
            .lt("match_time", now_iso)
            .execute()
        )
        signals = res.data or []

        if not signals:
            logger.info("✅ Aucun signal en attente de CLV tracking.")
            return 0

        logger.info(f"📊 {len(signals)} signaux à tracker...")

        results = []
        positive_clv = 0
        total_clv = 0.0

        for sig in signals:
            event_id = sig.get("event_id", "")
            sport = sig.get("sport", "")
            selection = sig.get("selection", "")
            match_name = sig.get("match_name", "?")

            # Cote que le bot avait trouvée = 1 / implied_prob_soft
            implied_prob = sig.get("implied_prob_soft", 0)
            bot_odds = round(1.0 / implied_prob, 4) if implied_prob > 0 else 0.0

            # Fetch closing line Pinnacle
            closing_odds = await fetch_closing_line(
                sport=sport,
                event_id=event_id,
                selection=selection,
                api_key=settings.odds_api_key,
            )

            if closing_odds is None:
                logger.warning(f"⚠️  Closing line introuvable: {match_name}")
                # Marquer comme void si pas de données
                db._client.table("signals").update(
                    {"status": "no_closing_data"}
                ).eq("id", sig["id"]).execute()
                continue

            # Calcul CLV réelle
            clv_real = compute_real_clv(bot_odds, closing_odds)
            total_clv += clv_real
            if clv_real > 0:
                positive_clv += 1

            # Mise à jour Supabase
            db._client.table("signals").update({
                "clv_estimate": round(clv_real, 5),
                "status": "clv_tracked",
            }).eq("id", sig["id"]).execute()

            verdict = "✅" if clv_real > 0 else "❌"
            logger.info(
                f"{verdict} {match_name} | {selection} "
                f"| Bot: {bot_odds:.3f} | Closing: {closing_odds:.3f} "
                f"| CLV: {clv_real:+.2%}"
            )

            results.append({
                "match": match_name,
                "selection": selection,
                "bot_odds": bot_odds,
                "closing_odds": closing_odds,
                "clv": clv_real,
            })

        if not results:
            logger.info("Aucun résultat CLV calculé.")
            return 0

        # 2. Rapport final
        n = len(results)
        avg_clv = total_clv / n
        beat_rate = positive_clv / n * 100

        logger.info("─" * 50)
        logger.info(f"📈 RAPPORT CLV SHADOW")
        logger.info(f"   Signaux analysés : {n}")
        logger.info(f"   CLV moyenne      : {avg_clv:+.2%}")
        logger.info(f"   Beat closing     : {positive_clv}/{n} ({beat_rate:.0f}%)")

        if avg_clv > 0:
            logger.info("   VERDICT : ✅ BOT MATHÉMATIQUEMENT GAGNANT")
        else:
            logger.info("   VERDICT : ⚠️  AJUSTER LES FILTRES EV+/SNR")
        logger.info("─" * 50)

        # 3. Rapport Telegram
        verdict_emoji = "✅" if avg_clv > 0 else "⚠️"
        verdict_text = "BOT GAGNANT — CLV positive" if avg_clv > 0 else "AJUSTER FILTRES — CLV négative"

        detail_lines = "\n".join(
            f"{'✅' if r['clv'] > 0 else '❌'} {r['match'][:30]} | CLV {r['clv']:+.2%}"
            for r in results[:8]  # max 8 lignes
        )

        report = (
            f"📊 *RAPPORT CLV SHADOW — PREDATOR PAIM*\n"
            f"{'─' * 28}\n"
            f"📌 Signaux : `{n}`\n"
            f"📈 CLV Moyenne : `{avg_clv:+.2%}`\n"
            f"🎯 Beat Closing : `{positive_clv}/{n} ({beat_rate:.0f}%)`\n"
            f"{'─' * 28}\n"
            f"{detail_lines}\n"
            f"{'─' * 28}\n"
            f"{verdict_emoji} *{verdict_text}*"
        )

        await notifier.send_audit_report(
            total_trades=n,
            avg_clv=avg_clv,
            win_rate=beat_rate,
            report_ai=report,
        )

        return 0

    except Exception as e:
        logger.critical(f"❌ CLV Tracker échoué: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(run_clv_tracker())
    sys.exit(exit_code)
