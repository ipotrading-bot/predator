"""
tests/test_system_page.py — la page /system est du JSX transpilé DANS le
navigateur (Babel standalone, ce dépôt n'a pas d'étape de build).

POURQUOI CE TEST EXISTE (panne du 2026-08-22) : la suppression du widget
« Quota OddsAPI » (Mission 2, Phase 2) a laissé un `</div>` orphelin. Côté
Flask, tout allait bien — le template rend, la route répond 200, le
smoke-test de routes passe. Mais Babel refusait de transpiler
(« Adjacent JSX elements must be wrapped in an enclosing tag »), donc
`ReactDOM.createRoot(...).render(...)` n'était jamais atteint et
`<div id="sbc-root">` restait VIDE : page entièrement blanche en
production, pendant que tous les tests étaient au vert.

Une erreur de syntaxe JSX est donc invisible côté serveur. Ce test la rend
visible : il compte les balises `<div>` du bloc JSX. C'est exactement la
classe de bug qui a cassé la page (on supprime un widget, on oublie une
balise fermante), et le comptage est sans faux positif tant que la page
n'utilise pas de `<div … />` auto-fermant (vérifié ici aussi).

Il garde par ailleurs les deux invariants sans lesquels la page est morte
avant même d'être parsée : le bloc doit être encadré par `{% raw %}` /
`{% endraw %}` (sinon Jinja avale les accolades du JSX) et le point
d'entrée `ReactDOM.createRoot` doit toujours viser `sbc-root`.
"""
import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "system.html"


@pytest.fixture(scope="module")
def html() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def jsx(html: str) -> str:
    """Le seul bloc que Babel transpile, isolé de tout le HTML statique."""
    start = html.index("{% raw %}") + len("{% raw %}")
    return html[start:html.index("{% endraw %}")]


class TestJinjaEnvelope:
    def test_le_jsx_est_protege_par_raw(self, html):
        # Sans {% raw %}, Jinja interprète `{ background: … }` comme une
        # expression et le script part en erreur de rendu côté serveur.
        assert html.count("{% raw %}") == 1
        assert html.count("{% endraw %}") == 1
        assert html.index("{% raw %}") < html.index("{% endraw %}")

    def test_le_conteneur_react_existe(self, html, jsx):
        assert 'id="sbc-root"' in html
        assert 'getElementById("sbc-root")' in jsx


class TestBalisesEquilibrees:
    def test_pas_de_div_auto_fermant(self, jsx):
        # Le comptage ci-dessous suppose qu'un `<div` ouvre toujours un bloc.
        assert not re.search(r"<div\b[^>]*/>", jsx)

    def test_autant_de_div_ouverts_que_fermes(self, jsx):
        ouverts = len(re.findall(r"<div\b", jsx))
        fermes  = len(re.findall(r"</div>", jsx))
        assert ouverts == fermes, (
            f"{ouverts} <div> pour {fermes} </div> — Babel refusera de "
            "transpiler et #sbc-root restera vide (page blanche)."
        )

    @pytest.mark.parametrize("tag", ["Panel", "span", "button", "select", "svg"])
    def test_composants_et_balises_multi_lignes_equilibres(self, jsx, tag):
        ouverts = len(re.findall(rf"<{tag}\b(?![^>]*/>)", jsx))
        fermes  = len(re.findall(rf"</{tag}>", jsx))
        assert ouverts == fermes, f"<{tag}> : {ouverts} ouvert(s), {fermes} fermé(s)"


