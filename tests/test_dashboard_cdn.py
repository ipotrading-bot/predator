"""
tests/test_dashboard_cdn.py — PHASE C4, puis compilation locale (2026-09-09).

`templates/system.html` chargeait quatre scripts depuis des URL FLOTTANTES :
`react@18` et `react-dom@18` suivaient tous les correctifs de la branche,
`cdn.tailwindcss.com` et `@babel/standalone` n'avaient AUCUNE version. Le
tiers décidait donc seul du code exécuté dans le navigateur de l'opérateur, à
chaque chargement, sans commit, sans revue et sans trace.

Depuis le 2026-09-09 la page ne charge PLUS AUCUN script distant : react et
react-dom sont vendorisés (api/static/js/vendor/, empreintes SRI vérifiées au
téléchargement par scripts/build_system.py), le JSX est compilé UNE fois
(api/static/js/system.js) et Tailwind est un CSS généré (system.css). Babel
standalone (2,9 Mo) et Tailwind « play » (398 Ko) ne partent plus vers le
téléphone.

Ce que ces tests gardent :

  · plus aucune URL flottante — si un script distant revenait, il porterait
    une version exacte, `integrity` ET `crossorigin` ;
  · les bundles vendorisés sont bien ceux qu'on croit (empreintes épinglées) ;
  · la page charge react, puis react-dom, puis le code compilé — et rien
    d'autre.

⚠️ Ces tests sont HORS RÉSEAU, comme toute la suite (voir tests/conftest.py).
Ils vérifient ce que le dépôt DÉCLARE et CONTIENT, jamais ce qu'un CDN sert.
"""
import base64
import hashlib
import pathlib
import re

import pytest

_RACINE = pathlib.Path(__file__).resolve().parent.parent
_GABARITS = sorted((_RACINE / "templates").glob("*.html"))
_SYSTEM = _RACINE / "templates" / "system.html"
_VENDOR = _RACINE / "api" / "static" / "js" / "vendor"

# Empreintes SRI relevées à l'épinglage (2026-08-27) sur unpkg — les mêmes
# que scripts/build_system.py vérifie avant d'écrire le fichier.
_VENDORS_SHA384 = {
    "react-18.3.1.production.min.js": "DGyLxAyjq0f9SPpVevD6IgztCFlnMF6oW/XQGmfe+IsZ8TqEiDrcHkMLKI6fiB/Z",
    "react-dom-18.3.1.production.min.js": "gTGxhz21lVGYNMcdJOyq01Edg0jhn/c22nsx0kyqP0TxaV5WVdsSH1fSDUf5YJj1",
}

_BALISE_SCRIPT = re.compile(r"<script\b[^>]*\bsrc=\"([^\"]+)\"[^>]*>", re.S)
_COMMENTAIRE = re.compile(r"<!--.*?-->", re.S)


def _sans_commentaires(gabarit: pathlib.Path) -> str:
    """Le gabarit privé de ses commentaires HTML.

    Indispensable : les commentaires de ce dépôt CITENT le code retiré pour
    expliquer pourquoi il l'est — Babel et Tailwind figurent dans la note
    historique. Un commentaire n'est pas exécuté ; l'analyser ferait échouer
    les gardes sur la documentation elle-même, et pousserait à l'effacer.
    """
    return _COMMENTAIRE.sub("", gabarit.read_text(encoding="utf-8"))


def _scripts(gabarit: pathlib.Path):
    """(url, balise complète) pour chaque <script src=…> RÉELLEMENT chargé."""
    texte = _sans_commentaires(gabarit)
    return [(m.group(1), m.group(0)) for m in _BALISE_SCRIPT.finditer(texte)]


def _distants(gabarit: pathlib.Path):
    return [(u, b) for u, b in _scripts(gabarit) if u.startswith(("http://", "https://", "//"))]


