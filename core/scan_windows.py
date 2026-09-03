"""
core/scan_windows.py — fenêtres favorables et politique de dépense OddsAPI.

POURQUOI
--------
Le plan OddsAPI fait 500 crédits/mois PAR CLÉ et le tarif se paie par LIGUE
peuplée (marchés × régions : 3 pour un `h2h,spreads,totals`, 2 pour le
baseball, 1 pour MMA/boxe). Dépenser uniformément sur 24h, c'est payer à
04:00 UTC un scan de la Bundesliga dont la ligne n'a pas bougé depuis
minuit. La valeur d'un scan est concentrée dans des FENÊTRES : l'heure où
les books soft traînent derrière le sharp pour CE marché (lag de fuseau,
veille de match, jour de carte). Hors fenêtre, un scan de fond suffit — il
garde la couverture 24h sans trou, mais espacé.

TROIS RÈGLES, toutes loggées (aucun scan utile supprimé en silence) :
1. pré-vol 0 crédit systématique avant tout appel payant (core/odds_api.py,
   existait déjà) ;
2. hors fenêtre favorable, jamais deux scans payants de la MÊME ligue à
   moins de BACKGROUND_MIN_INTERVAL_MIN (180 min) d'intervalle. Pourquoi
   180 : c'est la cadence du mode `standard` de scan.yml depuis le
   2026-08-22 (8 ticks sur les fenêtres favorables) — le tick horaire
   `reprice` ne paie rien, seul le standard atteint ce module ; dans une
   fenêtre favorable une ligue est repayée, et c'est voulu (c'est là que
   la ligne bouge) ;
3. garde de réserve : si le pool descend sous RESERVE_CREDITS, seuls les
   scans en fenêtre favorable et ceux qui servent la capture de closing
   line (sports avec un signal actif à < CLOSING_LINE_WINDOW_MIN du coup
   d'envoi) restent payés — les scans de fond s'espacent d'eux-mêmes.

La carte des fenêtres (UTC) vient de la mission « recentrage sports »
(2026-08-22) et du lag mesuré dans le ledger (voir predator-pipeline skill).
`CLOSING_LINE_BUDGET` (run_closing_line.py) n'est PAS touché par ce module.
"""
import os
from datetime import datetime, timezone

# ── Paramètres ────────────────────────────────────────────────────────
BACKGROUND_MIN_INTERVAL_MIN = int(os.environ.get("BACKGROUND_MIN_INTERVAL_MIN", "180"))
# Réserve journalière : ≈ 3 scans engine complets en fenêtre favorable
# (≈ 15-20 crédits chacun) — de quoi finir la journée sur les fenêtres qui
# comptent même si le fond a tout mangé. Surchargeable par l'env.
RESERVE_CREDITS = int(os.environ.get("ODDS_API_RESERVE_CREDITS", "60"))

