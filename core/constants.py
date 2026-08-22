"""
core/constants.py — PAIM v9.5 — Single Source of Truth
All thresholds, risk classification, and Kelly calculation used by engine,
rapport, dashboard, and audit must import from here — never redefined inline.
"""

# ELITE_EDGE / SOCCER_ELITE_EDGE / BASKETBALL_ELITE_EDGE are display-tier
# boundaries (risk_flag() below: LOW_VALUE/VALUE/HIGH_VALUE for the
# dashboard), not a send/no-send gate — left as fixed, sport-calibrated
# values.
ELITE_EDGE             = 2.5    # % — VALUE / HIGH_VALUE boundary (défaut)
SOCCER_ELITE_EDGE      = 1.5    # % — Soccer AH0 : marché serré, VALUE dès 1.5%
BASKETBALL_ELITE_EDGE  = 2.0    # % — NBA Finales : VALUE dès 2.0% (edges typiques 1.5–2.5%)
AH0_VALUE_THRESHOLD  = 1.5    # Soccer DNB : favori à 1.5+ = valeur intrinsèque
PURGE_EDGE_FLOOR     = 0.5    # % — floor purge : ne jamais supprimer au-dessus de ça
MIN_STAKE    = 2      # € — below this Kelly stake, signal is not actionable
BANKROLL_REF = 150    # € — 100 000 XOF (taux fixe 655.96 XOF/€)
MAX_EDGE     = 15.0   # % — hard cap; above = data mapping error, reject

# Plancher d'EV en DUR sous lequel rien ne sort, quoi que disent les seuils
# appris (core/learning_layer.py) : filet contre un nouveau glissement vers le
# bruit. Justification (2026-08-22) : pente de recalibration 0,12 et CLV nul
# sur le ledger réglé — l'edge estimé porte une erreur de modèle de l'ordre du
# pourcent, un plancher sous 1,5 % revient à parier l'erreur de mesure.
EV_EDGE_FLOOR = 1.5   # % EV

SUSPECT_EDGE = 10.0   # % — safety trigger: major sport edge above this = SUSPECT_DATA (cap totals=15%)

# ── Tax (PAIM v9.5) ───────────────────────────────────────────────────
# Set to 0.0 on 2026-07-08 — explicit operator instruction ("les 20%, on
# s'en soucie plus") after a day of near-zero signal volume, most of it
# caused by the now-fixed compute_alpha() k=1 gate (see git history
# around a30cd39), but the operator's call here is broader: stop
# accounting for the withholding tax in gating/sizing at all, trading
# tax-adjusted rigor for volume. This does NOT mean the real bookmaker
# stops withholding 20% on winnings — it means core.tax_engine's math
# (min_edge_required/optimal_stake_fraction/is_combo_tax_viable, all
# parameterized on this constant via run_engine.py's _TAX_RATE) now
# computes as if there were no tax: lower edges pass, and Kelly stakes
# are sized on the FULL untaxed payout — larger than the true tax-adjusted
# optimum if 20% genuinely is still withheld in reality. Revert to 0.20
# (the real rate — see core/tax_engine.py's module docstring for the tax
# model) to restore tax-aware gating and sizing.
TAX_RATE = 0.0   # % withheld on net profit of a winning bet — see core/tax_engine.py

# The tax gate lives exclusively in core.tax_engine.suggest_system() /
# is_combo_tax_viable() now, evaluated on the real assembled combo — NOT
# here, and not per-individual-signal in core/paim_engine.py's
# compute_alpha(). An earlier version of this file had a
# min_edge_for_k(k=1, ...) gate applied per-signal; confirmed live on
# 2026-07-08 that gating at k=1 (the single most demanding floor — see
# core.tax_engine.min_edge_required: per-leg requirement shrinks as
# system size k grows) discarded every real candidate before
# suggest_system() ever got the chance to combine them into a viable
# system (22/22 discarded in one scan, including an 11.35% edge).
# Removed rather than left as dead-but-harmless code — see git history
# around that date if this needs revisiting.

# ── Retry & Rate Limiting (Centralized) ──────────────────────────────
# (Supprimés le 2026-08-22, plus une seule référence dans le dépôt :
#  DELAY_XBET_MIN/MAX — le LineFeed 1XBet est injoignable depuis les runners
#  et core/harvester.py ne cadence plus rien ; DELAY_GEMINI_RATE — Gemini
#  n'est plus appelé en direct, il passe par core/ai_router.py qui porte son
#  propre rpm. Une constante morte se fait recopier de bonne foi.)
DELAY_DB_RETRY       = 1.0      # Seconds — Supabase transient error retry
MAX_DB_RETRIES       = 3        # Attempts — max retries before giving up
GLOBAL_TIMEOUT       = 540      # Seconds — 9 minutes, safety net for GitHub Actions
DEBUG_MODE           = False    # Will be set from env var PREDATOR_DEBUG

