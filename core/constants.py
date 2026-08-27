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

# ── Taxe ──────────────────────────────────────────────────────────────
# RÉTABLI À 0.20 LE 2026-08-27 — le taux réel.
#
# Historique, parce qu'il explique la panne : mis à 0.0 le 2026-07-08 sur
# instruction explicite de l'opérateur (« les 20%, on s'en soucie plus »),
# après une journée à volume quasi nul dont la cause réelle était ailleurs
# (le gate k=1 de compute_alpha, corrigé depuis — cf. git autour de a30cd39).
# Mettre la constante à zéro n'a JAMAIS empêché le bookmaker de prélever :
# ça a seulement fait calculer le moteur comme si la retenue n'existait pas.
# Conséquences mesurées : des edges plus faibles passaient les gates, et les
# mises Kelly étaient dimensionnées sur un payout NON taxé — donc plus
# grosses que l'optimum réel. Un moteur qui ignore un coût qu'il paie
# vraiment ne « gagne du volume » qu'en trompant sa propre comptabilité.
#
# Modèle de taxe : retenue sur le GAIN NET d'un pari GAGNANT uniquement
# (`net_b` ci-dessous). Un pari perdant n'est pas taxé, un remboursement non
# plus. Voir la docstring de core/tax_engine.py.
TAX_RATE = 0.20   # part retenue sur le gain net d'un pari gagnant


def net_b(odds: float, tax_rate: float = TAX_RATE) -> float:
    """
    Gain net par unité misée sur un pari gagnant, après taxe — le « b » de
    Kelly, fiscalisé : `(cote − 1) · (1 − taux)`.

    POINT UNIQUE DU MODÈLE DE TAXE. Il vit ici, à côté du taux lui-même, et
    non dans `core/tax_engine.py`, pour deux raisons :
      · `core/constants.py` ne dépend de rien, donc `learning_layer`,
        `risk_manager` et le dashboard peuvent fiscaliser leurs calculs sans
        tirer scipy dans le bundle Vercel via `tax_engine` ;
      · un modèle de taxe recopié à trois endroits finit par diverger — c'est
        exactement ce qui s'était produit (ROI brut dans `learning_layer` et
        `calibration_report`, ROI net dans `weekly_report`).
    Si le bookmaker taxait le payout brut ou la mise à la pose, cette
    fonction est la SEULE à changer : tout le reste s'en compose.
    """
    return (odds - 1) * (1 - tax_rate)


def roi_net_of_tax(rows: list[dict], tax_rate: float = TAX_RATE) -> float | None:
    """
    ROI pondéré Kelly et NET DE TAXE sur des lignes d'`ai_learning_ledger`.

        ROI = Σ mise·net_b(cote)  si WIN, −mise sinon   /   Σ mise

    `kelly_pct` tient lieu de mise : c'est un pourcentage du MÊME bankroll de
    référence pour toutes les lignes, donc il se simplifie correctement dans
    le rapport sans qu'on ait besoin du montant en euros.

    Seules les lignes DÉCISIVES (WIN/LOSS) portant une mise ET une cote
    comptent. PUSH/expired/closed ne portent aucun résultat ; une ligne sans
    `kelly_pct` (avant migration) est écartée du ROI plutôt que de recevoir
    une mise inventée. Rend None quand rien n'est mesurable — jamais 0.0, qui
    se lirait comme « à l'équilibre ».

    Formule unique du dépôt : `learning_layer`, `weekly_report` et
    `calibration_report` en portaient chacun une copie, dont deux OUBLIAIENT
    la taxe. Gardé par `tests/test_taxe_reelle.py`.
    """
    staked = [r for r in rows
              if r.get("outcome") in ("WIN", "LOSS") and r.get("kelly_pct") and r.get("odds")]
    if not staked:
        return None
    numer = sum(r["kelly_pct"] * net_b(r["odds"], tax_rate) if r["outcome"] == "WIN"
                else -r["kelly_pct"] for r in staked)
    denom = sum(r["kelly_pct"] for r in staked)
    return numer / denom if denom else None

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

