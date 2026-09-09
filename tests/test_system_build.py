"""Gardien de la compilation de /system (2026-09-09).

`assets/system.jsx` est la SOURCE ; `api/static/js/system.js` et
`api/static/css/system.css` sont GÉNÉRÉS par `python scripts/build_system.py`
et portent en tête l'empreinte SHA-256 de la source au moment de la
compilation. Une source modifiée sans recompilation servirait l'ANCIENNE
page en production pendant que les tests lisent la nouvelle — c'est ce que
ce test refuse. Hors réseau : il ne compile rien, il compare.
"""
import hashlib
import re
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
SRC = RACINE / "assets" / "system.jsx"
JS = RACINE / "api" / "static" / "js" / "system.js"
CSS = RACINE / "api" / "static" / "css" / "system.css"


def _empreinte_declaree(p: Path) -> str:
    tete = p.read_text(encoding="utf-8", errors="replace")[:400]
    m = re.search(r"source-sha256: ([0-9a-f]{64})", tete)
    assert m, f"{p.name} ne porte pas l'empreinte de sa source — pas généré par scripts/build_system.py ?"
    return m.group(1)


def test_le_js_et_le_css_compiles_sont_a_jour():
    attendu = hashlib.sha256(SRC.read_bytes()).hexdigest()
    for p in (JS, CSS):
        assert p.is_file(), p
        assert _empreinte_declaree(p) == attendu, (
            f"{p.name} a été compilé depuis une autre version de assets/system.jsx "
            "— relancer `python scripts/build_system.py`")


def test_le_js_compile_ne_contient_plus_de_jsx():
    js = JS.read_text(encoding="utf-8")
    assert "React.createElement(" in js
    assert "<Panel" not in js and "</div>" not in js
    assert 'getElementById("sbc-root")' in js


def test_le_css_compile_couvre_les_classes_de_la_source():
    """Les classes Tailwind écrites en clair dans la source doivent exister
    dans le CSS généré : une classe ajoutée sans recompiler ne s'applique pas."""
    # Les classes de la charte (predator.css) et celles du <style> du gabarit
    # ne viennent pas de Tailwind : elles sont cherchées là où elles vivent.
    css = CSS.read_text(encoding="utf-8")
    maison = ((RACINE / "api" / "static" / "css" / "predator.css").read_text(encoding="utf-8")
              + (RACINE / "templates" / "system.html").read_text(encoding="utf-8"))
    src = SRC.read_text(encoding="utf-8")
    classes = set()
    for m in re.finditer(r'className="([^"]+)"', src):
        classes.update(m.group(1).split())

    def definie(c: str, feuille: str) -> bool:
        return re.search(r"\." + re.escape(c) + r"(?=[\s{,:>.\[])", feuille) is not None

    manquantes = [c for c in sorted(classes)
                  if re.fullmatch(r"[a-z0-9\-]+", c) and not definie(c, css) and not definie(c, maison)]
    assert not manquantes, manquantes
