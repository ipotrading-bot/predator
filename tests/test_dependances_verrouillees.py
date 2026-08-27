"""
tests/test_dependances_verrouillees.py — PHASE D2.

`requirements.txt` ne déclarait que six paquets, tous en `>=`. Le code
réellement exécuté en CI et sur Vercel était donc choisi par PyPI, à chaque
installation, sans commit et sans revue — le même défaut que les URL de CDN
flottantes fermées en C4, et pour la même raison de fond.

Ce n'était pas théorique : `flask>=3.0.0` installait 3.1.3, `pytest>=8.0.0`
installait 9.1.1 (une MAJEURE d'écart), et 52 des 58 paquets présents
n'étaient nommés nulle part — ils entraient par transitivité, libres.

Ce que ces tests gardent :

  · plus aucune borne molle — `==` partout, transitives comprises, sinon
    `supabase==2.31.0` laisse `cryptography`, `pydantic` et `httpx` bouger
    sous lui ;
  · les deux fichiers restent DISJOINTS : ce que Vercel installe ne contient
    pas l'outillage de test ;
  · la CI installe bien le fichier dev, et n'installe plus pyflakes à la
    volée dans un `run:` ;
  · l'outil de déploiement lui-même est épinglé.

⚠️ Ces tests lisent ce que le dépôt DÉCLARE. Que le verrou soit installable
et suffisant a été vérifié autrement, et ne peut pas l'être ici : la suite est
hors réseau. La vérification a consisté à créer un venv vierge, y installer
requirements-dev.txt, comparer `pip freeze` au verrou (48 = 48, aucun écart)
et rejouer les 1362 tests dedans ; puis à n'installer que requirements.txt et
vérifier que `api.index` s'importe sans pytest présent.
"""
import pathlib
import re

import pytest

_RACINE = pathlib.Path(__file__).resolve().parent.parent
_RUNTIME = _RACINE / "requirements.txt"
_DEV = _RACINE / "requirements-dev.txt"
_EPINGLE = re.compile(r"^([A-Za-z0-9._-]+)==([A-Za-z0-9._+!-]+)$")


def _exigences(fichier: pathlib.Path) -> list[str]:
    """Les lignes utiles : ni commentaire, ni vide, ni inclusion `-r`."""
    return [ligne.strip() for ligne in fichier.read_text(encoding="utf-8").splitlines()
            if ligne.strip() and not ligne.lstrip().startswith(("#", "-r", "-c"))]


def _noms(fichier: pathlib.Path) -> set[str]:
    return {m.group(1).lower().replace("_", "-")
            for m in (_EPINGLE.match(l) for l in _exigences(fichier)) if m}


class TestPlusAucuneBorneMolle:
    @pytest.mark.parametrize("fichier", [_RUNTIME, _DEV], ids=lambda f: f.name)
    def test_chaque_ligne_est_epinglee_a_une_version_exacte(self, fichier):
        for ligne in _exigences(fichier):
            assert _EPINGLE.match(ligne), (
                f"{fichier.name} : {ligne!r} n'épingle pas une version exacte. "
                "Un `>=` rend l'installation non déterministe — c'est PyPI qui "
                "décide alors du code exécuté en production.")

    @pytest.mark.parametrize("fichier", [_RUNTIME, _DEV], ids=lambda f: f.name)
    def test_aucun_operateur_relache_nulle_part(self, fichier):
        """`~=` et `>=` compris : `~=` autorise encore la version mineure
        suivante, ce qui suffit à changer le comportement sans commit."""
        for ligne in _exigences(fichier):
            for op in (">=", "<=", "~=", ">", "<", "!="):
                assert op not in ligne, f"{fichier.name} : opérateur {op} dans {ligne!r}"

    def test_les_transitives_sont_epinglees_elles_aussi(self):
        """Le point qui distingue un vrai verrou d'une liste de vœux. Cinq
        directes seulement — s'il n'y a qu'elles, tout le reste flotte."""
        assert len(_noms(_RUNTIME)) >= 40, (
            "requirements.txt ne porte que ses dépendances directes : les "
            "transitives restent libres, donc l'installation reste variable")