# ── Round-line push penalty (totals on integer lines e.g. 9.0, 8.0) ────
# P(push) on MLB integer totals historically ~8–12% (score lands exactly on line).
# Reduces effective sharp_prob: p_adj = p_win / (1 - p_push)
# Half-lines (.5) have P(push)=0 — no adjustment needed.
PUSH_PROB_ROUND_LINE = 0.10    # 10% — conservative MLB/baseball estimate

# ── MLB Totals lineup confirmation window ─────────────────────────────
# Starting pitchers are officially confirmed ~1h before first pitch.
# Signals generated more than MLB_LINEUP_WINDOW_H hours before game time
# are based on a total line that doesn't yet reflect the actual starter.
# ERA, recent form, and matchup are not priced in until lineup is locked.
MLB_LINEUP_WINDOW_H  = 6       # Hours — discard MLB totals signals beyond this

# ── Closing-line capture ──────────────────────────────────────────────
# Shared by core/closing_line.py (free capture from the OddsAPI scan feed,
# every market) and core/audit_engine.py (web-search oracle fallback, h2h
# only). Lived in audit_engine until 2026-08-01; moved here when a second
# module started depending on the same window.
#
# WINDOW SIZING — measured, not guessed. The closing-line workflow asks for
# one run per hour; GitHub Actions actually delivers 0.48/h (100 runs over
# 206h, measured 2026-08-01), with a MEDIAN gap between executions of
# 116 min and a worst observed gap of 254. The original 5-minute window was
# anchored on the execution instant, so it covered ~4% of the timeline at
# effectively random minutes: across 203 ledger rows it captured a closing
# price exactly zero times, while the job still exited green every run.
#
# So the window can never be sized to a cadence this scheduler does not
# honour. Instead capture is REFRESHED: every run re-prices any signal still
# ahead of kickoff, so whichever run happens to be the last one before
# kickoff leaves behind the closest available price. Correctness no longer
# depends on the scheduler firing at a particular minute — only on it firing
# at all. CLOSING_LINE_REFRESH_MIN bounds the oracle cost of that refresh.
CLOSING_LINE_WINDOW_MIN  = 240  # capture starts this many minutes before kickoff
CLOSING_LINE_TIGHTEN_MIN = 90   # only inside this do we re-price; further out one price is enough
CLOSING_LINE_REFRESH_MIN = 20   # and even then, not more often than this
CLOSING_LINE_BUDGET      = 30   # Max oracle (web search) calls per closing-line run

# Columns both capture paths write. Passed as `optional_cols` on every write
# so a project that has not yet applied sql/migrate_v9_6_closing_line.sql,
# v9_11 (closing_captured_at) or v9_12 (closing_source) degrades to writing
# the columns it does have instead of losing the whole update.
CLOSING_LINE_COLS = frozenset({"closing_pinnacle_price", "clv_pct_real",
                               "closing_captured_at", "closing_source"})
# Values of signals.closing_source — which feed produced the stored price.
CLOSING_SRC_ODDSAPI = "oddsapi"   # exact, per-market, from the scan feed
CLOSING_SRC_ORACLE  = "oracle"    # web-search estimate, h2h/DNB favourite only

