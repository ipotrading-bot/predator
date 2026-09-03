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
