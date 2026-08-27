"""
tests/test_sans_scipy.py — PHASE D1.

scipy pesait ~35 Mo dans chaque job CI et dans le bundle Vercel pour TROIS
appels, tous remplaçables :

  · `optimize.minimize_scalar` cherchait numériquement le maximum d'une
    fonction strictement concave dont la racine de la dérivée s'écrit en une
    ligne — c'est le critère de Kelly, `f* = p − (1−p)/b` ;
  · `stats.norm.ppf` existe dans la bibliothèque standard depuis Python 3.8
    (`statistics.NormalDist().inv_cdf`) ;
  · `stats.multivariate_normal(...).cdf` sur deux dimensions se ramène à
    l'identité de Plackett et une quadrature élémentaire.

Ce que ces tests gardent :

  · plus AUCUN module du dépôt n'importe scipy, et le fichier de dépendances
    ne le déclare plus — les deux, sinon la dépendance revient par pip ou
    par un import oublié ;
  · les formes closes sont justes, éprouvées contre des identités
    ANALYTIQUES indépendantes (pas contre scipy, qui n'est plus là pour
    servir de référence, et qui était d'ailleurs le moins exact des deux).

⚠️ La comparaison à scipy a bien eu lieu, mais AVANT le retrait, et son
résultat est consigné dans les docstrings de `core/tax_engine` :
80 529 points pour Kelly (écart max 3·10⁻⁶, imputable à la tolérance du
SOLVEUR) et 15 210 points pour Φ₂ (écart max 1,5·10⁻⁹). Un test qui
réimporterait scipy pour rejouer cette comparaison réintroduirait exactement
la dépendance qu'on retire.
"""
import ast
import math
import pathlib
import statistics

import pytest

from core.constants import net_b
from core.tax_engine import _phi, _phi2, optimal_stake_fraction

_RACINE = pathlib.Path(__file__).resolve().parent.parent


def _modules_du_depot():
    """Tous les .py suivis par git, sauf ce fichier — qui NOMME scipy."""
    import subprocess
    sortie = subprocess.run(["git", "ls-files", "*.py"], cwd=_RACINE,
                            capture_output=True, text=True, check=True).stdout
    return [_RACINE / ligne for ligne in sortie.split()
            if ligne and not ligne.endswith("test_sans_scipy.py")]


class TestLaDependanceEstReellementPartie:
    def test_aucun_module_nimporte_scipy(self):
        """Sur l'AST, pas sur le texte : un commentaire a le droit d'expliquer
        POURQUOI scipy a été retiré — c'est même l'intérêt des docstrings de
        `tax_engine`. Même parti pris que pour les modèles IA morts nommés en
        commentaire (tests/test_ai_router.py)."""
        coupables = []
        for chemin in _modules_du_depot():
            arbre = ast.parse(chemin.read_text(encoding="utf-8"), str(chemin))
            for noeud in ast.walk(arbre):
                if isinstance(noeud, ast.Import):
                    noms = [a.name for a in noeud.names]
                elif isinstance(noeud, ast.ImportFrom):
                    noms = [noeud.module or ""]
                else:
                    continue
                if any(n == "scipy" or n.startswith("scipy.") for n in noms):
                    coupables.append(f"{chemin.relative_to(_RACINE)}:{noeud.lineno}")
        assert not coupables, "scipy est de retour : " + ", ".join(coupables)

    def test_le_fichier_de_dependances_ne_le_declare_plus(self):
        """Sans ce test, `pip install -r requirements.txt` continuerait de le
        télécharger dans chaque job même si plus une ligne ne l'importe."""
        texte = (_RACINE / "requirements.txt").read_text(encoding="utf-8")
        assert "scipy" not in texte

    def test_le_module_qui_lutilisait_simporte_toujours(self):
        """Témoin : sans lui, les deux tests ci-dessus passeraient aussi bien
        si `tax_engine` avait été supprimé."""
        from core import tax_engine
        assert callable(tax_engine.optimal_stake_fraction)