# ── Carte des fenêtres favorables (UTC) ───────────────────────────────
# (jours, heure_début, heure_fin) — heure_fin exclue ; une fenêtre qui passe
# minuit est écrite en deux morceaux. jours : 0=lundi … 6=dimanche, None=tous.
# Une fenêtre est l'heure d'un SCAN (T-2h30 à T-6h du coup d'envoi), pas
# l'heure du match — voir le bloc de la carte ci-dessous.
_ALL = None
# ── Carte recalée sur les 8 scans standard (2026-09-03) ───────────────
# Scans : 06:03 · 09:03 · 11:03 · 13:03 · 16:03 · 19:03 · 21:03 · 23:03 UTC
# (scripts/ci_scan_mode.py). Une fenêtre = les scans qui tombent entre
# T-2h30 et ~T-6h des coups d'envoi de la ligue : assez tôt pour que le
# signal sorte RECOMMANDÉ (T-2h = fantôme, core/learning_layer), assez tard
# pour que la ligne soit mûre. Un scan à moins de 2 h des coups d'envoi
# n'achetait que des fantômes — mesuré le 2026-09-03 : les scans de 16:04,
# 19:50 et 21:00 étaient 100 % fantômes sur le Big 5 (coups d'envoi
# 16:45–19:30 UTC en septembre). Le pré-vol de core/odds_api saute de toute
# façon une ligue dont plus aucun match n'est jouable : la fenêtre dit OÙ
# le crédit vaut le plus, le pré-vol empêche de le perdre.
# Jours : 0=lundi … 6=dimanche ; heure_fin exclue.
_WINDOWS: dict[str, list[tuple]] = {
    # KBO/NPB : coups d'envoi 09:00–10:30 UTC → scan de 06:03 seulement
    "baseball_kbo":                      [(_ALL, 5, 8)],
    "baseball_npb":                      [(_ALL, 5, 8)],
    # Big 5 + coupes d'Europe : soirée 16:45–19:30 UTC → 13:03 et 16:03 ;
    # week-end, coups d'envoi dès 11:30 → 09:03 et 11:03 en plus
    "soccer_epl":                        [((5, 6), 9, 13), (_ALL, 13, 17)],
    "soccer_spain_la_liga":              [((5, 6), 9, 13), (_ALL, 13, 17)],
    "soccer_germany_bundesliga":         [((5, 6), 9, 13), (_ALL, 13, 17)],
    "soccer_italy_serie_a":              [((5, 6), 9, 13), (_ALL, 13, 17)],
    "soccer_france_ligue_one":           [((5, 6), 9, 13), (_ALL, 13, 17)],
    "soccer_uefa_champs_league":         [(_ALL, 13, 17)],
    "soccer_uefa_europa_league":         [(_ALL, 13, 17)],
    # Euroleague : jeudi/vendredi 16:45–19:00 UTC → 13:03 et 16:03
    "basketball_euroleague":             [((3, 4), 13, 17)],
    # Amérique du Sud : 21:30–02:30 UTC → 19:03, 21:03, 23:03
    "soccer_brazil_campeonato":          [(_ALL, 19, 24)],
    "soccer_conmebol_copa_libertadores": [(_ALL, 19, 24)],
    "soccer_argentina_primera_division": [(_ALL, 19, 24)],
    "soccer_mexico_ligamx":              [(_ALL, 19, 24)],
    "soccer_usa_mls":                    [(_ALL, 19, 24)],
    # MLB : matinées 17:05–20:10 UTC → 13:03/16:03 ; soirées 23:05–02:40 → 19:03+
    "baseball_mlb":                      [(_ALL, 13, 17), (_ALL, 19, 24)],
    # NFL : jeudi soir US = vendredi 00:20 UTC (payé jeudi 21:03) ; dimanche
    # 17:00 / 20:25 / 00:20 (13:03 → 23:03) ; lundi soir = mardi 00:15 (21:03)
    "americanfootball_nfl":              [((3,), 21, 24), ((6,), 13, 24), ((0,), 21, 24)],
    # NBA/WNBA/NHL : tip-off 23:00–03:30 UTC → 19:03, 21:03, 23:03
    "basketball_nba":                    [(_ALL, 19, 24)],
    "basketball_wnba":                   [(_ALL, 19, 24)],
    "icehockey_nhl":                     [(_ALL, 19, 24)],
    # Australie : AFL/NRL 03:00–10:00 UTC → 23:03 la veille et 06:03
    "aussierules_afl":                   [(_ALL, 23, 24), (_ALL, 5, 8)],
    "rugbyleague_nrl":                   [(_ALL, 23, 24), (_ALL, 5, 8)],
    # Combat : cartes du vendredi au dimanche, 22:00–04:00 UTC → 19:03+
    "mma_mixed_martial_arts":            [((4, 5, 6), 19, 24)],
    "boxing_boxing":                     [((4, 5, 6), 19, 24)],
    # NCAAF : jeudi/vendredi 23:00–03:00 UTC → 19:03+ ; samedi 16:00–04:00
    # → 13:03 jusqu'à 23:03
    "americanfootball_ncaaf":            [((3, 4), 19, 24), ((5,), 13, 24)],
}

# Fenêtres par PRÉFIXE de clé — pour les ligues dont la clé exacte n'existe
# pas à l'avance. Le tennis en est le cas : `tennis_atp_us_open` n'apparaît
# au catalogue OddsAPI que quelques jours avant le tournoi (voir
# core.odds_api.discover_tennis_keys). Sans cette table, is_favorable() les
# traiterait comme « jamais favorable » et ne les paierait qu'en scan de
# fond — c'est-à-dire à peu près jamais pendant un Slam.
# Sessions : US Open 15:00→04:00 UTC, Slams européens 09:00→21:00, Australie
# 00:00→12:00 ; la fenêtre couvre l'union — c'est une fenêtre de dépense,
# pas une règle métier, être large ne coûte que des crédits sur des matchs
# qui existent vraiment.
_PREFIX_WINDOWS: dict[str, list[tuple]] = {
    "tennis_": [(_ALL, 9, 24), (_ALL, 0, 4)],
}


