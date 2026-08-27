"""
tests/test_dashboard_cdn.py — PHASE C4.

`templates/system.html` chargeait quatre scripts depuis des URL FLOTTANTES :
`react@18` et `react-dom@18` suivaient tous les correctifs de la branche,
`cdn.tailwindcss.com` et `@babel/standalone` n'avaient AUCUNE version. Le
tiers décidait donc seul du code exécuté dans le navigateur de l'opérateur, à
chaque chargement, sans commit, sans revue et sans trace.

Ce n'est pas une hypothèse : `@babel/standalone` sans version servait du 7.x
quand la page a été écrite, il rend du 8.0.4 aujourd'hui. La page a changé de
MAJEURE de Babel sans qu'aucune ligne du dépôt ne le mentionne.

Ce que ces tests gardent :

  · plus aucune URL flottante — chaque source distante porte une version exacte ;
  · toute source DISTANTE porte `integrity` ET `crossorigin` (l'un sans
    l'autre ne vérifie rien) ;
  · Tailwind est servi depuis le dépôt, et le fichier vendorisé est bien celui
    qu'on croit — sinon « pas de tiers » ne veut plus rien dire ;
  · aucun script n'est ajouté sans l'une de ces deux protections.

⚠️ Ces tests sont HORS RÉSEAU, comme toute la suite (voir tests/conftest.py).
Ils vérifient ce que le dépôt DÉCLARE, pas ce que le CDN sert aujourd'hui :
comparer une empreinte à la réalité demanderait un appel réseau, et une suite
qui dépend d'un tiers échoue le jour où ce tiers tousse. La correspondance
empreinte ↔ octets a été établie à l'épinglage, et c'est le navigateur qui la
revérifie à chaque chargement — c'est exactement le travail de SRI.
"""
import hashlib
import pathlib
import re

import pytest

_RACINE = pathlib.Path(__file__).resolve().parent.parent
_GABARITS = sorted((_RACINE / "templates").glob("*.html"))

# Empreinte du bundle Tailwind vendorisé, relevée à l'épinglage le 2026-08-27
# et identique à celle servie alors par cdn.tailwindcss.com/3.4.17 — c'est le
# même octet, servi d'ailleurs.
_TAILWIND = _RACINE / "api" / "static" / "js" / "tailwind-3.4.17.min.js"
_TAILWIND_SHA384 = "igm5BeiBt36UU4gqwWS7imYmelpTsZlQ45FZf+XBn9MuJbn4nQr7yx1yFydocC/K"

_BALISE_SCRIPT = re.compile(r"<script\b[^>]*\bsrc=\"([^\"]+)\"[^>]*>", re.S)
_COMMENTAIRE = re.compile(r"<!--.*?-->", re.S)


def _sans_commentaires(gabarit: pathlib.Path) -> str:
    """Le gabarit privé de ses commentaires HTML.

    Indispensable : les commentaires de ce dépôt CITENT le code retiré pour
    expliquer pourquoi il l'est — la balise `cdn.tailwindcss.com` figure dans
    la note de retour arrière. Un commentaire n'est pas exécuté ; l'analyser
    ferait échouer les gardes sur la documentation elle-même, et pousserait à
    l'effacer. Même règle que pour les modèles IA morts nommés en commentaire
    (tests/test_ai_router.py) et pour `replace_signal_row` (tests/test_db.py).
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
    def test_le_cdn_tailwind_nest_plus_appele(self, gabarit):
        """Ce CDN ne peut PAS être protégé par SRI (aucun en-tête CORS,
        mesuré le 2026-08-27) : la seule fermeture est de ne pas l'appeler."""
        urls = [u for u, _ in _scripts(gabarit)]
        assert not any("cdn.tailwindcss.com" in u for u in urls), \
            f"{gabarit.name} appelle un CDN sur lequel l'intégrité est impossible"


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


class TestTailwindEstServiParLeDepot:
    def test_le_bundle_vendorise_existe(self):
        assert _TAILWIND.is_file(), \
            "le bundle Tailwind a disparu — la page perdrait sa mise en forme"

    def test_le_bundle_est_bien_celui_quon_croit(self):
        """Sans cette vérification, « plus aucun tiers » ne veut rien dire :
        un fichier vendorisé remplacé en silence est exactement le risque que
        SRI ferme sur un CDN."""
        empreinte = hashlib.sha384(_TAILWIND.read_bytes()).digest()
        import base64
        assert base64.b64encode(empreinte).decode() == _TAILWIND_SHA384

    def test_le_gabarit_le_sert_depuis_notre_origine(self):
        urls = [u for u, _ in _scripts(_RACINE / "templates" / "system.html")]
        assert any(u.startswith("/static/js/tailwind-3.4.17.min.js") for u in urls)

    def test_le_bundle_najoute_aucun_appel_reseau(self):
        """Un bundle vendorisé qui irait chercher des morceaux ailleurs
        rouvrirait la porte qu'on vient de fermer. Les URL présentes doivent
        être des messages d'erreur, jamais des cibles de requête."""
        texte = _TAILWIND.read_text(encoding="utf-8", errors="replace")
        for motif in ("fetch(\"http", "fetch('http", "XMLHttpRequest",
                      "importScripts("):
            assert motif not in texte, f"le bundle tente un accès réseau : {motif}"


class TestLaPageResteChargeable:
    """Le pire résultat de C4 serait une page qui ne charge plus : SRI casse
    BRUYAMMENT, et c'est voulu, mais uniquement quand l'octet a changé."""

    def test_les_quatre_scripts_sont_toujours_la(self):
        urls = [u for u, _ in _scripts(_RACINE / "templates" / "system.html")]
        assert any("react@" in u and "react-dom" not in u for u in urls)
        assert any("react-dom@" in u for u in urls)
        assert any("babel" in u for u in urls)
        assert any("tailwind" in u for u in urls)

    def test_la_config_tailwind_suit_le_script_qui_la_definit(self):
        """`tailwind.config = …` sur un `tailwind` non défini lève, et toute
        la page s'arrête là."""
        texte = _sans_commentaires(_RACINE / "templates" / "system.html")
        assert texte.index("tailwind-3.4.17.min.js") < texte.index("tailwind.config")

    def test_babel_est_charge_avant_le_script_quil_doit_compiler(self):
        texte = _sans_commentaires(_RACINE / "templates" / "system.html")
        assert texte.index("@babel/standalone") < texte.index('type="text/babel"')

    def test_react_est_charge_avant_babel(self):
        """Le bloc JSX déstructure `React` dès sa première ligne."""
        texte = _sans_commentaires(_RACINE / "templates" / "system.html")
        assert texte.index("react-dom@") < texte.index("@babel/standalone")