# Fractional Kelly par sport — uniquement les 6 sport-types actifs sélectionnés
# (à ne pas confondre avec les 19 SPORT_KEYS d'odds_api.py, plus fins : plusieurs
# ligues/compétitions collapsent vers le même sport-type ici)
# Principe : sharper market = fraction plus haute = mise plus agressive
# Sports exclus (cricket, darts, boxing, tennis, etc.) → retirés pour réduire bruit
#
# TEMPORARILY REDUCED to the 0.10-0.15 band (Task 10, PAIM v9.5) — was
# 0.20-0.30. Tasks 1 (real outcome-based learning), 3 (real closing-line
# CLV) and 4 (Wilson-CI-gated thresholds, _MIN_SAMPLES=30) only just
# started producing trustworthy signal on this run; until each segment
# has >=30 real settled signals validating a genuine post-tax edge
# (core/learning_layer.py's Wilson-CI gate is the actual check — see
# _sport_stats), staking at the old, more aggressive fractions would size
# real money on a still-unvalidated edge. Relative ordering (sharper
# market = higher fraction) is preserved, just compressed into the
# smaller band. Restore the original values once validated.
#
# CRITÈRE DE RESTAURATION (Phase 4 du recentrage, 2026-08-22) — chiffré et
# loggé dans meta `sport_verdict_<sport>` par core/learning_layer.sport_verdict :
# ≥ 30 signaux réglés (zone jouable) ET borne basse de Wilson > rentabilité
# post-taxe → « promotion_eligible » : la fraction peut être remontée
# PROGRESSIVEMENT (un cran par cycle hebdo) vers sa valeur d'origine
# (0.20-0.30 selon le sport). ≥ 30 réglés et edge non démontré → « retrait
# proposé » au rapport hebdo. Les deux sont des DÉCISIONS OPÉRATEUR : ce
# dict ne bouge jamais tout seul.
KELLY_FRACTION = {
    "basketball":  0.15,   # NBA — marché le plus sharp au monde, confiance max
    "hockey":      0.13,   # NHL — sharp, liquid, peu de bruit
    "soccer":      0.12,   # FIFA WC + Copa Lib + MLS + Brasileirão — relevé légèrement
    "baseball":    0.12,   # MLB + KBO + NPB — volume élevé + lag timezone documenté
    "rugbyleague": 0.10,   # NRL — Pinnacle très sharp, marché australien fiable
    "aussierules": 0.10,   # AFL — Pinnacle + Betfair très actifs
    # ── Sports de combat — flux OddsAPI réel depuis le 2026-08-22 ──────
    # Jusque-là le MMA était à 0.08 parce que son prix de référence venait
    # d'une recherche web (fetch_mma_events) et qu'une cote lue sur le web
    # vaut moins qu'une cote de book sharp. Il passe à 0.10 : le prix devient
    # un vrai Pinnacle (OddsAPI mma_mixed_martial_arts), mais reste SOUS les
    # sports majeurs tant que le ledger n'a pas validé l'edge par CLV réel —
    # le +37,5% de ROI historique tient sur 8 paris.
    "mma":         0.10,
    # Boxe : marché mince, jamais validé dans le ledger. À réévaluer après
    # 30 signaux réglés (critère de promotion de la Phase 4).
    "boxing":      0.08,
    # ── Phase 2 (2026-08-22) ───────────────────────────────────────────
    # NFL : sharpness niveau NBA (le marché le plus liquide des US) — 0.14,
    # un cran sous la NBA le temps que le ledger confirme sur ce sport.
    "americanfootball":      0.14,
    # Euroleague : mécaniques basketball mais marché nettement moins sharp
    # que la NBA — ne PAS hériter du 0.15 ; 0.12 (niveau soccer/baseball).
    "euroleague_basketball": 0.12,
}

# Sports RETIRÉS le 2026-08-22 (mission « recentrage sports ») : prix de
# référence issu d'une recherche web IA, jamais d'un book sharp — du bruit.
# run_engine._emit refuse tout signal pour ces sports ; les lignes
# historiques de `signals`/`ai_learning_ledger` sont CONSERVÉES (zéro perte
# de données) et leur settlement continue normalement.
RETIRED_SPORTS = frozenset({"esports", "tabletennis", "volleyball", "handball"})


# ══════════════════════════════════════════════════════════════════════
# WIZ (PAIM v10.0, 2026-07-23) — couche d'analyse contextuelle par IA
# ══════════════════════════════════════════════════════════════════════
# Wiz ne touche JAMAIS à edge_pct / sharp_prob / pinnacle_price /
# xbet_odd / kelly_pct / risk_flag, et n'écrit aucune colonne de `signals`
# (voir sql/migrate_v10_0_wiz.sql pour le pourquoi). Tout ce qui suit ne
# sert qu'à CLASSER et à SIGNALER — jamais à recalculer.

# Domaine de panne séparé : Mistral, jamais Groq/Tavily. Le moteur
# principal dépend déjà de ces derniers et leur quota journalier meurt
# régulièrement (voir core/ai_search.py) — une panne Wiz ne doit pas
# pouvoir dégrader le settlement, ni l'inverse.
#
# Brave a été ABANDONNÉ le 2026-07-23 (décision opérateur) : son plan
# gratuit exige une carte bancaire à l'inscription. Le connecteur
# `web_search` de Mistral le remplace intégralement — vérifié live le même
# jour, il retourne de vrais résultats datés avec URLs sources. Il est du
# reste propulsé par Brave en interne (le favicon des résultats pointe vers
# imgs.search.brave.com), donc on garde les résultats visés par la spec
# d'origine sans le compte ni la carte.