class TestPlusAucuneURLFlottante:
    @pytest.mark.parametrize("gabarit", _GABARITS, ids=lambda g: g.name)
    def test_chaque_script_distant_porte_une_version_exacte(self, gabarit):
        """« Exacte » veut dire : trois nombres. `react@18` en a un seul et
        suit donc tous les correctifs ; c'est précisément ce qu'on ferme."""
        for url, _balise in _distants(gabarit):
            assert re.search(r"@\d+\.\d+\.\d+(?:[-+][\w.]+)?(?:/|$)", url), \
                f"{gabarit.name} : version non épinglée → {url}"

    @pytest.mark.parametrize("gabarit", _GABARITS, ids=lambda g: g.name)
    def test_aucun_cdn_de_transpilation_nest_appele(self, gabarit):
        """Tailwind play ne peut PAS être protégé par SRI (aucun en-tête CORS,
        mesuré le 2026-08-27) et Babel standalone pèse 2,9 Mo : ni l'un ni
        l'autre n'a sa place dans un téléphone."""
        urls = [u for u, _ in _scripts(gabarit)]
        for interdit in ("cdn.tailwindcss.com", "@babel/standalone", "tailwind-3"):
            assert not any(interdit in u for u in urls), \
                f"{gabarit.name} charge {interdit} — la page se recompile dans le navigateur"


class TestToutTiersEstVerifie:
    @pytest.mark.parametrize("gabarit", _GABARITS, ids=lambda g: g.name)
    def test_chaque_script_distant_porte_integrity_et_crossorigin(self, gabarit):
        """Les deux, ou aucun des deux ne sert. Sans `crossorigin`, le
        navigateur ne peut pas lire la réponse d'un tiers pour la hacher :
        `integrity` seul est décoratif."""
        for url, balise in _distants(gabarit):
            assert "integrity=" in balise, f"{gabarit.name} : SRI absent → {url}"
            assert "crossorigin" in balise, \
                f"{gabarit.name} : crossorigin absent, l'integrity ne vérifiera rien → {url}"

    @pytest.mark.parametrize("gabarit", _GABARITS, ids=lambda g: g.name)
    def test_les_empreintes_sont_du_sha384_base64(self, gabarit):
        for _url, balise in _distants(gabarit):
            m = re.search(r'integrity="([^"]+)"', balise)
            assert m, balise[:120]
            for empreinte in m.group(1).split():
                algo, _, valeur = empreinte.partition("-")
                assert algo in ("sha256", "sha384", "sha512"), empreinte
                assert len(valeur) >= 40, f"empreinte trop courte : {empreinte}"


class TestReactEstServiParLeDepot:
    @pytest.mark.parametrize("nom", sorted(_VENDORS_SHA384), ids=str)
    def test_le_bundle_vendorise_est_bien_celui_quon_croit(self, nom):
        """Sans cette vérification, « plus aucun tiers » ne veut rien dire :
        un fichier vendorisé remplacé en silence est exactement le risque que
        SRI ferme sur un CDN."""
        p = _VENDOR / nom
        assert p.is_file(), f"{nom} a disparu — la page /system serait blanche"
        empreinte = base64.b64encode(hashlib.sha384(p.read_bytes()).digest()).decode()
        assert empreinte == _VENDORS_SHA384[nom], nom

    @pytest.mark.parametrize("nom", sorted(_VENDORS_SHA384), ids=str)
    def test_le_bundle_najoute_aucun_appel_reseau(self, nom):
        texte = (_VENDOR / nom).read_text(encoding="utf-8", errors="replace")
        for motif in ("fetch(\"http", "fetch('http", "importScripts("):
            assert motif not in texte, f"{nom} tente un accès réseau : {motif}"


class TestLaPageResteChargeable:
    """Le pire résultat serait une page qui ne charge plus : react, puis
    react-dom, puis le code compilé, tous depuis notre origine."""

    def test_les_trois_scripts_et_rien_dautre(self):
        urls = [u.split("?")[0] for u, _ in _scripts(_SYSTEM)]
        assert urls == ["/static/js/vendor/react-18.3.1.production.min.js",
                        "/static/js/vendor/react-dom-18.3.1.production.min.js",
                        "/static/js/system.js"], urls
        assert not _distants(_SYSTEM)

    def test_le_css_compile_est_lie(self):
        texte = _sans_commentaires(_SYSTEM)
        assert '/static/css/system.css?v={{ version }}' in texte
        assert "tailwind.config" not in texte and 'type="text/babel"' not in texte
