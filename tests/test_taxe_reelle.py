"""
tests/test_taxe_reelle.py — PHASES A2 et A3.

Le bookmaker retient 20 % sur le gain net d'un pari gagnant. Le moteur
calculait comme si ce n'était pas le cas : `TAX_RATE` valait 0.0 depuis le
2026-07-08, et deux des trois calculs de ROI du dépôt oubliaient la retenue
même quand on la leur donnait.

Ce que ces tests gardent, et pourquoi chacun a compté :

  · UN SEUL TAUX. `core.constants.TAX_RATE` et `core.tax_engine.DEFAULT_TAX_RATE`
    ont cohabité à 0.0 et 0.20 pendant sept semaines : le taux réellement
    appliqué dépendait de quel module l'appelant avait consulté.
  · UNE SEULE FORMULE DE ROI. Elle était recopiée dans `learning_layer`,
    `weekly_report` et `calibration_report`. Deux copies sur trois calculaient
    un ROI BRUT — dont celle qui sert à monter ou baisser les seuils.
  · UN DRAWDOWN FISCALISÉ. Le disjoncteur créditait le gain BRUT : il
    sous-estimait la perte réelle, donc se déclenchait trop tard.
  · UN POINT MORT JUSTE. `p_breakeven` supposait la taxe prélevée sur le
    PAYOUT BRUT — modèle qu'aucune autre fonction n'applique. L'erreur était
    DORMANTE à taux nul (les deux formules donnent alors 1/cote) et n'est
    devenue visible qu'en rétablissant le taux réel.
"""
import pytest

from core.constants import TAX_RATE, net_b, roi_net_of_tax
from core.risk_manager import rolling_drawdown
from core.stats_utils import p_breakeven
from core.tax_engine import DEFAULT_TAX_RATE


def _ligne(outcome, odds=2.0, kelly=1.0, jour="2026-08-25"):
    return {"outcome": outcome, "odds": odds, "kelly_pct": kelly,
            "created_at": f"{jour}T12:00:00+00:00"}


class TestUnSeulTaux:
    def test_le_taux_est_celui_decide_par_l_operateur(self):
        assert TAX_RATE == 0.0, (
            "TAX_RATE est une décision opérateur (0.0, réitérée le 2026-09-01) : "
            "à 0.20, combiné au refus « Kelly nulle » de _emit, l'émission se "
            "ferme (INCIDENTS.md 2026-09-01). Ne pas la changer sans instruction "
            "explicite dans la session courante.")

    def test_tax_engine_derive_le_taux_au_lieu_de_le_redeclarer(self):
        assert DEFAULT_TAX_RATE == TAX_RATE

    def test_le_modele_de_taxe_ne_frappe_que_le_gain_net(self):
        # 2.00 → +1.00 de gain brut, dont 20 % retenus → +0.80.
        assert net_b(2.00, 0.20) == pytest.approx(0.80)
        # Une mise perdue n'est pas taxée : le modèle ne s'applique qu'au gain.
        assert net_b(2.00, 0.0) == pytest.approx(1.00)


class TestFormuleUnique:
    """`roi_net_of_tax` est LA formule ; les trois modules qui la portaient
    doivent la consommer, pas la réimplémenter."""

    def test_le_roi_net_est_sous_le_roi_brut(self):
        rows = [_ligne("WIN"), _ligne("WIN"), _ligne("LOSS")]
        assert roi_net_of_tax(rows, 0.20) < roi_net_of_tax(rows, 0.0)

    def test_un_lot_sans_gain_est_insensible_a_la_taxe(self):
        # La retenue ne frappe que les gains : deux pertes coûtent pareil.
        rows = [_ligne("LOSS"), _ligne("LOSS")]
        assert roi_net_of_tax(rows, 0.20) == roi_net_of_tax(rows, 0.0) == -1.0

    def test_la_mise_pondere_le_resultat(self):
        gros_gain = [_ligne("WIN", kelly=10.0), _ligne("LOSS", kelly=1.0)]
        petite_mise = [_ligne("WIN", kelly=1.0), _ligne("LOSS", kelly=10.0)]
        assert roi_net_of_tax(gros_gain) > roi_net_of_tax(petite_mise)

    @pytest.mark.parametrize("outcome", ["PUSH", "expired", "closed", None])
    def test_une_ligne_non_decisive_nentre_pas_au_denominateur(self, outcome):
        assert roi_net_of_tax([_ligne(outcome)]) is None

    def test_une_ligne_sans_mise_est_ecartee_pas_dotee_dune_mise_inventee(self):
        assert roi_net_of_tax([{"outcome": "WIN", "odds": 2.0, "kelly_pct": None}]) is None

    def test_rien_de_mesurable_rend_none_et_pas_zero(self):
        # 0.0 se lirait « à l'équilibre », ce qui est une affirmation.
        assert roi_net_of_tax([]) is None

    def test_learning_layer_consomme_la_formule(self):
        from core.learning_layer import _sport_stats
        rows = [_ligne("WIN", odds=1.85), _ligne("WIN", odds=1.85),
                _ligne("LOSS", odds=1.85)]
        assert _sport_stats(rows)["roi"] == pytest.approx(roi_net_of_tax(rows, TAX_RATE))

    def test_aucun_module_ne_reimplemente_la_formule(self):
        """Garde structurelle : la panne d'origine n'était pas un mauvais
        calcul, c'était TROIS calculs. Un quatrième réapparaîtrait en silence."""
        import pathlib
        import re
        racine = pathlib.Path(__file__).resolve().parent.parent
        # Signature de la formule recopiée : une somme pondérée par kelly_pct
        # qui multiplie par (odds - 1).
        motif = re.compile(r'kelly_pct"?\]?\s*\*\s*\(\s*r?\[?"?odds')
        coupables = []
        for f in list(racine.glob("core/*.py")) + list(racine.glob("scripts/*.py")) \
                + list(racine.glob("*.py")):
            if f.name == "constants.py":
                continue          # le point unique, seul autorisé
            if motif.search(f.read_text(encoding="utf-8")):
                coupables.append(str(f.relative_to(racine)))
        assert coupables == [], \
            f"ROI pondéré Kelly réimplémenté hors de core/constants.py : {coupables}"


