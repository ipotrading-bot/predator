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
   2026-08-22 (8 ticks sur les fenêtres favorables) — un tick golden
   hour (horaire, fenêtre 2h) entre deux scans engine ne repaie donc plus
   une ligue au repos ; dans une fenêtre favorable il la repaie, et c'est
   voulu (c'est là que la ligne bouge) ;
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


class SpendPolicy:
    """Décide, ligue par ligue, si un scan payant est autorisé maintenant.

    `last_paid_age_min(sport_key)` / `note_paid(sport_key)` sont fournis par
    l'appelant (run_engine : horodatages dans meta) — ce module ne connaît
    pas Supabase. `exempt_sports` : sport-types ayant un signal actif proche
    du coup d'envoi (capture de closing line) — jamais espacés.
    Sans politique (None), core/odds_api.fetch_odds paie comme avant.
    """

    def __init__(self, last_paid_age_min, note_paid, exempt_sports=frozenset(),
                 min_interval_min: int = BACKGROUND_MIN_INTERVAL_MIN,
                 reserve_credits: int = RESERVE_CREDITS, log=None):
        self._age = last_paid_age_min
        self._note = note_paid
        self.exempt_sports = set(exempt_sports)
        self.min_interval = min_interval_min
        self.reserve = reserve_credits
        self.log = log
        self.skipped: list[tuple[str, str]] = []   # (sport_key, raison) — pour le rapport

    def allow(self, sport_key: str, sport_type: str, now: datetime,
              pool_remaining: int | None) -> tuple[bool, str]:
        if is_favorable(sport_key, now):
            return True, "fenêtre favorable"
        if sport_type in self.exempt_sports:
            return True, "closing line imminente"
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
        return True, "scan de fond"

    def note_paid(self, sport_key: str) -> None:
        try:
            self._note(sport_key)
        except Exception:
            pass

    def _skip(self, sport_key: str, reason: str) -> None:
        self.skipped.append((sport_key, reason))
        if self.log:
            self.log.info("DÉPENSE | %s sauté — %s", sport_key, reason)
