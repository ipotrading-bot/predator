"""
tests/test_totaux_tennis.py — un total ne se règle QU'AVEC un décompte
(2026-09-17).

ESPN ne publie pas de score chiffré pour un match de tennis ni pour un
combat : seulement un drapeau `winner`, d'où le 1-0 de
`score_sources._espn_candidat`. Ce 1-0 réglait les TOTAUX comme un score :
1 + 0 = 1, donc tout « Moins de » sortait GAGNANT et tout « Plus de »
PERDANT, quelle que soit la ligne. Les trois totaux tennis du ledger
portaient déjà cette signature — justes par coïncidence, les lignes étant
hautes.

Depuis : le décompte réel (les JEUX, `linescores` d'ESPN) règle les totaux et
les handicaps ; le drapeau ne règle plus que le h2h ; sans décompte, UNKNOWN.
"""
from core.settlement import determine_outcome
from core.score_sources import SPORTS_SCORE_DRAPEAU, _espn_decompte


def _tennis(market, selection, hs=1, as_=0, decompte=None):
    return determine_outcome("tennis", market, selection, "A", "B", hs, as_, decompte)


class TestSansDecompteOnNeReglePas:
    def test_un_total_tennis_sur_un_drapeau_rend_unknown(self):
        """Le bug : ces deux-là rendaient WIN et LOSS mécaniquement."""
        assert _tennis("totals_under", "Under 34.5") == "UNKNOWN"
        assert _tennis("totals_over", "Over 22.5") == "UNKNOWN"

    def test_un_handicap_tennis_sur_un_drapeau_rend_unknown(self):
        assert _tennis("spreads_home", "A -4.5") == "UNKNOWN"

    def test_le_mma_aussi(self):
        assert determine_outcome("mma", "totals_over", "Over 2.5",
                                 "A", "B", 1, 0) == "UNKNOWN"

    def test_un_appelant_sans_decompte_ne_peut_pas_inventer(self):
        """relance_expires et backfill_expired_results appellent sans
        décompte : ils doivent rendre UNKNOWN, pas une issue devinée."""
        for decompte in (None, (), (7,), ("x", "y")):
            assert _tennis("totals_under", "Under 20.5", decompte=decompte) == "UNKNOWN"


class TestAvecLeDecompteOnRegleJuste:
    def test_les_jeux_reels_tranchent(self):
        # 27 jeux : sous 34.5, au-dessus de 22.5.
        assert _tennis("totals_under", "Under 34.5", decompte=(13, 14)) == "WIN"
        assert _tennis("totals_over", "Over 22.5", decompte=(13, 14)) == "WIN"
        assert _tennis("totals_under", "Under 22.5", decompte=(13, 14)) == "LOSS"

    def test_la_ligne_pile_est_un_push(self):
        assert _tennis("totals_over", "Over 20.0", decompte=(13, 7)) == "PUSH"

    def test_le_h2h_garde_le_drapeau(self):
        """En tennis le vainqueur peut avoir MOINS de jeux (6-0 6-7 6-7) :
        on ne déduit jamais un vainqueur d'un décompte."""
        assert determine_outcome("tennis", "h2h", "A", "A", "B", 1, 0) == "WIN"
        assert determine_outcome("tennis", "h2h", "B", "A", "B", 1, 0) == "LOSS"
        # Le décompte, même contraire, ne change pas un h2h.
        assert determine_outcome("tennis", "h2h", "A", "A", "B", 1, 0,
                                 decompte=(18, 14)) == "WIN"
        assert determine_outcome("tennis", "h2h", "B", "A", "B", 0, 1,
                                 decompte=(18, 14)) == "WIN"


class TestLesTroisLignesDuLedger:
    """Rejeu des trois totaux tennis réglés avant le correctif, sur les jeux
    RÉELS lus chez ESPN le 2026-09-17. Ils tombaient juste ; ce test existe
    pour que le correctif ne les retourne pas — et pour montrer d'où venait
    la coïncidence."""

    def test_shelton_tsitsipas_27_jeux_under_39_5(self):
        assert _tennis("totals_under", "Under 39.5", decompte=(9, 18)) == "WIN"

    def test_zverev_van_de_zandschulp_27_jeux_under_34_5(self):
        assert _tennis("totals_under", "Under 34.5", decompte=(8, 19)) == "WIN"

    def test_sabalenka_pegula_20_jeux_over_22_5(self):
        assert _tennis("totals_over", "Over 22.5", decompte=(7, 13)) == "LOSS"


class TestLeDecompteVientDesLinescores:
    def test_les_jeux_sont_sommes_set_par_set(self):
        a = {"linescores": [{"value": 6.0, "tiebreak": 7}, {"value": 6.0}, {"value": 6.0}]}
        b = {"linescores": [{"value": 7.0, "tiebreak": 9}, {"value": 3.0}, {"value": 2.0}]}
        assert _espn_decompte(a, b) == (18, 12)

    def test_un_camp_sans_ligne_de_score_ne_donne_aucun_decompte(self):
        """On ne complète pas un décompte : mieux vaut UNKNOWN."""
        assert _espn_decompte({"linescores": [{"value": 6.0}]}, {}) is None
        assert _espn_decompte({}, {}) is None
        assert _espn_decompte({"linescores": [{"value": "?"}]},
                              {"linescores": [{"value": 6.0}]}) is None

    def test_les_sports_a_drapeau_sont_nommes_une_fois(self):
        assert SPORTS_SCORE_DRAPEAU == {"tennis", "mma"}


class TestNonRegressionDesSportsChiffres:
    def test_le_foot_et_le_basket_ne_changent_pas(self):
        assert determine_outcome("soccer", "totals_over", "Over 2.5",
                                 "A", "B", 2, 1) == "WIN"
        assert determine_outcome("soccer", "totals_under", "Under 2.5",
                                 "A", "B", 2, 1) == "LOSS"
        assert determine_outcome("basketball", "spreads_home", "A -5.5",
                                 "A", "B", 110, 100) == "WIN"
        assert determine_outcome("soccer", "spreads_away", "B +1.5",
                                 "A", "B", 2, 1) == "WIN"
