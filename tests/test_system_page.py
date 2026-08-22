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