# Noms de modèles Mistral : NON vérifiés live sur ce compte au moment de
# l'écriture (2026-07-23) — contrairement aux modèles Groq de
# core/ai_search.py qui l'ont été via GET /models. Ils sont donc
# surchargeables par WIZ_MISTRAL_MODELS (liste séparée par virgules) sans
# toucher au code. Le premier qui répond gagne ; les suivants servent de
# repli quand un modèle est retiré du free tier.
WIZ_MISTRAL_MODELS = ["mistral-small-latest", "open-mistral-nemo", "mistral-large-latest"]

# Mistral free tier = 2 requêtes/minute. Wiz est un batch cron, pas de
# l'interactif : on respecte la limite par un throttle explicite plutôt
# que de la contourner. 31s > 30s pour absorber la dérive d'horloge.
WIZ_MISTRAL_MIN_INTERVAL_S = 31.0

# Le goulot d'étranglement n'est plus un quota de requêtes depuis l'abandon
# de Brave — c'est la DURÉE DU RUN. Le free tier Mistral plafonne à 2
# requêtes/minute, donc une analyse = ~31 s incompressibles, quel que soit
# le temps de calcul réel. 20 matchs = ~10,5 min, ce qui tient sous le
# `timeout-minutes: 20` de .github/workflows/wiz.yml avec de la marge pour
# les retries. Monter cette valeur sans monter le timeout du workflow ferait
# tuer le run en plein milieu.
#
# Côté tokens il n'y a pas de tension : une recherche consomme ~6 000 tokens
# de connecteur (mesuré live le 2026-07-23) sur un budget mensuel de l'ordre
# du milliard. Ces chiffres de free tier bougent chaque semaine et ne sont
# PAS codés en dur comme une garantie — ils ne servent qu'à justifier le
# dimensionnement ci-dessous.
WIZ_RUN_BUDGET_DEFAULT   = 20    # matchs analysés max par run (1 recherche chacun)
WIZ_SEARCH_RESULTS_MAX   = 10    # sources retenues par analyse (borne le JSON stocké)

# Angles de recherche suggérés au connecteur, par match. Ce n'est plus un
# budget de requêtes (le connecteur décide lui-même combien il en lance)
# mais le nombre d'axes Tier A qu'on lui demande de couvrir.
WIZ_QUERIES_PER_MATCH    = 2     # Tier A : compositions/absences + enjeu (ou météo sur les totals)

WIZ_LOOKAHEAD_H   = 24   # n'analyser que les signaux dont le coup d'envoi est < 24h
WIZ_TTL_H_DEFAULT = 8    # ne pas ré-analyser un match analysé il y a moins de 8h

# TTL d'une NON-RÉPONSE. Une ligne INDISPONIBLE n'est pas une analyse : c'est
# l'aveu que Wiz n'a pas pu chercher (quota, source muette, JSON illisible).
# Lui appliquer le TTL de 8h revient à graver un incident passager dans la
# page pour la journée — et, le 2026-08-22, à retarder de 8h l'effet de toute
# réparation des sources. On repasse donc dès le run suivant (cron = 2h).
WIZ_RETRY_UNAVAILABLE_H = 2
# Exception au TTL : les compositions officielles tombent ~1h avant le coup
# d'envoi (cf. MLB_LINEUP_WINDOW_H plus haut, même logique). Une analyse
# faite à T-20h ne les a pas vues — on autorise donc une seconde passe dans
# la fenêtre T-3h, qui est la seule qui puisse encore attraper un titulaire
# absent avant que le pari ne soit posé.
WIZ_CONFIRM_WINDOW_H = 3

# ── Hiérarchie des sources (R3 : le consensus est CONTRARIAN) ──────────
# Tier A — décisif : absences/blessures confirmées, compositions probables,
#          lanceur MLB confirmé, back-to-back NBA, météo (totals), enjeu
#          sportif (équipe déjà qualifiée). C'est ce qui EXPLIQUE un edge.
# Tier B — modérateur : forme récente, H2H, stats avancées, actu du club.
# Tier C — contrarian : consensus pronostiqueurs, % de paris publics.
#          Poids NÉGATIF, pas faible : un consensus public massif dans le
#          sens du signal est un drapeau JAUNE (cote potentiellement gonflée
#          par le flux public), jamais une confirmation. Les données de
#          pronostiqueurs sont statistiquement perdantes en moyenne — les
#          traiter comme prédictives serait pire que les ignorer.
WIZ_TIER_WEIGHTS = {"A": 1.0, "B": 0.5, "C": -0.35}

