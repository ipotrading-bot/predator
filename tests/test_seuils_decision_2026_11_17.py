"""
tests/test_seuils_decision_2026_11_17.py — deux seuils d'émission décidés par
l'opérateur le 2026-11-17 (« 1 et 2 oui »), après « nfl est pauvre, tennis est
toujours pauvre ».

  1. Tennis : `meta.threshold_tennis = 1.5`, posé à la main — la cible que
     `_verdict_seuil` rendait déjà (gain prouvé) et que la règle des 20 réglés
     neufs retenait encore. Le DÉFAUT du sport ne bouge pas.
  2. NFL : défaut du sport à 1,5 %, sans preuve (recommandés 7-8, n=15) ;
     ~1 pari de plus par semaine d'après cinq semaines de logs. Le défaut et
     non une clé meta : sous _MIN_SAMPLES réglés, tout seuil posé est retiré.

Gardés ici : les deux valeurs, le plancher du moteur, et le fait qu'un seuil
posé à sa cible n'est ni réécrit ni retiré.
"""
from core import learning_layer as ll
from core.constants import EV_EDGE_FLOOR


class TestNFL:
    def test_le_defaut_est_le_plancher_du_moteur(self):
        assert ll.SPORT_DEFAULTS["americanfootball"] == EV_EDGE_FLOOR == 1.5

    def test_une_perte_prouvee_le_remontera_seule(self):
        assert ll._cible_seuil("perte", ll.SPORT_DEFAULTS["americanfootball"]) == ll._THRESHOLD_MAX
        assert ll._cible_seuil("neutre", ll.SPORT_DEFAULTS["americanfootball"]) is None

    def test_les_sports_voisins_ne_bougent_pas(self):
        for sport in ("hockey", "tennis", "mma", "euroleague_basketball"):
            assert ll.SPORT_DEFAULTS[sport] == 2.0


class TestTennis:
    def test_la_valeur_posee_est_la_cible_du_gain_prouve(self):
        assert ll._cible_seuil("gain", ll.SPORT_DEFAULTS["tennis"]) == 1.5

    def test_un_seuil_deja_a_sa_cible_nest_ni_reecrit_ni_retire(self):
        """Le posé à la main ne rejoue aucun cliquet : aucune écriture, et la
        base du dernier mouvement n'est pas touchée."""
        class _SB:
            def table(self, _t):
                raise AssertionError("aucune écriture attendue")
        bases = {"threshold_tennis": "37:abc"}
        tenu = ll._poser_ou_retirer(_SB(), "threshold_tennis", "tennis", 1.5, 1.5, 44, "44:def",
                                    bases, "gain prouvé", "tennis", 2.0, [], "2026-11-17T00:00:00")
        assert tenu == 1.5 and bases == {"threshold_tennis": "37:abc"}

    def test_un_verdict_redevenu_neutre_attend_vingt_regles_neufs(self):
        class _SB:
            def table(self, _t):
                raise AssertionError("aucune écriture attendue")
        bases = {"threshold_tennis": "37:abc"}
        tenu = ll._poser_ou_retirer(_SB(), "threshold_tennis", "tennis", 1.5, None, 44, "44:def",
                                    bases, "neutre", "tennis", 2.0, [], "2026-11-17T00:00:00")
        assert tenu == 1.5