class TestKellyEnFormeClose:
    """`f* = p − (1−p)/b` où b = net_b(cote, taux).

    Éprouvé contre la CONDITION D'OPTIMALITÉ elle-même — la dérivée de la
    croissance logarithmique s'annule au point rendu — plutôt que contre une
    table de valeurs recopiée, qu'il faudrait croire sur parole."""

    @staticmethod
    def _derivee_croissance_log(f, p, b):
        """d/df [ p·ln(1+f·b) + (1−p)·ln(1−f) ]."""
        return p * b / (1 + f * b) - (1 - p) / (1 - f)

    @pytest.mark.parametrize("p,cote,taux", [
        (0.60, 2.10, 0.20), (0.55, 2.00, 0.20), (0.75, 1.50, 0.20),
        (0.60, 2.10, 0.00), (0.90, 1.30, 0.35), (0.52, 3.00, 0.20),
    ])
    def test_la_derivee_sannule_au_point_rendu(self, p, cote, taux):
        f = optimal_stake_fraction(p, cote, tax_rate=taux, kelly_multiplier=1.0)
        if f == 0.0:
            return                                  # cas à edge négatif, traité plus bas
        assert self._derivee_croissance_log(f, p, net_b(cote, taux)) == pytest.approx(0, abs=1e-5)

    def test_cest_bien_un_MAXIMUM_pas_un_point_quelconque(self):
        """La dérivée s'annule aussi à un minimum. Ici g est strictement
        concave, donc de part et d'autre la croissance doit être MOINDRE."""
        p, cote, taux = 0.60, 2.10, 0.20
        b = net_b(cote, taux)
        f = optimal_stake_fraction(p, cote, tax_rate=taux, kelly_multiplier=1.0)

        def g(x):
            return p * math.log(1 + x * b) + (1 - p) * math.log(1 - x)

        assert g(f) > g(f - 0.01) and g(f) > g(f + 0.01)

    def test_un_edge_negatif_rend_zero_et_non_une_poussiere(self):
        """Le cas qui comptait vraiment : le solveur borné s'arrêtait PRÈS de
        la borne f=0 sans l'atteindre, et laissait fuir une mise minuscule.
        La forme close rend un f* franchement négatif, ramené à zéro."""
        assert optimal_stake_fraction(0.30, 1.50, tax_rate=0.20) == 0.0
        assert optimal_stake_fraction(0.50, 1.90, tax_rate=0.20) == 0.0

    def test_la_taxe_reduit_toujours_la_mise(self):
        sans = optimal_stake_fraction(0.60, 2.10, tax_rate=0.0)
        avec = optimal_stake_fraction(0.60, 2.10, tax_rate=0.20)
        assert 0 < avec < sans

    def test_le_kelly_fractionnaire_est_proportionnel(self):
        plein = optimal_stake_fraction(0.60, 2.10, tax_rate=0.20, kelly_multiplier=1.0)
        demi = optimal_stake_fraction(0.60, 2.10, tax_rate=0.20, kelly_multiplier=0.5)
        assert demi == pytest.approx(plein / 2, abs=1e-6)

    def test_la_mise_est_plafonnee_a_0999(self):
        """Une quasi-certitude à grosse cote donnerait f* > 1. Miser tout le
        bankroll sur un pari non certain ruine à la première perte."""
        assert optimal_stake_fraction(0.999, 50.0, tax_rate=0.0,
                                      kelly_multiplier=1.0) <= 0.999

    def test_les_entrees_absurdes_rendent_zero_sans_lever(self):
        for p, cote in ((0.0, 2.0), (1.0, 2.0), (-0.5, 2.0), (0.6, 1.0), (0.6, 0.5)):
            assert optimal_stake_fraction(p, cote, tax_rate=0.20) == 0.0