def is_favorable(sport_key: str, now: datetime | None = None) -> bool:
    """True si `now` (UTC) tombe dans une fenêtre favorable de cette ligue.
    Une ligue sans fenêtre déclarée est traitée comme JAMAIS favorable
    (scan de fond seulement) — ajouter une entrée plutôt que d'ouvrir tout."""
    now = now or datetime.now(timezone.utc)
    wd, hour = now.weekday(), now.hour
    windows = _WINDOWS.get(sport_key)
    if windows is None:
        # Clé inconnue : une fenêtre par préfixe s'applique-t-elle ?
        windows = next((w for pfx, w in _PREFIX_WINDOWS.items()
                        if sport_key.startswith(pfx)), [])
    for days, start, end in windows:
        if days is not None and wd not in days:
            continue
        if start <= hour < end:
            return True
    return False


def favorable_leagues(now: datetime | None = None) -> set[str]:
    return {k for k in _WINDOWS if is_favorable(k, now)}


# ── Rythme mensuel (2026-09-01) ───────────────────────────────────────
# Décision opérateur : « 1 mois seulement, maximum d'utilisation, suffisant
# pour tenir 30 jours ». Le pool (5 comptes × 500) doit être DÉPENSÉ EN
# ENTIER sur le cycle, jamais avant sa fin. Ce n'est pas le gouverneur retiré
# le 2026-08-01 (« ne pas rationner ») : celui-ci étalait un budget que
# l'opérateur voulait brûler ; celui-là vise 100 % du pool, à la bonne
# vitesse. Trois mécanismes, tous loggés :
#   1. allocation du jour = crédits restants du POOL ÷ jours restants du cycle
#      — recalculée à chaque scan, donc l'inutilisé d'un jour creux est
#      reporté sur les suivants (« maximum d'utilisation ») ;
#   2. plafond intra-journée linéaire (INTRADAY_LEAD_H) : à 02:00 UTC on ne
#      peut engager que ~15 % de l'allocation, à 22:00 la totalité — le tick
#      de nuit (SA/MLB) ne mange pas la soirée Big 5, qui porte le volume ;
#   3. parts par priorité : closing line imminente jusqu'à EXEMPT_SHARE,
#      fenêtre favorable / golden (T-2h) jusqu'à 100 %, scan de fond jusqu'à
#      BACKGROUND_SHARE — le fond ne préempte jamais les fenêtres.
# ODDS_API_PACING=0 remet l'ancien comportement (paie tout, réserve seule).
PACING_ENABLED = os.environ.get("ODDS_API_PACING", "1") == "1"
CYCLE_DAYS = float(os.environ.get("ODDS_API_CYCLE_DAYS", "30"))
BACKGROUND_SHARE = float(os.environ.get("ODDS_API_BACKGROUND_SHARE", "0.5"))
EXEMPT_SHARE = float(os.environ.get("ODDS_API_EXEMPT_SHARE", "1.1"))
INTRADAY_LEAD_H = float(os.environ.get("ODDS_API_INTRADAY_LEAD_H", "2"))


def daily_allowance(pool_remaining: int | float | None, days_left: float | None) -> float | None:
    """Crédits à engager aujourd'hui pour finir le pool à la fin du cycle.
    None si l'un des deux est inconnu : pas de rythme, on paie comme avant."""
    if pool_remaining is None or days_left is None:
        return None
    return max(0.0, float(pool_remaining)) / max(1.0, float(days_left))


def intraday_cap(allowance: float, now: datetime) -> float:
    """Part de l'allocation engageable à cette heure UTC : linéaire, 100 %
    atteint à (24 - INTRADAY_LEAD_H) h. La journée démarre douce pour que le
    soir — les fenêtres qui comptent — trouve encore du budget."""
    frac = (now.hour + now.minute / 60.0 + INTRADAY_LEAD_H) / 24.0
    return allowance * min(1.0, max(0.0, frac))