class TestScenariosDerivesDuMoteur:
    """Chantier « scénarios » (2026-09-03) : les retours conditionnels par
    nombre de gagnants se calculent avec kCombinations/comboReturnFactor, à
    côté du bloc MATH, JAMAIS dedans — « moteur inchangé » est une règle
    d'opérateur, ce test la rend mécanique."""

    MATH_FUNCTIONS = ("kCombinations", "legBranches", "comboReturnFactor",
                      "comboMaxFactor", "bucketOf")

    def _bloc_math(self, jsx: str) -> str:
        debut = jsx.index("MATH — combinatoire")
        return jsx[debut:jsx.index("SCÉNARIOS — dérivés du moteur")]

    def test_le_bloc_math_contient_exactement_ses_cinq_fonctions(self, jsx):
        bloc = self._bloc_math(jsx)
        assert "moteur inchangé" in bloc
        assert re.findall(r"^function (\w+)\(", bloc, re.M) == list(self.MATH_FUNCTIONS)

    def test_les_fonctions_de_scenarios_existent_apres_le_bloc_math(self, jsx):
        # `systemReturn`, `scenariosByWinners`, `breakEven` et `compoundMargin`
        # sont parties le 2026-09-04 avec le panneau « Scénarios » du système
        # seul, que `scenariosComplets` couvre entièrement. Elles ne sont pas
        # devenues du code mort : elles ont été retirées.
        fin_math = jsx.index("SCÉNARIOS — dérivés du moteur")
        for fn in ("taxOfBet", "scenariosComplets", "breakEvenComplet"):
            pos = jsx.index(f"function {fn}(")
            assert pos > fin_math, f"{fn} doit vivre HORS du bloc MATH"
        for parti in ("systemReturn", "scenariosByWinners", "compoundMargin"):
            assert f"function {parti}(" not in jsx, f"{parti} devait partir avec son panneau"

    def test_les_scenarios_passent_par_le_moteur(self, jsx):
        debut = jsx.index("SCÉNARIOS — dérivés du moteur")
        bloc = jsx[debut:jsx.index("UI ATOMS")]
        assert "kCombinations(" in bloc and "comboReturnFactor(" in bloc
        # Des retours CONDITIONNELS, jamais des probabilités (règle 7) : le
        # bloc le dit en toutes lettres.
        assert "jamais des probabilités" in bloc

    def test_le_panneau_scenarios_du_systeme_seul_a_ete_retire(self, jsx):
        """2026-09-04 : « Scénarios » (système seul) est retiré — `Scénarios
        complets` couvre les mêmes k, exactement, toutes sections confondues.
        Ce qui SURVIT est ce qui comptait : le libellé règle 7 et le point
        mort, portés désormais par le panneau complet."""
        assert 'title="Scénarios"' not in jsx, "le panneau système seul devait partir"
        assert "Marge par jambe (%)" not in jsx and "compoundMargin(" not in jsx
        assert 'title="Scénarios complets"' in jsx
        assert "pas des probabilités" in jsx      # règle 7, toujours affichée
        assert "Point mort" in jsx
        assert "scenariosComplets(" in jsx and "breakEvenComplet(scenariosTotal)" in jsx

    @pytest.mark.parametrize("tag", ["table", "thead", "tbody", "tr", "th", "td", "p", "label", "sup"])
    def test_les_balises_du_panneau_sont_equilibrees(self, jsx, tag):
        ouverts = len(re.findall(rf"<{tag}\b(?![^>]*/>)", jsx))
        fermes = len(re.findall(rf"</{tag}>", jsx))
        assert ouverts == fermes, f"<{tag}> : {ouverts} ouvert(s), {fermes} fermé(s)"


class TestSectionsAdditionnees:
    """Chantier « sections additionnées » (2026-09-04) : simples, combiné et
    système ont chacun leur mise et leur bilan brut/net, s'additionnent dans
    un total, et les scénarios COMPLETS (toutes sections) sont exacts. Même
    règle que les scénarios système : dérivé du moteur, jamais dedans, jamais
    des probabilités."""

    def test_les_scenarios_complets_vivent_hors_du_bloc_math_et_passent_par_le_moteur(self, jsx):
        fin_math = jsx.index("SCÉNARIOS — dérivés du moteur")
        debut = jsx.index("function scenariosComplets(")
        assert debut > fin_math
        corps = jsx[debut:jsx.index("function breakEvenComplet(")]
        # Une combinaison du système est évaluée par le moteur, puis comptée
        # dans un scénario si toutes ses jambes y sont gagnantes (masque).
        assert "kCombinations(" in corps and "comboReturnFactor(" in corps
        assert "(c.mask & mask) === c.mask" in corps
        # Interdit : rappeler systemReturn par scénario (2^N × 2^N, page gelée à N = 10).
        assert "systemReturn(" not in corps

    def test_les_quatre_sections_et_le_mode_de_mise_sont_rendus(self, jsx):
        for titre in ("Système", "Mises individuelles", "Combiné", "Total", "Scénarios complets"):
            assert f'title="{titre}"' in jsx, titre
        assert 'label: "Mise par ligne"' in jsx and 'label: "Mise totale"' in jsx
        assert "switchStakeMode" in jsx and "totalStakeWanted / totalLignes" in jsx
        # Chaque section entre ou sort du total sans perdre ses saisies.
        assert jsx.count('label="Dans le total"') == 3
        assert "scenariosComplets(" in jsx and "breakEvenComplet(scenariosTotal)" in jsx

    def test_le_doublon_systeme_simples_combine_est_signale(self, jsx):
        # Un système M/N contient le combiné N/N, et à M = 1 les N simples :
        # additionner les deux est jouer deux fois — la page le dit.
        assert "contient déjà" in jsx and "jouer deux fois" in jsx

    def test_les_tables_tiennent_dans_un_telephone(self, html):
        # predator.css impose min-width: 760px aux tables (grilles de signaux) ;
        # sans cet override, les tableaux de /system débordent sur mobile.
        assert ":where(#sbc-root) table { min-width: 0; }" in html