class TestLoiNormaleSansScipy:
    def test_phi_aux_points_connus(self):
        assert _phi(0.0) == pytest.approx(0.5, abs=1e-15)
        assert _phi(1.959963985) == pytest.approx(0.975, abs=1e-9)
        assert _phi(-1.0) + _phi(1.0) == pytest.approx(1.0, abs=1e-15)

    def test_inv_cdf_de_la_bibliotheque_standard_inverse_bien_phi(self):
        for q in (0.01, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99):
            assert _phi(statistics.NormalDist().inv_cdf(q)) == pytest.approx(q, abs=1e-12)

    @pytest.mark.parametrize("rho", [-0.999, -0.99, -0.9, -0.3, -0.15,
                                     0.15, 0.3, 0.9, 0.99, 0.999])
    def test_phi2_contre_le_theoreme_de_sheppard(self, rho):
        """Référence ANALYTIQUE indépendante, et non une valeur recopiée :
        Φ₂(0, 0, ρ) = ¼ + arcsin(ρ)/2π. C'est le seul point où la bivariée a
        une forme close élémentaire, et il tombe pile là où la quadrature doit
        être vérifiée puisque l'intégration part de ρ = 0.

        ⚠️ LA TOLÉRANCE EST À LA PRÉCISION MACHINE, ET C'EST VOLONTAIRE. En
        h = k = 0 l'intégrande de la forme en sinus vaut 1/2π, CONSTANT, et
        Simpson intègre une constante exactement — il n'y a donc rien à
        tolérer. L'intégration en r que ce module employait d'abord rendait
        5,1·10⁻⁶ à ρ = 0,99 : un seuil lâche la laisserait revenir sans que
        rien ne tombe. Les |ρ| extrêmes sont dans la liste pour la même
        raison, alors même que l'usage réel reste dans [0,15 ; 0,30]."""
        assert _phi2(0.0, 0.0, rho) == pytest.approx(0.25 + math.asin(rho) / (2 * math.pi),
                                                     abs=1e-14)

    @pytest.mark.parametrize("rho", [-0.99, -0.9, -0.2, 0.2, 0.9, 0.99])
    def test_les_bornes_degenerees_ne_levent_pas(self, rho):
        """ρ = ±1 sort du domaine de l'intégrale (arcsin ±1 = ±π/2, cos = 0).
        Les deux cas limites ont une forme close — comonotone et
        contre-monotone — plutôt qu'une division par zéro."""
        assert _phi2(0.5, -0.5, 1.0) == pytest.approx(min(_phi(0.5), _phi(-0.5)))
        assert _phi2(0.5, -0.5, -1.0) == pytest.approx(max(0.0, _phi(0.5) + _phi(-0.5) - 1))
        assert 0.0 <= _phi2(0.5, -0.5, rho) <= 1.0

    def test_rho_nul_redonne_EXACTEMENT_lindependance(self):
        """Le mode "discount" divise par le produit indépendant : si ρ=0 ne
        redonnait pas exactement ce produit, un rho nul escompterait quand
        même, en silence."""
        for h, k in ((0.0, 0.0), (1.0, -0.5), (-2.0, 2.0)):
            assert _phi2(h, k, 0.0) == _phi(h) * _phi(k)

    def test_phi2_est_symetrique_en_ses_deux_marges(self):
        assert _phi2(0.7, -1.3, 0.25) == pytest.approx(_phi2(-1.3, 0.7, 0.25), abs=1e-12)

    def test_une_correlation_positive_augmente_la_probabilite_jointe(self):
        """Dépendance de quadrant positive : c'est le fait qui oblige
        `_pairwise_gaussian_copula_joint` à passer −rho pour ESCOMPTER.
        Si cette propriété se perdait, un combiné corrélé paraîtrait plus
        viable qu'un combiné indépendant — l'inverse du but."""
        independant = _phi(0.5) * _phi(0.5)
        assert _phi2(0.5, 0.5, 0.30) > independant
        assert _phi2(0.5, 0.5, -0.30) < independant

    def test_phi2_reste_une_probabilite_jusque_dans_les_queues(self):
        """C'est là que ça cassait : en h = k = −2 et ρ = −0,9, l'annulation
        entre Φ(h)·Φ(k) et l'intégrale rendait −2,7·10⁻¹⁴. Un négatif ferait
        DIVERGER le rapport `correlated_joint / independent_joint` du mode
        "discount", donc la probabilité du combiné."""
        for h in (-4.0, -3.0, -2.0, 0.0, 2.0, 3.0, 4.0):
            for k in (-4.0, -3.0, -2.0, 0.0, 2.0, 3.0, 4.0):
                for rho in (-0.99, -0.9, -0.2, 0.2, 0.9, 0.99):
                    assert 0.0 <= _phi2(h, k, rho) <= 1.0, (h, k, rho)