# Poids des red flags par sévérité. Séparés des arguments : un red flag
# n'est pas « un argument contre », c'est une explication candidate de
# l'edge — exactement ce que Wiz est là pour trouver (R2), donc il pèse
# plus lourd qu'un argument Tier B ordinaire.
WIZ_SEVERITY_WEIGHTS = {"haute": 1.0, "moyenne": 0.5, "basse": 0.2}

# Nombre max d'arguments/red flags retenus par analyse. Borne la taille du
# JSON stocké et la hauteur du panneau déplié sur /wiz ; au-delà, un modèle
# qui produit 15 arguments dilue surtout ceux qui comptent.
WIZ_MAX_ARGUMENTS = 6
WIZ_MAX_RED_FLAGS = 4

# ── Score composite de classement ─────────────────────────────────────
# wiz_rank_score = W_EDGE*edge_norm + W_WIZ*(confidence/100) + W_CONS*(consensus/100)
# Le quantitatif garde la primauté (W_EDGE le plus fort) : Wiz module le
# classement, il ne décide pas. À confiance Wiz nulle, un signal à fort
# edge doit rester devant un signal à faible edge — c'est testé dans
# tests/test_wiz_engine.py.
WIZ_W_EDGE = 0.45
WIZ_W_WIZ  = 0.35
WIZ_W_CONS = 0.20

# edge_norm = min(edge_pct / WIZ_EDGE_NORM_CAP, 1). Le cap est MAX_EDGE :
# au-delà, run_engine.py rejette déjà le signal comme erreur de mapping,
# donc rien de réel ne sature cette normalisation.
WIZ_EDGE_NORM_CAP = MAX_EDGE

# Confiance attribuée quand aucune information n'a pu être collectée
# (recherche impossible, IA morte, JSON illisible). NEUTRE, pas pénalisant :
# l'absence d'information n'est pas une information négative — un
# INDISPONIBLE ne doit ni promouvoir ni rétrograder un signal par rapport
# à ce que son edge seul lui vaudrait.
WIZ_NEUTRAL_CONFIDENCE = 50.0

# Même logique pour le terme consensus du score : `signals.consensus_score`
# est l'accord entre sources sharp (core/paim_engine.py
# calculate_consensus_price), pas le consensus public — il est NULL sur
# tout signal issu du harvester/oracle, qui n'a qu'une seule source. Un
# signal sans mesure d'accord ne doit pas être rétrogradé pour autant.
WIZ_NEUTRAL_CONSENSUS = 50.0

# Seuils de verdict appliqués au score pondéré des arguments (somme des
# poids×tier signés, direction « contre » comptant en négatif). Le verdict
# renvoyé par le LLM est retenu, mais borné par ces seuils : un modèle qui
# annonce CONFIRME alors que ses propres arguments pointent contre est
# corrigé vers le bas — jamais l'inverse.
WIZ_VETO_SCORE   = -1.0   # faisceau lourd d'indices que l'edge est un piège
WIZ_ALERTE_SCORE = -0.35  # au moins un red flag Tier A crédible
WIZ_CONFIRME_SCORE = 0.5  # rien n'explique l'edge → le soft book est juste lent

# Un seul red flag de sévérité haute suffit à faire passer ALERTE, quel
# que soit le reste : c'est la fonction la plus rentable de Wiz (R2), on
# ne la laisse pas diluer par des arguments favorables.
WIZ_HIGH_SEVERITY_FORCES_ALERTE = True

# Plafond de wiz_confidence PAR VERDICT — ajouté le 2026-07-23 après le
# premier appel live à Mistral.
#
# Le modèle a renvoyé verdict=VETO avec wiz_confidence=75 : il a compris
# « ma confiance dans mon analyse » là où le prompt demande « ma confiance
# que le signal aboutisse ». Les deux lectures sont défendables en français,
# et aucune reformulation du prompt ne garantit qu'un modèle (ou le
# prochain) tranchera dans le bon sens.
#
# Conséquence si on ne borne pas : wiz_confidence entre dans rank_score avec
# un poids de +0.35, donc un match que Wiz vient de qualifier de PIÈGE
# remontait EN TÊTE du classement — l'inverse exact de la fonction du
# module. Constaté live : VETO/conf 75 = 0.6445 contre NEUTRE/conf 50 =
# 0.557 à edge identique.
#
# On borne donc la confiance par le verdict, qui lui est dérivé d'arguments
# sourcés et vérifiés. CONFIRME et NEUTRE restent libres : le risque n'est
# pas symétrique, un excès de prudence coûte un pari manqué, un excès de
# confiance coûte une mise.
WIZ_CONFIDENCE_CEILING = {"VETO": 15.0, "ALERTE": 40.0, "NEUTRE": 100.0, "CONFIRME": 100.0}


