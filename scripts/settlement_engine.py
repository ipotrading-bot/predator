"""
scripts/settlement_engine.py — Settlement Engine (23:00 UTC)
Récupère les résultats des matchs de la journée, calcule P&L,
met à jour l'Equity Curve et envoie le rapport de clôture.

Logique :
  1. Récupère les signaux "pending" dont le match_time est passé
  2. Fetch les scores finaux via The-Odds-API /scores
  3. Détermine Win / Loss / Void
  4. Calcule le P&L réel (mise × cote - mise)
  5. Met à jour Supabase (status, outcome, profit_eur)
  6. Insère un snapshot bankroll pour l'Equity Curve
  7. Envoie le rapport de clôture sur Telegram
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("settlement_engine")


async def fetch_scores(sport: str, api_key: str) -> list[dict]:
    """Récupère les scores des matchs terminés (dernières 24h)."""
    url = f"https://api.the-odds-api.com/v4/sports/{sport}/scores/"
    params = {"apiKey": api_key, "daysFrom": 1}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        logger.warning(f"Scores fetch error ({sport}): {e}")
    return []


def determine_outcome(signal: dict, match_result: dict) -> tuple[int | None, float]:
    """
    Détermine Win(1) / Loss(0) / Void(None) et calcule le P&L.
    Retourne (outcome, profit_eur).
    """
    scores = match_result.get("scores") or []
    if len(scores) < 2:
        return None, 0.0

    try:
        home_score = int(scores[0]["score"])
        away_score = int(scores[1]["score"])
    except (ValueError, KeyError):
        return None, 0.0

    # Void si match nul (AH 0.0)
    if home_score == away_score:
        return None, 0.0

    winner = match_result.get("home_team") if home_score > away_score else match_result.get("away_team")
    selection = signal.get("selection", "")
    stake = signal.get("recommended_stake", 0) or 0

    # Récupérer la cote utilisée (1 / implied_prob_soft)
    implied = signal.get("implied_prob_soft", 0)
    odds_used = round(1.0 / implied, 3) if implied > 0 else 0.0

    if selection and winner and selection.lower() in winner.lower():
        profit = round(stake * (odds_used - 1), 2)
        return 1, profit
    else:
        return 0, round(-stake, 2)


async def run_settlement_engine() -> int:
    try:
        from config import settings
        from data.supabase_client import SupabaseClient
        from core.notifications import TelegramNotifier

        db = SupabaseClient()
        notifier = TelegramNotifier()

        # 1. Signaux pending dont le match est passé
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
            logger.info("✅ Aucun signal à régler ce soir.")
            return 0

        logger.info(f"⚙️  {len(signals)} signaux à régler...")

        # 2. Fetch scores par sport (une seule requête par sport)
        sports_needed = list({s["sport"] for s in signals if s.get("sport")})
        scores_by_sport: dict[str, list[dict]] = {}
        for sport in sports_needed:
            scores_by_sport[sport] = await fetch_scores(sport, settings.odds_api_key)

        # 3. Régler chaque signal
        total_profit = 0.0
        wins, losses, voids = 0, 0, 0
        settled_details = []

        for sig in signals:
            sport = sig.get("sport", "")
            match_name = sig.get("match_name", "?")
            scores = scores_by_sport.get(sport, [])

            # Trouver le match dans les scores
            match_result = next(
                (
                    m for m in scores
                    if (
                        m.get("home_team", "") + " vs " + m.get("away_team", "")
                    ).lower() == match_name.lower()
                    and m.get("completed", False)
                ),
                None,
            )

            if not match_result:
                logger.warning(f"Score introuvable: {match_name}")
                continue

            outcome, profit = determine_outcome(sig, match_result)

            # Mise à jour Supabase
            await db.update_result(
                signal_id=sig["id"],
                outcome=outcome if outcome is not None else -1,
                profit_eur=profit,
            )

            total_profit += profit
            if outcome == 1:
                wins += 1
            elif outcome == 0:
                losses += 1
            else:
                voids += 1

            verdict = "✅ WIN" if outcome == 1 else ("❌ LOSS" if outcome == 0 else "↩️ VOID")
            logger.info(f"{verdict} | {match_name} | P&L: {profit:+.2f}€")
            settled_details.append(f"{verdict} {match_name[:30]} | {profit:+.2f}€")

        # 4. Snapshot bankroll
        try:
            perf = db.get_performance_summary()
            current_balance = settings.starting_bankroll + perf.get("total_profit", 0)
            drawdown = min(0.0, (current_balance - settings.starting_bankroll) / settings.starting_bankroll)
            roi = (current_balance - settings.starting_bankroll) / settings.starting_bankroll

            await db.insert_bankroll_snapshot(
                balance=current_balance,
                drawdown=drawdown,
                roi=roi,
            )
            logger.info(f"📈 Snapshot bankroll: {current_balance:.2f}€ | ROI: {roi:+.2%}")
        except Exception as e:
            logger.warning(f"Snapshot bankroll échoué: {e}")

        # 5. Rapport Telegram
        n = wins + losses + voids
        win_rate = wins / max(n, 1) * 100
        detail = "\n".join(settled_details[:10])

        report = (
            f"🏁 *SETTLEMENT ENGINE — 23:00 UTC*\n"
            f"{'─' * 28}\n"
            f"✅ Wins: `{wins}` | ❌ Losses: `{losses}` | ↩️ Voids: `{voids}`\n"
            f"💰 P&L du jour: `{total_profit:+.2f}€`\n"
            f"🎯 Win Rate: `{win_rate:.0f}%`\n"
            f"{'─' * 28}\n"
            f"{detail}"
        )

        await notifier.send_audit_report(
            total_trades=n,
            avg_clv=0.0,
            win_rate=win_rate,
            report_ai=report,
        )

        logger.info(
            f"✅ Settlement terminé | {wins}W/{losses}L/{voids}V "
            f"| P&L: {total_profit:+.2f}€"
        )
        return 0

    except Exception as e:
        logger.critical(f"❌ Settlement Engine échoué: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_settlement_engine()))