class SpendPolicy:
    """Décide, ligue par ligue, si un scan payant est autorisé maintenant.

    `last_paid_age_min(sport_key)` / `note_paid(sport_key)` sont fournis par
    l'appelant (run_engine : horodatages dans meta) — ce module ne connaît
    pas Supabase. `exempt_sports` : sport-types ayant un signal actif proche
    du coup d'envoi (capture de closing line) — jamais espacés.

    Rythme mensuel (optionnel) : `allowance` (crédits du jour), `spent_today`
    (déjà engagés aujourd'hui, toutes exécutions confondues) et `note_spent`
    (persiste les crédits payés). (Le rang « golden T-2h », qui traitait toute
    ligue peuplée au rang fenêtre en mode golden, est parti avec ce mode le
    2026-09-03.) Sans politique (None), core/odds_api.fetch_odds paie comme
    avant.
    """

    def __init__(self, last_paid_age_min, note_paid, exempt_sports=frozenset(),
                 min_interval_min: int = BACKGROUND_MIN_INTERVAL_MIN,
                 reserve_credits: int = RESERVE_CREDITS, log=None,
                 allowance: float | None = None, spent_today: float = 0.0,
                 note_spent=None, reglables=None):
        self._age = last_paid_age_min
        self._note = note_paid
        self._note_spent = note_spent
        self.exempt_sports = set(exempt_sports)
        # Sports RÉGLABLES (core/score_sources.sports_reglables) : liste
        # d'autorisation, `None` = pas de contrôle. Un sport hors liste (boxe,
        # tennis — y compris les clés dynamiques que SPORT_KEYS ne connaît pas)
        # n'est pas payé : le périmètre refuserait ses matchs à l'émission, et
        # un crédit sur un pari qu'on ne saura jamais régler est perdu deux
        # fois (2026-09-03 : tennis US Open payé à 16:01 pour rien).
        self.reglables = None if reglables is None else set(reglables)
        self.min_interval = min_interval_min
        self.reserve = reserve_credits
        self.log = log
        self.allowance = allowance if PACING_ENABLED else None
        self.spent_today = float(spent_today)
        self.engaged = 0.0            # crédits accordés par allow() dans CE scan
        self.skipped: list[tuple[str, str]] = []   # (sport_key, raison) — pour le rapport

    # ── Rythme ──
    def _cap(self, share: float, now: datetime, intraday: bool) -> float | None:
        if self.allowance is None:
            return None
        base = intraday_cap(self.allowance, now) if intraday else self.allowance
        return base * share

    def _within(self, cost: float, share: float, now: datetime, intraday: bool):
        cap = self._cap(share, now, intraday)
        if cap is None:
            return True, ""
        projected = self.spent_today + self.engaged + cost
        if projected <= cap + 1e-9:
            return True, ""
        return False, (f"rythme : {self.spent_today + self.engaged:.0f} engagés aujourd'hui "
                       f"+ {cost:.0f} > plafond {cap:.0f} (allocation {self.allowance:.0f}/j)")

    def budget_left(self, now: datetime) -> float | None:
        """Crédits encore engageables à cette heure (rang fenêtre) — pour le log."""
        cap = self._cap(1.0, now, True)
        return None if cap is None else max(0.0, cap - self.spent_today - self.engaged)

    def allow(self, sport_key: str, sport_type: str, now: datetime,
              pool_remaining: int | None, cost: float = 3.0) -> tuple[bool, str]:
        if self.reglables is not None and sport_type not in self.reglables:
            reason = "sport non réglable (aucune source de scores) — pas payé"
            self._skip(sport_key, reason)
            return False, reason
        if sport_type in self.exempt_sports:
            ok, why = self._within(cost, EXEMPT_SHARE, now, intraday=False)
            if ok:
                self.engaged += cost
                return True, "closing line imminente"
            self._skip(sport_key, "closing line imminente mais " + why)
            return False, why
        if is_favorable(sport_key, now):
            rank = "fenêtre favorable"
            ok, why = self._within(cost, 1.0, now, intraday=True)
            if ok:
                self.engaged += cost
                return True, rank
            self._skip(sport_key, f"{rank} mais {why}")
            return False, why
        age = self._age(sport_key)
        if age is not None and age < self.min_interval:
            reason = (f"scan de fond : déjà payé il y a {age:.0f} min "
                      f"(< {self.min_interval})")
            self._skip(sport_key, reason)
            return False, reason
        if pool_remaining is not None and pool_remaining < self.reserve:
            reason = (f"réserve : pool à {pool_remaining} crédits "
                      f"(< {self.reserve}) — fond espacé, fenêtres favorables "
                      f"et closing line prioritaires")
            self._skip(sport_key, reason)
            return False, reason
        ok, why = self._within(cost, BACKGROUND_SHARE, now, intraday=True)
        if not ok:
            self._skip(sport_key, "scan de fond : " + why)
            return False, why
        self.engaged += cost
        return True, "scan de fond"

    def note_paid(self, sport_key: str, cost: float | None = None) -> None:
        try:
            self._note(sport_key)
        except Exception:
            pass
        if cost and self._note_spent is not None:
            try:
                self._note_spent(float(cost))
            except Exception:
                pass

    def _skip(self, sport_key: str, reason: str) -> None:
        self.skipped.append((sport_key, reason))
        if self.log:
            self.log.info("DÉPENSE | %s sauté — %s", sport_key, reason)
