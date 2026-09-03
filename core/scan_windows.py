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
_ALL = None
_WINDOWS: dict[str, list[tuple]] = {
    # KBO/NPB : lag Asie documenté 06:00–13:00
    "baseball_kbo":                      [(_ALL, 6, 13)],
    "baseball_npb":                      [(_ALL, 6, 13)],
    # Big 5 + coupes d'Europe : soirée européenne 17:00–22:00
    "soccer_epl":                        [(_ALL, 17, 22)],
    "soccer_spain_la_liga":              [(_ALL, 17, 22)],
    "soccer_germany_bundesliga":         [(_ALL, 17, 22)],
    "soccer_italy_serie_a":              [(_ALL, 17, 22)],
    "soccer_france_ligue_one":           [(_ALL, 17, 22)],
    "soccer_uefa_champs_league":         [(_ALL, 17, 22)],
    "soccer_uefa_europa_league":         [(_ALL, 17, 22)],
    # Euroleague : jeudi/vendredi soir
    "basketball_euroleague":             [((3, 4), 17, 22)],
    # Amérique du Sud + MLB : 21:00–02:00
    "soccer_brazil_campeonato":          [(_ALL, 21, 24), (_ALL, 0, 2)],
    "soccer_conmebol_copa_libertadores": [(_ALL, 21, 24), (_ALL, 0, 2)],
    "soccer_argentina_primera_division": [(_ALL, 21, 24), (_ALL, 0, 2)],
    "soccer_mexico_ligamx":              [(_ALL, 21, 24), (_ALL, 0, 2)],
    "soccer_usa_mls":                    [(_ALL, 21, 24), (_ALL, 0, 2)],
    "baseball_mlb":                      [(_ALL, 21, 24), (_ALL, 0, 2)],
    # NFL : jeudi soir US (= vendredi 00:20 UTC), dimanche 17:00/20:15/00:20,
    # lundi 00:15 UTC
    "americanfootball_nfl":              [((4,), 0, 4), ((6,), 16, 24), ((0,), 0, 4)],
    # NBA/WNBA/NHL : tip-off 22:00–04:00
    "basketball_nba":                    [(_ALL, 22, 24), (_ALL, 0, 4)],
    "basketball_wnba":                   [(_ALL, 22, 24), (_ALL, 0, 4)],
    "icehockey_nhl":                     [(_ALL, 22, 24), (_ALL, 0, 4)],
    # Australie : journée/soirée AU = matin UTC
    "aussierules_afl":                   [(_ALL, 2, 12)],
    "rugbyleague_nrl":                   [(_ALL, 5, 12)],
    # Combat : vendredi–dimanche autour des cartes
    "mma_mixed_martial_arts":            [((4, 5, 6), 0, 24)],
    "boxing_boxing":                     [((4, 5, 6), 0, 24)],
    # NCAAF : jeudi/vendredi soir US (= 22:00 UTC → 05:00 UTC) et samedi
    # toute la journée/nuit US — le gros du volume.
    "americanfootball_ncaaf":            [((3, 4), 22, 24), ((4, 5), 0, 5),
                                          ((5,), 16, 24), ((6,), 0, 5)],
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
