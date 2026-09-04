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
        fin_math = jsx.index("SCÉNARIOS — dérivés du moteur")
        for fn in ("systemReturn", "scenariosByWinners", "breakEven", "compoundMargin"):
            pos = jsx.index(f"function {fn}(")
            assert pos > fin_math, f"{fn} doit vivre HORS du bloc MATH"

    def test_les_scenarios_passent_par_le_moteur(self, jsx):
        debut = jsx.index("SCÉNARIOS — dérivés du moteur")
        bloc = jsx[debut:jsx.index("UI ATOMS")]
        assert "kCombinations(" in bloc and "comboReturnFactor(" in bloc
        # Des retours CONDITIONNELS, jamais des probabilités (règle 7) : le
        # bloc le dit en toutes lettres.
        assert "jamais des probabilités" in bloc

    def test_le_panneau_scenarios_et_le_champ_marge_sont_rendus(self, jsx):
        """Commit 2 : le panneau existe, il porte le libellé « pas des
        probabilités » (règle 7), le point mort et le champ de marge."""
        assert 'title="Scénarios"' in jsx
        assert "retours conditionnels — pas des probabilités" in jsx
        assert "Point mort" in jsx and "Cotes trop courtes pour un système" in jsx
        assert "Marge par jambe (%)" in jsx and "compoundMargin(" in jsx
        assert "N ≥ 5" in jsx
        # Les scénarios de l'UI viennent des fonctions pures, pas d'un recalcul
        assert "scenariosByWinners(selections" in jsx and "breakEven(scenarios)" in jsx

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