class TestDrawdownFiscalise:
    def test_la_courbe_dequite_credite_le_gain_NET(self):
        # Une seule ligne gagnante à cote 3.00, mise 10 : +20 brut, +16 net.
        # Le drawdown se mesure sur le creux, donc on compare deux courbes.
        gagnants = [_ligne("WIN", odds=3.0, kelly=10.0, jour="2026-08-01"),
                    _ligne("LOSS", odds=3.0, kelly=10.0, jour="2026-08-02")]
        dd = rolling_drawdown(gagnants)
        # Pic après le gain net : 100 + 10·net_b(3.0) = 116 ; creux 106.
        attendu = (100 + 10 * net_b(3.0) - (100 + 10 * net_b(3.0) - 10)) / \
                  (100 + 10 * net_b(3.0))
        assert dd == pytest.approx(attendu, abs=1e-6)

    def test_un_drawdown_fiscalise_est_toujours_pire_ou_egal(self):
        """Créditer le gain brut gonfle le pic ET le fond ; sur une série qui
        gagne puis perd, le drawdown mesuré à taxe nulle sous-estime le réel."""
        rows = [_ligne("WIN", odds=2.5, kelly=5.0, jour="2026-08-01"),
                _ligne("WIN", odds=2.5, kelly=5.0, jour="2026-08-02"),
                _ligne("LOSS", odds=2.5, kelly=5.0, jour="2026-08-03"),
                _ligne("LOSS", odds=2.5, kelly=5.0, jour="2026-08-04")]
        assert rolling_drawdown(rows) > 0.0

    def test_les_pertes_ne_sont_pas_taxees(self):
        # Deux pertes : la taxe n'a aucune prise, le drawdown est le même
        # qu'il ait été calculé brut ou net.
        rows = [_ligne("LOSS", odds=2.0, kelly=10.0, jour="2026-08-01"),
                _ligne("LOSS", odds=2.0, kelly=10.0, jour="2026-08-02")]
        assert rolling_drawdown(rows) == pytest.approx(0.20, abs=1e-6)

    def test_moins_de_deux_lignes_decisives_ne_produit_pas_de_drawdown(self):
        assert rolling_drawdown([_ligne("WIN")]) == 0.0
        assert rolling_drawdown([]) == 0.0


class TestPointMortDormantJusquAuTauxReel:
    """L'erreur de `p_breakeven` n'était visible qu'à taux non nul — c'est ce
    qui l'a fait survivre. Ce test fige la raison."""

    ANCIENNE = staticmethod(lambda o, t: 1 / ((1 - t) * o))

    def test_les_deux_formules_coincident_a_taux_nul(self):
        for odds in (1.30, 1.85, 2.50, 5.00):
            assert p_breakeven(odds, 0.0) == pytest.approx(self.ANCIENNE(odds, 0.0),
                                                           abs=1e-4)

    def test_elles_divergent_des_que_la_taxe_existe(self):
        # Écart mesuré au taux réel : +15,6 pts à cote 1,30, +8,4 à 1,85,
        # +4,5 à 2,50. Toujours dans le même sens — l'ancienne EXIGE plus.
        for odds in (1.30, 1.85, 2.50):
            ecart = self.ANCIENNE(odds, 0.20) - p_breakeven(odds, 0.20)
            assert ecart > 0.04, (odds, ecart)

    def test_lecart_est_le_plus_grand_la_ou_le_moteur_parie(self):
        # Le moteur joue des favoris courts : c'est exactement là que
        # l'ancienne formule exigeait le plus d'excès.
        court = self.ANCIENNE(1.30, 0.20) - p_breakeven(1.30, 0.20)
        long_ = self.ANCIENNE(5.00, 0.20) - p_breakeven(5.00, 0.20)
        assert court > long_