class TestLeRuntimeNeContientPasLOutillageDeTest:
    def test_pytest_nest_pas_dans_ce_que_vercel_installe(self):
        """Il partait dans le bundle serverless, où rien ne l'exécute jamais.
        Et chaque paquet installé est du code tiers lancé par `pip install`
        dans un job qui porte des clés (C5)."""
        for outil in ("pytest", "pyflakes", "pluggy", "iniconfig"):
            assert outil not in _noms(_RUNTIME), f"{outil} ne devrait pas partir sur Vercel"

    def test_le_fichier_dev_tire_le_runtime(self):
        """Sans l'inclusion, un venv de dev pourrait avoir pytest sans flask —
        et les tests s'exécuteraient contre un arbre différent de la prod."""
        assert "-r requirements.txt" in _DEV.read_text(encoding="utf-8")

    def test_les_deux_fichiers_ne_se_recouvrent_pas(self):
        """Une version déclarée deux fois finit par diverger — c'est la panne
        la plus fréquente de ce dépôt (voir CLAUDE.md, « listes qui
        divergent »)."""
        commun = _noms(_RUNTIME) & _noms(_DEV)
        assert not commun, f"déclarés des deux côtés : {sorted(commun)}"

    def test_les_directes_du_runtime_sont_bien_celles_quon_importe(self):
        assert {"flask", "requests", "supabase", "python-dotenv", "pyyaml"} <= _noms(_RUNTIME)


def _sans_commentaires(chemin: pathlib.Path) -> str:
    """Le YAML privé de ses commentaires.

    Indispensable, et pour la raison déjà rencontrée en C4 : les commentaires
    de ce dépôt CITENT ce qui a été retiré pour expliquer pourquoi. La ligne
    qui épingle le CLI Vercel est précédée d'une note nommant `vercel@latest`.
    Un commentaire ne s'exécute pas ; l'analyser ferait échouer la garde sur
    sa propre documentation, et pousserait à l'effacer.

    ⚠️ On ne coupe qu'au `#` précédé d'un espace ou en début de ligne : un `#`
    collé à du texte peut appartenir à une valeur.
    """
    lignes = []
    for ligne in chemin.read_text(encoding="utf-8").splitlines():
        nu = ligne.lstrip()
        if nu.startswith("#"):
            continue
        lignes.append(ligne.split(" #", 1)[0] if " #" in ligne else ligne)
    return "\n".join(lignes)


class TestLaCIUtiliseReellementLeVerrou:
    @staticmethod
    def _ci():
        return _sans_commentaires(_RACINE / ".github" / "workflows" / "ci.yml")

    @staticmethod
    def _setup():
        return _sans_commentaires(_RACINE / ".github" / "actions" / "setup" / "action.yml")

    def test_le_job_de_test_demande_les_dependances_de_dev(self):
        assert "dev: 'true'" in self._ci()

    def test_pyflakes_nest_plus_installe_a_la_volee(self):
        """Le linter qui juge le dépôt était choisi par PyPI au moment du run."""
        assert "pip install -q pyflakes" not in self._ci()
        assert "pyflakes" in _noms(_DEV)

    def test_la_cle_de_cache_couvre_les_deux_fichiers(self):
        """Sinon un changement de requirements-dev.txt réutiliserait un cache
        périmé, et la version installée ne serait pas celle du dépôt."""
        setup = self._setup()
        assert "requirements.txt" in setup and "requirements-dev.txt" in setup
        cle = [l for l in setup.splitlines() if "key:" in l and "sitepkg" in l]
        assert cle and "requirements-dev.txt" in cle[0], \
            "la clé de cache ignore requirements-dev.txt"

    def test_la_cle_de_cache_distingue_dev_et_runtime(self):
        """Sans cela, deux jobs partagent une entrée et le premier arrivé
        décide si pytest est présent pour l'autre."""
        cle = [l for l in self._setup().splitlines() if "key:" in l and "sitepkg" in l][0]
        assert "inputs.dev" in cle

    def test_le_chemin_de_declenchement_inclut_le_fichier_dev(self):
        """Un fichier gardé qui change sans réveiller son gardien, c'est
        exactement ce qui s'est produit le 2026-08-26 avec .github/**."""
        assert "'requirements-dev.txt'" in self._ci()

    def test_le_temoin_du_filtre_de_commentaires(self):
        """Sans lui, `_sans_commentaires` pourrait tout effacer et chaque
        assertion « X n'est pas là » passerait pour la mauvaise raison."""
        ci = self._ci()
        assert "vercel@latest" in (_RACINE / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "npm install -g vercel@" in ci and "runs-on: ubuntu-latest" in ci

    def test_loutil_de_deploiement_est_epingle(self):
        """`vercel@latest` laissait npm choisir, au moment du run, l'outil qui
        pousse en PRODUCTION — le dernier maillon flottant, et le plus
        sensible."""
        ci = self._ci()
        assert "vercel@latest" not in ci
        assert re.search(r"npm install -g vercel@\d+\.\d+\.\d+", ci), \
            "le CLI Vercel doit porter une version exacte"
