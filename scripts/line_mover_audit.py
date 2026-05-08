"""
scripts/line_mover_audit.py — Line Mover Audit (12:00 UTC)
Vérifie si les cotes des signaux du matin ont bougé depuis le scan de 06:00.

Logique :
  1. Récupère les signaux "pending" créés dans les 8 dernières heures
  2. Re-fetch les cotes actuelles via The-Odds-API
  3. Compare : si l'Alpha a AUGMENTÉ → opportunité renforcée → alerte Telegram
  4. Si l'Alpha a DISPARU → signal obsolète → marquer "stale"

Alpha Decay doctrine :
  - Alpha qui augmente = marché qui confirme l'inefficience → BET
  - Alpha qui disparaît = marché qui s'est corrigé → SKIP
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("line_mover_audit")


async def run_line_mover_audit() -> int:
    try:
        from config import settings
        from data.supabase_client import SupabaseClient
        from data.odds_fetcher import OddsFetcher
        from core.math_engine import calculate_shin_probabilities
        from core.paim_engine import PAIMEngine
        from core.notifications import TelegramNotifier

        db = SupabaseClient()
        notifier = TelegramNotifier()
        engine = PAIMEngine(
            kelly_fraction=settings.kelly_fraction,
            max_stake_pct=settings.max_single_stake_pct,
        )

        # 1. Récupérer les signaux pending des 8 dernières heures
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat()
        res = (
            db._client.table("signals")
            .select("*")
            .eq("status", "pending")
            .gte("created_at", cutoff)
            .execute()
        )
        signals = res.data or []

        if not signals:
            logger.info("✅ Aucun signal récent à auditer.")
            return 0

        logger.info(f"📊 {len(signals)} signaux à re-vérifier...")

        # 2. Re-fetch les cotes actuelles
        async with OddsFetcher() as fetcher:
            all_events = await fetcher.fetch_all_sports_odds()

        # Indexer les événements par ID
        events_by_id = {e["id"]: e for e in all_events}

        reinforced = []
        stale = []

        for sig in signals:
            event_id = sig.get("event_id", "")
            event = events_by_id.get(event_id)

            if not event:
                logger.warning(f"Événement introuvable: {sig.get('match_name')}")
                continue

            # Re-calculer l'Alpha actuel
            bookmakers = event.get("bookmakers", [])
            sharp_probs: dict[str, float] = {}

            for bm in bookmakers:
                if bm.get("key") not in settings.sharp_books:
                    continue
                for market in bm.get("markets", []):
                    if market.get("key") != "h2h":
                        continue
                    outcomes = market.get("outcomes", [])
                    raw_odds = [o["price"] for o in outcomes if o.get("price", 0) > 1.0]
                    if len(raw_odds) >= 2:
                        try:
                            probs = calculate_shin_probabilities(raw_odds)
                            for i, o in enumerate(outcomes):
                                if i < len(probs):
                                    sharp_probs[o["name"]] = probs[i]
                        except Exception:
                            pass

            if not sharp_probs:
                continue

            selection = sig.get("selection", "")
            sharp_prob = sharp_probs.get(selection)
            if not sharp_prob:
                continue

            # Trouver la cote soft actuelle
            soft_odds_now = None
            target_bm = sig.get("bookmaker_target", "")
            for bm in bookmakers:
                bm_key = settings.synonyms.get(bm.get("key", ""), bm.get("key", ""))
                if bm_key != target_bm:
                    continue
                for market in bm.get("markets", []):
                    if market.get("key") != sig.get("market_type", "h2h"):
                        continue
                    for outcome in market.get("outcomes", []):
                        if outcome.get("name") == selection:
                            soft_odds_now = outcome.get("price")

            if not soft_odds_now:
                continue

            alpha_now = engine.compute_ev(sharp_prob, soft_odds_now)
            alpha_original = sig.get("alpha_spread", 0)
            delta = alpha_now - alpha_original

            match_name = sig.get("match_name", "?")

            if alpha_now <= 0.005:
                # Alpha disparu → signal obsolète
                db._client.table("signals").update(
                    {"status": "stale"}
                ).eq("id", sig["id"]).execute()
                stale.append(match_name)
                logger.info(f"❌ Stale: {match_name} | Alpha {alpha_original:.2%} → {alpha_now:.2%}")

            elif delta >= 0.005:
                # Alpha renforcé → opportunité confirmée
                reinforced.append({
                    "match": match_name,
                    "alpha_original": alpha_original,
                    "alpha_now": alpha_now,
                    "delta": delta,
                })
                logger.info(
                    f"🔥 Renforcé: {match_name} | "
                    f"{alpha_original:.2%} → {alpha_now:.2%} (+{delta:.2%})"
                )
            else:
                logger.info(
                    f"➡️  Stable: {match_name} | Alpha {alpha_now:.2%} (Δ{delta:+.2%})"
                )

        # 3. Rapport Telegram
        if reinforced or stale:
            lines = ["📈 *LINE MOVER AUDIT — 12:00 UTC*\n" + "─" * 28]

            if reinforced:
                lines.append("🔥 *ALPHA RENFORCÉ (BET CONFIRMÉ) :*")
                for r in reinforced:
                    lines.append(
                        f"• {r['match'][:35]} | "
                        f"{r['alpha_original']:.1%} → {r['alpha_now']:.1%} "
                        f"(+{r['delta']:.1%})"
                    )

            if stale:
                lines.append("\n❌ *SIGNAUX OBSOLÈTES (SKIP) :*")
                for s in stale[:5]:
                    lines.append(f"• {s[:40]}")

            await notifier.send_audit_report(
                total_trades=len(reinforced) + len(stale),
                avg_clv=sum(r["alpha_now"] for r in reinforced) / max(len(reinforced), 1),
                win_rate=len(reinforced) / max(len(reinforced) + len(stale), 1) * 100,
                report_ai="\n".join(lines),
            )

        logger.info(
            f"✅ Line Mover Audit terminé | "
            f"{len(reinforced)} renforcés | {len(stale)} obsolètes"
        )
        return 0

    except Exception as e:
        logger.critical(f"❌ Line Mover Audit échoué: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_line_mover_audit()))
