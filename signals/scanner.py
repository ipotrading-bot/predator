"""
signals/scanner.py — MarketScanner v3.0 (Multi-Source Data Fusion)

Pipeline CIM (Centrale d'Intelligence de Marché) :
  1. OddsFetcher     → Cotes sharp vs soft (The-Odds-API)
  2. Shin Method     → Probabilités réelles sans marge
  3. PAIMEngine      → Filtre EV+ / SNR (seuils doctrinaires)
  4. NewsEngine      → Détection news market-moving (NewsAPI, < 6h)
  5. GroqClient      → Pré-filtre bayésien ultra-rapide (Llama 3)
  6. Perplexity      → Grounding factuel (blessures/lineups temps réel)
  7. GeminiValidator → Chain-of-Thought final avec contexte enrichi
  8. Kelly stake     → Mise fractionnaire plafonnée
  9. Supabase        → Persistance avec colonne `sources_validated`
  10. Telegram       → Alerte avec badge sources

Doctrine Alpha Decay :
  - News > 4h = déjà dans le prix → ignorée
  - Consensus requis : Pinnacle + au moins 1 source contextuelle
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from config import settings
from core.math_engine import calculate_shin_probabilities
from core.news_engine import news_engine, NewsImpact
from core.notifications import TelegramNotifier
from core.paim_engine import PAIMEngine, PAIMSignal, ScanResult
from core.validator import check_market_red_flags
from data.odds_fetcher import OddsFetcher
from data.supabase_client import SupabaseClient
from api.groq_client import groq_client
from api.perplexity_client import perplexity_grounding

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Dossier d'Arbitrage — résultat de la fusion multi-sources
# ─────────────────────────────────────────────────────────────────

@dataclass
class ArbitrageDossier:
    """
    Dossier complet d'un signal après fusion de toutes les sources.
    Chaque champ `*_ok` indique si la source a validé le signal.
    """
    signal: PAIMSignal
    meta: dict

    # Sources de validation
    odds_ok: bool = True          # Toujours True (source primaire)
    news_ok: bool = False         # NewsAPI : pas de news market-moving
    groq_ok: bool = False         # Groq : approuvé
    perplexity_ok: bool = False   # Perplexity : effectif confirmé
    gemini_ok: bool = False       # Gemini : pas de red flag

    # Détails
    news_impact: Optional[NewsImpact] = None
    groq_confidence: float = 0.0
    perplexity_summary: str = ""
    gemini_context: str = ""

    @property
    def sources_count(self) -> int:
        """Nombre de sources ayant validé le signal."""
        return sum([self.odds_ok, self.news_ok, self.groq_ok,
                    self.perplexity_ok, self.gemini_ok])

    @property
    def sources_badge(self) -> str:
        """Badge lisible pour le dashboard et Telegram."""
        badges = []
        if self.odds_ok:
            badges.append("✅ Odds")
        if self.news_ok:
            badges.append("✅ News")
        if self.groq_ok:
            badges.append("✅ Groq")
        if self.perplexity_ok:
            badges.append("✅ Perplexity")
        if self.gemini_ok:
            badges.append("✅ Gemini")
        return " | ".join(badges) if badges else "⚠️ Non validé"

    @property
    def is_consensus(self) -> bool:
        """Consensus = Odds + au moins 2 autres sources."""
        return self.odds_ok and self.sources_count >= 3


# ─────────────────────────────────────────────────────────────────
# MarketScanner v3.0
# ─────────────────────────────────────────────────────────────────

class MarketScanner:
    """
    Orchestre le pipeline PAIM Multi-Sources complet.

    Args:
        bankroll: Capital de départ en euros (défaut : settings.starting_bankroll)
    """

    def __init__(self, bankroll: Optional[float] = None):
        self.bankroll = bankroll or settings.starting_bankroll
        self.engine = PAIMEngine(
            kelly_fraction=settings.kelly_fraction,
            max_stake_pct=settings.max_single_stake_pct,
        )
        self.engine.min_ev_threshold = settings.min_ev_threshold
        self.db = SupabaseClient()
        self.notifier = TelegramNotifier()

    # ─────────────────────────────────────────────────────────────
    # Point d'entrée principal
    # ─────────────────────────────────────────────────────────────

    async def run_scan(self) -> ScanResult:
        """Lance un cycle de scan complet. Retourne un ScanResult."""
        start = time.monotonic()
        result = ScanResult()

        logger.info(
            f"🦅 Démarrage scan CIM v3.0 | bankroll={self.bankroll:,.0f}€ "
            f"| EV+ min={self.engine.min_ev_threshold:.1%} "
            f"| sports={len(settings.target_sports)}"
        )

        try:
            # 1. Fetch toutes les cotes en parallèle
            async with OddsFetcher() as fetcher:
                events = await fetcher.fetch_all_sports_odds()

            result.events_analyzed = len(events)
            logger.info(f"📡 {len(events)} événements récupérés")

            if not events:
                logger.warning("Aucun événement retourné — scan terminé.")
                result.duration_seconds = time.monotonic() - start
                return result

            # 2. Pré-analyse NewsAPI en batch (une seule passe pour tous les matchs)
            news_cache = await self._prefetch_news(events)

            # 3. Traitement événement par événement
            dossiers: list[ArbitrageDossier] = []

            for event in events:
                event_dossiers = await self._process_event(event, news_cache)
                result.signals_found += len(event_dossiers)
                for d in event_dossiers:
                    result.signals_validated += 1
                    dossiers.append(d)

            result.signals_rejected = result.signals_found - result.signals_validated

            # 4. Persistance + notifications
            if dossiers:
                await self._persist_and_notify(dossiers)

            # 5. Ticket système 7/9 si assez de signaux consensus
            consensus = [d for d in dossiers if d.is_consensus]
            if len(consensus) >= settings.system_min_wins:
                sigs = [d.signal for d in consensus[: settings.system_size]]
                metas = [d.meta for d in consensus[: settings.system_size]]
                await self.notifier.send_system_ticket(sigs, metas)

        except Exception as e:
            logger.critical(f"❌ Erreur critique scan: {e}", exc_info=True)

        result.duration_seconds = round(time.monotonic() - start, 2)
        logger.info(
            f"✅ Scan CIM terminé | {result.events_analyzed} events | "
            f"{result.signals_validated} signaux | {result.duration_seconds}s"
        )
        return result

    # ─────────────────────────────────────────────────────────────
    # Pré-fetch NewsAPI en batch
    # ─────────────────────────────────────────────────────────────

    async def _prefetch_news(self, events: list[dict]) -> dict[str, NewsImpact]:
        """
        Pré-charge les news pour tous les matchs avec Alpha potentiel.
        Évite N appels séquentiels — une seule passe batch.
        """
        if not news_engine.is_available():
            return {}

        # On ne pré-charge que les matchs des sports cibles
        matches = []
        for event in events:
            home = event.get("home_team", "")
            away = event.get("away_team", "")
            sport = event.get("sport_key", "")
            if home and away:
                matches.append((f"{home} vs {away}", sport))

        if not matches:
            return {}

        logger.info(f"📰 NewsAPI batch: {len(matches)} matchs à analyser")
        return await news_engine.batch_analyze(matches, hours_back=6)

    # ─────────────────────────────────────────────────────────────
    # Traitement d'un événement
    # ─────────────────────────────────────────────────────────────

    async def _process_event(
        self,
        event: dict,
        news_cache: dict[str, NewsImpact],
    ) -> list[ArbitrageDossier]:
        """
        Analyse un événement et retourne les dossiers d'arbitrage validés.
        """
        event_id = event.get("id", "")
        home_team = event.get("home_team", "")
        away_team = event.get("away_team", "")
        sport = event.get("sport_key", "")
        commence_time = event.get("commence_time", "")
        event_name = f"{home_team} vs {away_team}"

        bookmakers = event.get("bookmakers", [])
        if not bookmakers:
            return []

        # Extraire les probabilités sharp (Shin Method)
        sharp_odds = self._extract_sharp_odds(bookmakers)
        if not sharp_odds:
            return []

        # Récupérer l'impact news pré-chargé
        news_impact = news_cache.get(event_name)

        # Bloquer immédiatement si news market-moving critique (score > 0.8)
        if news_impact and news_impact.impact_score > 0.8:
            logger.warning(
                f"📰 News critique bloquante: {event_name} "
                f"| score={news_impact.impact_score:.0%} "
                f"| {news_impact.injury_alerts[0][:80] if news_impact.injury_alerts else ''}"
            )
            return []

        validated: list[ArbitrageDossier] = []

        for bm in bookmakers:
            bm_key = bm.get("key", "")
            # Normalisation insensible à la casse + synonymes
            bm_key_lower = bm_key.lower()
            bm_key_norm = settings.synonyms.get(bm_key_lower, bm_key_lower)
            if bm_key_norm not in [s.lower() for s in settings.soft_books]:
                continue

            for market in bm.get("markets", []):
                market_key = market.get("key", "")
                if market_key not in ("h2h", "spreads"):
                    continue

                raw_outcomes = market.get("outcomes", [])

                # ── Binary Synthesis : Soccer h2h → Draw No Bet ───
                # Le h2h soccer a 3 issues (Home/Draw/Away).
                # On convertit en DNB : retire le nul, recalcule les cotes binaires.
                if market_key == "h2h" and "soccer" in sport:
                    dnb_outcomes = _apply_draw_no_bet(raw_outcomes)
                    if not dnb_outcomes:
                        continue
                    outcomes_to_process = dnb_outcomes
                    effective_market_key = "dnb"
                else:
                    outcomes_to_process = raw_outcomes
                    effective_market_key = market_key

                for outcome in outcomes_to_process:
                    selection = outcome.get("name", "")
                    soft_odds_val = outcome.get("price", 0.0)
                    if soft_odds_val <= 1.0:
                        continue

                    sharp_prob = sharp_odds.get(selection)
                    if sharp_prob is None:
                        continue

                    # ── Étape 1 : Filtre mathématique PAIM ────────
                    signal = self.engine.evaluate_signal(
                        event_id=event_id,
                        market_key=effective_market_key,
                        selection=selection,
                        bookmaker_target=bm_key_norm,
                        sharp_prob=sharp_prob,
                        soft_odds=soft_odds_val,
                        bankroll=self.bankroll,
                        min_ev=self.engine.min_ev_threshold,
                        min_snr=settings.min_snr_ratio,
                    )
                    if signal is None:
                        continue

                    meta = {
                        "event_name": event_name,
                        "sport": sport,
                        "commence_time": commence_time,
                    }
                    dossier = ArbitrageDossier(
                        signal=signal,
                        meta=meta,
                        news_impact=news_impact,
                    )

                    # ── Étape 2 : Validation News ──────────────────
                    dossier.news_ok = (
                        news_impact is None or not news_impact.market_moving
                    )
                    if not dossier.news_ok:
                        logger.info(
                            f"📰 News rejet (score={news_impact.impact_score:.0%}): "
                            f"{event_name} | {news_impact.summary[:80]}"
                        )
                        continue

                    # ── Étape 3 : Pré-filtre Groq ─────────────────
                    groq_result = await groq_client.quick_filter({
                        "sport": sport,
                        "market_key": effective_market_key,
                        "selection": selection,
                        "ev_plus": round(signal.ev_plus * 100, 2),
                        "sharp_prob": round(signal.sharp_prob * 100, 2),
                        "implied_prob_soft": round(signal.implied_prob_soft * 100, 2),
                        "snr_ratio": round(signal.snr_ratio, 2),
                        "recommended_stake": signal.recommended_stake,
                    })
                    dossier.groq_ok = groq_result.get("approved", True)
                    dossier.groq_confidence = groq_result.get("confidence", 0.5)

                    if not dossier.groq_ok:
                        logger.info(
                            f"⚡ Groq rejet: {event_name} | {selection} "
                            f"| {groq_result.get('reason', '')}"
                        )
                        continue

                    # ── Étape 4 : Perplexity Grounding ────────────
                    # Uniquement si EV+ > 2% (économiser le quota)
                    if signal.ev_plus >= 0.02 and perplexity_grounding.is_available():
                        perp_result = await perplexity_grounding.verify_factual_claim(
                            query_text=f"{event_name} lineup injuries",
                            context_type="injury",
                        )
                        dossier.perplexity_ok = perp_result.is_reliable
                        dossier.perplexity_summary = perp_result.summary

                        # Bloquer si Perplexity confirme une blessure critique
                        if (perp_result.verified
                                and perp_result.confidence >= 0.8
                                and "OUT" in perp_result.summary.upper()):
                            logger.warning(
                                f"🔍 Perplexity rejet (blessure confirmée): "
                                f"{event_name} | {perp_result.summary[:80]}"
                            )
                            continue
                    else:
                        # Pas de quota Perplexity → approuver par défaut
                        dossier.perplexity_ok = True
                        dossier.perplexity_summary = "Non vérifié (quota ou EV+ < 2%)"

                    # ── Étape 5 : Chain-of-Thought Gemini ─────────
                    # Contexte enrichi : news + Perplexity → Gemini
                    enriched_context = _build_gemini_context(
                        event_name=event_name,
                        market_key=effective_market_key,
                        signal=signal,
                        news_impact=news_impact,
                        perplexity_summary=dossier.perplexity_summary,
                    )

                    gemini_response = await asyncio.get_event_loop().run_in_executor(
                        None,
                        check_market_red_flags,
                        enriched_context,
                        market_key,
                    )

                    dossier.gemini_ok = not (
                        gemini_response and "🚨 RED FLAG" in gemini_response
                    )
                    dossier.gemini_context = (
                        gemini_response or "✅ Aucun risque majeur détecté."
                    )

                    if not dossier.gemini_ok:
                        logger.warning(
                            f"🚨 Gemini red flag: {event_name} "
                            f"| {gemini_response[:80]}"
                        )
                        continue

                    # ── Signal validé par la fusion ────────────────
                    signal.ai_context = dossier.gemini_context
                    validated.append(dossier)

                    logger.info(
                        f"✅ DOSSIER VALIDÉ: {event_name} | {selection} "
                        f"| EV+={signal.ev_plus:.2%} | SNR={signal.snr_ratio:.2f} "
                        f"| Sources: {dossier.sources_badge}"
                    )

        return validated

    # ─────────────────────────────────────────────────────────────
    # Extraction des probabilités sharp (Shin Method)
    # ─────────────────────────────────────────────────────────────

    def _extract_sharp_odds(self, bookmakers: list[dict]) -> dict[str, float]:
        """
        Extrait les probabilités réelles depuis les cotes Pinnacle via Shin Method.
        Retourne un dict {team_name: sharp_prob}.
        """
        for bm in bookmakers:
            if bm.get("key") not in settings.sharp_books:
                continue

            for market in bm.get("markets", []):
                if market.get("key") != "h2h":
                    continue

                outcomes = market.get("outcomes", [])
                if len(outcomes) < 2:
                    continue

                raw_odds = [o["price"] for o in outcomes if o.get("price", 0) > 1.0]
                if len(raw_odds) < 2:
                    continue

                try:
                    shin_probs = calculate_shin_probabilities(raw_odds)
                except Exception as e:
                    logger.warning(f"Shin Method échoué: {e}")
                    continue

                return {
                    outcomes[i]["name"]: shin_probs[i]
                    for i in range(min(len(outcomes), len(shin_probs)))
                }

        return {}

    # ─────────────────────────────────────────────────────────────
    # Persistance et notifications
    # ─────────────────────────────────────────────────────────────

    async def _persist_and_notify(self, dossiers: list[ArbitrageDossier]) -> None:
        """Insère les dossiers en Supabase et envoie les alertes Telegram."""
        for dossier in dossiers:
            signal = dossier.signal
            meta = dossier.meta
            try:
                signal_id = await self.db.insert_signal(
                    signal=signal,
                    event_name=meta["event_name"],
                    sport=meta["sport"],
                    match_time_iso=meta["commence_time"],
                    ai_context=signal.ai_context,
                    sources_badge=dossier.sources_badge,
                )

                class _Val:
                    context_summary = (
                        f"{signal.ai_context}\n\n"
                        f"🔗 Sources: {dossier.sources_badge}"
                    )

                await self.notifier.send_signal(
                    signal=signal,
                    meta=meta,
                    validation=_Val(),
                    signal_id=signal_id,
                )

            except Exception as e:
                logger.error(
                    f"Erreur persist/notify {meta.get('event_name')}: {e}"
                )


# ─────────────────────────────────────────────────────────────────
# Helper : contexte enrichi pour Gemini (Chain-of-Thought)
# ─────────────────────────────────────────────────────────────────

def _build_gemini_context(
    event_name: str,
    market_key: str,
    signal: PAIMSignal,
    news_impact: Optional[NewsImpact],
    perplexity_summary: str,
) -> str:
    """
    Construit le contexte enrichi pour le prompt Gemini Chain-of-Thought.
    Court et précis — évite les timeouts Gemini.
    """
    news_section = "Aucune news récente."
    if news_impact and news_impact.top_headlines:
        news_section = news_impact.top_headlines[0][:100]

    perp_section = (perplexity_summary or "Non vérifié.")[:120]

    return (
        f"{event_name} | {market_key.upper()}\n"
        f"EV+={signal.ev_plus:.2%} | Sharp={signal.sharp_prob:.3f} | Soft={signal.implied_prob_soft:.3f}\n"
        f"Perplexity: {perp_section}\n"
        f"News: {news_section}\n"
        f"L'Alpha est-il une inefficience pure ou justifié par une blessure ?"
    )


def _apply_draw_no_bet(outcomes: list[dict]) -> list[dict]:
    """
    Convertit les cotes h2h 1N2 en Draw No Bet (DNB).

    Formule DNB :
      Cote_DNB_Home = Cote_Home / (1 - 1/Cote_Draw)
      Cote_DNB_Away = Cote_Away / (1 - 1/Cote_Draw)

    Retourne une liste de 2 outcomes (Home DNB, Away DNB).
    Retourne [] si les cotes sont invalides ou si le nul est introuvable.
    """
    home_outcome = next((o for o in outcomes if o.get("name") not in ("Draw",) and outcomes.index(o) == 0), None)
    draw_outcome = next((o for o in outcomes if o.get("name") == "Draw"), None)
    away_outcome = next((o for o in outcomes if o.get("name") not in ("Draw",) and outcomes.index(o) != 0), None)

    # Fallback : chercher par position si noms non standards
    if not draw_outcome and len(outcomes) == 3:
        draw_outcome = outcomes[1]
    if not home_outcome and len(outcomes) >= 1:
        home_outcome = outcomes[0]
    if not away_outcome and len(outcomes) >= 3:
        away_outcome = outcomes[2]

    if not all([home_outcome, draw_outcome, away_outcome]):
        return []

    try:
        cote_home = float(home_outcome["price"])
        cote_draw = float(draw_outcome["price"])
        cote_away = float(away_outcome["price"])
    except (KeyError, ValueError, TypeError):
        return []

    if cote_draw <= 1.0 or cote_home <= 1.0 or cote_away <= 1.0:
        return []

    draw_prob = 1.0 / cote_draw
    if draw_prob >= 1.0:
        return []

    dnb_home = round(cote_home / (1.0 - draw_prob), 3)
    dnb_away = round(cote_away / (1.0 - draw_prob), 3)

    return [
        {"name": home_outcome["name"], "price": dnb_home},
        {"name": away_outcome["name"], "price": dnb_away},
    ]