def wiz_enforce() -> bool:
    """True quand Wiz a le droit de BLOQUER un signal (verdict VETO).

    Défaut : False. Wiz démarre en mode observation — il affiche, il
    classe, il ne bloque rien. Le verdict VETO est calculé et stocké dès
    maintenant, mais reste sans effet tant que WIZ_ENFORCE=1 n'est pas
    explicitement positionné. On ne l'activera qu'après avoir mesuré, sur
    ~30 signaux réglés, que wiz_confidence apporte réellement de
    l'information (Brier score, core/learning_layer.py).
    """
    import os
    return os.environ.get("WIZ_ENFORCE", "0").strip() in ("1", "true", "True", "yes")


def wiz_run_budget() -> int:
    """Budget Brave (requêtes) pour ce run — surchargeable par WIZ_RUN_BUDGET."""
    import os
    try:
        return max(0, int(os.environ.get("WIZ_RUN_BUDGET", WIZ_RUN_BUDGET_DEFAULT)))
    except (TypeError, ValueError):
        return WIZ_RUN_BUDGET_DEFAULT


def wiz_ttl_h() -> float:
    """Âge au-delà duquel une analyse Wiz est périmée — surchargeable par WIZ_TTL_H."""
    import os
    try:
        return max(0.0, float(os.environ.get("WIZ_TTL_H", WIZ_TTL_H_DEFAULT)))
    except (TypeError, ValueError):
        return float(WIZ_TTL_H_DEFAULT)


def wiz_mistral_models() -> list:
    """Modèles Mistral à essayer dans l'ordre — surchargeable par WIZ_MISTRAL_MODELS.

    Existe parce que les noms de modèles du free tier Mistral n'ont pas pu
    être vérifiés live à l'écriture : si l'un d'eux est renommé/retiré,
    l'opérateur corrige par variable d'environnement sans redéploiement.
    """
    import os
    raw = os.environ.get("WIZ_MISTRAL_MODELS", "").strip()
    if raw:
        models = [m.strip() for m in raw.split(",") if m.strip()]
        if models:
            return models
    return list(WIZ_MISTRAL_MODELS)


def risk_flag(edge_pct: float, elite: float = ELITE_EDGE) -> str:
    """Consistent risk label stored in DB and used by all consumers.
    `elite` lets callers pass a sport-specific boundary (SOCCER_ELITE_EDGE,
    BASKETBALL_ELITE_EDGE) — defaults to the generic ELITE_EDGE."""
    if edge_pct >= elite * 2:
        return "HIGH_VALUE"
    if edge_pct >= elite:
        return "VALUE"
    return "LOW_VALUE"


def kelly_stake(xbet_odd: float, sharp_prob: float,
                bankroll: int = BANKROLL_REF,
                sport: str = "soccer",
                current_exposure: float = 0.0) -> int:
    """
    Fractional Kelly adaptatif par sport — fraction dans KELLY_FRACTION.
    Returns 0 (non-actionable) if computed stake < MIN_STAKE.

    `current_exposure` (Task 7, core/risk_manager.py) is capital already
    committed to other active signals — the effective bankroll available
    for THIS stake is reduced by it, so a portfolio already at or past its
    exposure cap (core.risk_manager.MAX_EXPOSURE_PCT) naturally sizes new
    stakes down to 0 instead of stacking risk on top of risk. Defaults to
    0 (no reduction) for callers that haven't computed exposure.
    """
    b = xbet_odd - 1
    if b <= 0 or sharp_prob <= 0:
        return 0
    effective_bankroll = max(0.0, bankroll - current_exposure)
    if effective_bankroll <= 0:
        return 0
    kf = (sharp_prob * b - (1 - sharp_prob)) / b
    fraction = KELLY_FRACTION.get(sport, 0.12)   # fallback also inside Task 10's temporary 0.10-0.15 band
    stake = round(max(0.0, kf * fraction) * effective_bankroll)
    return stake if stake >= MIN_STAKE else 0
