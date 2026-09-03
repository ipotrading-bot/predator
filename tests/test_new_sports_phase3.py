"""
tests/test_new_sports_phase3.py — Phase 3 du recentrage sports (2026-08-22).

NCAAF (sport-type `college_football`) et tennis majeur (clés DYNAMIQUES)
entrent dans le périmètre. Choisis tous deux pour la même raison, mesurée sur
254 paris : le favori COURT y est la norme, et la tranche < 1,50 est la seule
où le ledger est rentable (81 % de réussite, +2,2 u ; les tranches 1,50–2,20
perdent). C'est l'inverse de la question posée (« sports à grosses cotes ») :
le moteur ne considère que les sélections à ≥ 50–55 % de probabilité sharp
(SHARP_PROB_BY_MARKET), il ne parierait JAMAIS un outsider, quel que soit le
sport — 1 pari sur 254 au-dessus de 2,20, perdu.

NCAAF a un sport-type DÉDIÉ, pas `americanfootball` : même raisonnement que
`euroleague_basketball` vs `basketball` — Kelly basse tant que non validé, et
un contexte de settlement qui ne dit pas « NFL » pour un match universitaire.
"""
import run_engine
from core import odds_api, scan_windows
from core.constants import KELLY_FRACTION
from core.learning_layer import SPORT_DEFAULTS
from core.odds_api import SPORT_KEYS, _MARKETS_BY_SPORT
from core.paim_engine import _SPORT_PFX


class TestNCAAF:
    def test_cle_et_sport_type_dedie(self):
        assert SPORT_KEYS["americanfootball_ncaaf"] == "college_football"
        # Le sport-type NFL reste intact, on n'a rien fusionné.
        assert SPORT_KEYS["americanfootball_nfl"] == "americanfootball"

    def test_kelly_basse_et_sous_la_nfl(self):
        """Non validé au ledger = 0.10, comme la boxe et le MMA à leur entrée.
        Et strictement sous la NFL : les lignes universitaires sont moins
        sharp, hériter du 0.14 serait une affirmation sans mesure."""
        assert KELLY_FRACTION["college_football"] == 0.10
        assert KELLY_FRACTION["college_football"] < KELLY_FRACTION["americanfootball"]

    def test_miroir_des_mecaniques_nfl(self):
        assert _MARKETS_BY_SPORT["college_football"] == _MARKETS_BY_SPORT["americanfootball"]
        assert "college_football" in run_engine._MAJOR_SPORTS     # cap SUSPECT
        assert _SPORT_PFX["college_football"] == "CFB"

    def test_contexte_de_settlement_ne_dit_pas_nfl(self):
        """La recherche web du score est guidée par ce libellé : « NFL
        american football » pour un match NCAA l'enverrait vers la mauvaise
        ligue, et un score introuvable laisse le signal `active` pour
        toujours."""
        from core import score_sources
        assert score_sources._TSDB_SPORTS["college_football"] == "American Football"
        assert score_sources._TSDB_SPORTS["americanfootball"] == "American Football"

    def test_fenetre_favorable_le_samedi(self):
        from datetime import datetime, timezone
        samedi_20h_utc = datetime(2026, 9, 5, 20, 0, tzinfo=timezone.utc)   # samedi
        mardi_20h_utc  = datetime(2026, 9, 1, 20, 0, tzinfo=timezone.utc)   # mardi
        assert scan_windows.is_favorable("americanfootball_ncaaf", samedi_20h_utc)
        assert not scan_windows.is_favorable("americanfootball_ncaaf", mardi_20h_utc)

    def test_pas_de_garde_de_saison(self):
        """Pas de présaison en NCAA : le pré-vol gratuit suffit. Une date
        ici serait une deuxième source de vérité à tenir à jour."""
        assert "americanfootball_ncaaf" not in odds_api.SEASON_OPENS


class TestInvariantDesQuatreFichiers:
    """Le test de la Phase 2 le garde pour SPORT_KEYS ; on l'étend ici aux
    deux sport-types de la Phase 3 — dont `tennis`, qui n'apparaît dans
    SPORT_KEYS qu'à l'exécution (clés dynamiques) et que le test statique ne
    verrait donc pas."""

    NOUVEAUX = ("college_football", "tennis")

    def test_kelly_seuils_quotas_emoji(self):
        from api.index import (_SPORT_EMOJI, _SPORT_LABEL, _SPORT_LABEL_SHORT,
                               _SPORT_ORDER)
        for s in self.NOUVEAUX:
            assert s in KELLY_FRACTION, s
            assert s in SPORT_DEFAULTS, s
            assert s in run_engine.SPORT_QUOTA, s
            assert s in run_engine.SPORT_EMOJI, s
            assert s in run_engine._SPORT_ORDER, s
            for tbl in (_SPORT_EMOJI, _SPORT_LABEL, _SPORT_LABEL_SHORT, _SPORT_ORDER):
                assert s in tbl, s
            # La vérification des requêtes Wiz par sport a disparu avec Wiz
            # (page + moteur supprimés le 2026-08-26). Le reste de ce test —
            # l'invariant des sport-keys à travers Kelly, quotas, emoji et
            # libellés du dashboard — est intact.

    def test_aucun_sport_actif_retire(self):
        for key in ("americanfootball_nfl", "basketball_euroleague",
                    "soccer_uefa_champs_league", "mma_mixed_martial_arts",
                    "boxing_boxing", "basketball_wnba", "baseball_mlb"):
            assert key in SPORT_KEYS, key