# ── Contre-expertise d'exchange (2026-08-27) ──────────────────────────
# Matchbook cessait d'être consulté dès qu'un prix Pinnacle existait
# (`run_engine._enrich_from_exchange` faisait `continue`). Or api-sports sert
# Pinnacle sur 100 % de ses matchs foot : l'exchange était donc écarté
# PRÉCISÉMENT sur les matchs qui portent les signaux. Câblé en bouche-trou, il
# ne pouvait pas jouer le rôle qui compte — détecter un Pinnacle PÉRIMÉ, qui
# est la fabrique à faux edge.
#
# Deux avis sharp INDÉPENDANTS qui divergent de plus de ce nombre de POINTS de
# probabilité ne peuvent pas être tous les deux à jour. On refuse alors le
# signal plutôt que de choisir : le désaccord est en soi l'information.
#
# LA VALEUR EST PROVISOIRE, et il faut le savoir avant de s'y fier. Mesurée le
# 2026-08-27 sur les seules paires Pinnacle × Matchbook appariables du jour :
#   n = 5 | médiane 0,23 pt | p90 0,87 pt | max 0,87 pt
# Cinq paires, TOUTES sur des marchés liquides (Barcelona, Celta Vigo,
# Estudiantes…). La divergence sur les marchés ILLIQUIDES — OBOS-ligaen,
# Argentine B, c'est-à-dire l'essentiel du slate réel — n'est PAS mesurée : le
# prix milieu d'un carnet creux s'écarte mécaniquement plus, sans qu'aucun des
# deux books ne soit périmé. 2,0 points laisse donc de la marge à cette
# illiquidité tout en restant à plus du double du bruit observé.
#
# C'est pourquoi `_enrich_from_exchange` logge CHAQUE comparaison, pas
# seulement les refus : c'est ce journal qui donnera à A6 la distribution
# réelle, celle qu'aucune mesure ponctuelle ne pouvait fournir.
#
# ⚠️ Ce garde recoupe `paim_engine._DIVERGENCE_CV_LIMIT` (1,2 % de CV), qui
# rejetterait les mêmes carnets sous l'étiquette VOLATILE — soit ~0,7 à 1,2
# point de probabilité selon le prix. Les deux mesurent la même chose dans
# deux unités. Celui-ci s'applique en AMONT et nomme la vraie cause ; A6
# devrait les unifier plutôt que de les laisser cohabiter.
EXCHANGE_DIVERGENCE_PTS = 2.0   # points de probabilité

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
CLOSING_SRC_ODDSAPI  = "oddsapi"    # exact, per-market, from the scan feed
CLOSING_SRC_ORACLE   = "oracle"     # web-search estimate, h2h/DNB favourite only
# Ajouté le 2026-08-26. OddsAPI étant obsolète, la voie `oddsapi` est morte et
# seul l'oracle restait — h2h favori, sur le budget Groq des scans. Les prix
# sharp Matchbook sont déjà chargés à chaque scan (fetch_matchbook_prices) et
# n'étaient utilisés que pour l'edge : les lire aussi pour la clôture donne un
# prix EXACT, gratuit et horaire. `signals.closing_source` est un `text` sans
# contrainte (sql/migrate_v9_12_closing_source.sql) : aucune migration.
CLOSING_SRC_EXCHANGE = "exchange"   # prix d'exchange réel (Matchbook/Betfair), h2h

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
    # ── Phase 3 (2026-08-22) — NCAAF + tennis Grand Chelem ────────────
    # Deux sports choisis pour la même raison : le favori COURT y est la
    # norme, et c'est la seule tranche que le ledger valide (81 % sous 1,50).
    # Fraction basse identique pour les deux — « non validé au ledger » vaut
    # plus que toute intuition de sharpness ; à réévaluer après 30 réglés.
    "college_football": 0.10,   # NCAAF — lignes moins sharp que la NFL, ne PAS hériter du 0.14
    "tennis":           0.10,   # Slams + Masters 1000 seulement (clés dynamiques)
}

# Sports RETIRÉS le 2026-08-22 (mission « recentrage sports ») : prix de
# référence issu d'une recherche web IA, jamais d'un book sharp — du bruit.
# run_engine._emit refuse tout signal pour ces sports ; les lignes
# historiques de `signals`/`ai_learning_ledger` sont CONSERVÉES (zéro perte
# de données) et leur settlement continue normalement.
RETIRED_SPORTS = frozenset({"esports", "tabletennis", "volleyball", "handball"})


def risk_flag(edge_pct: float, elite: float = ELITE_EDGE) -> str:
    """Consistent risk label stored in DB and used by all consumers.
    `elite` lets callers pass a sport-specific boundary (SOCCER_ELITE_EDGE,
    BASKETBALL_ELITE_EDGE) — defaults to the generic ELITE_EDGE."""
    if edge_pct >= elite * 2:
        return "HIGH_VALUE"
    if edge_pct >= elite:
        return "VALUE"
    return "LOW_VALUE"


def kelly_stake(executable_odd: float, sharp_prob: float,
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
    b = executable_odd - 1   # gain net par unité misée, au prix RÉELLEMENT jouable
    if b <= 0 or sharp_prob <= 0:
        return 0
    effective_bankroll = max(0.0, bankroll - current_exposure)
    if effective_bankroll <= 0:
        return 0
    kf = (sharp_prob * b - (1 - sharp_prob)) / b
    fraction = KELLY_FRACTION.get(sport, 0.12)   # fallback also inside Task 10's temporary 0.10-0.15 band
    stake = round(max(0.0, kf * fraction) * effective_bankroll)
    return stake if stake >= MIN_STAKE else 0