class TestFiscaliteAppliqueePartout:
    """Chantier « impôt partout » (2026-09-04, instruction opérateur explicite
    — règle dure n°11). Brut = AVANT impôt, net = APRÈS. La retenue porte sur
    le gain net de chaque pari GAGNANT (modèle `core/constants.net_b`), et le
    taux vient d'un champ, jamais d'une constante enterrée dans le template."""

    def test_le_taux_vient_dun_champ_operateur_jamais_dune_constante(self, jsx):
        assert "const [taxRateInput, setTaxRateInput] = useState(" in jsx
        assert "parseFloat(taxRateInput)" in jsx
        assert "Taux d'imposition (%)" in jsx
        # L'ancien 20 % en dur, et l'assiette codée en dur, ont disparu.
        assert "const taxRate = 20;" not in jsx
        assert "taxBase" not in jsx
        assert "taxEnabled" not in jsx

    def test_un_seul_modele_fiscal_le_gain_net_dun_pari_gagnant(self, jsx):
        debut = jsx.index("function taxOfBet(")
        corps = jsx[debut:jsx.index("// Scénarios COMPLETS")]
        # Un pari perdu (R < 1) ou remboursé (R = 1) n'est jamais taxé : c'est
        # le max(0, …) qui le garantit, pas une condition posée ailleurs.
        assert "Math.max(0, R * stake - stake) * rate" in corps
        # POINT UNIQUE : système, simples, combiné et scénarios passent par lui.
        assert jsx.count("taxOfBet(") >= 6

    def test_chaque_section_affiche_brut_impot_net(self, jsx):
        # Système, simples, combiné, Total, Système — résumé.
        assert jsx.count("Impôt (${taxRate} %)") >= 5
        assert jsx.count("(avant impôt)") >= 3 and jsx.count("(après impôt)") >= 3
        assert "Net = brut − mise − impôt" in jsx

    def test_les_scenarios_et_le_point_mort_sont_juges_apres_impot(self, jsx):
        # Le pire/meilleur cas se choisit sur le NET, et le point mort aussi :
        # l'impôt frappant chaque pari gagnant, il peut DÉCALER le point mort.
        assert "r.net < worst.net" in jsx and "r.net > best.net" in jsx
        assert "sc.worst.net >= -1e-9" in jsx
        assert "sc.worst.net > 1e-9" in jsx

    def test_lindependance_avec_le_moteur_est_ecrite_noir_sur_blanc(self, html):
        """La page a son taux, `core.constants.TAX_RATE` a le sien (0.0, décision
        opérateur qui pilote l'ÉMISSION via le b de Kelly). Les deux ont cohabité
        en silence à 20 % et 0 % — ce test exige que l'écart reste EXPLIQUÉ dans
        le template, pour qu'il ne se redécouvre pas comme un bug.
        Il ne fige AUCUNE valeur : le taux du moteur reste une décision opérateur."""
        assert "core.constants.TAX_RATE" in html
        assert "l'ÉMISSION" in html
        import core.constants as cc
        assert isinstance(cc.TAX_RATE, float)   # la valeur ne regarde pas ce test
