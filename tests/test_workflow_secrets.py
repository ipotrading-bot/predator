"""
tests/test_workflow_secrets.py — hygiène des workflows (bornes de durée, version
de Python unique, ops.py et .env.example alignés sur le registre IA).

Le câblage des secrets par workflow (fournisseurs IA ↔ jobs, cloisonnement
Groq, REPRICE sans clé payante) n'est PLUS vérifié ici par regex sur le YAML :
depuis la refonte du 2026-08-26 les workflows ne listent aucun secret à la
main. Il est calculé par scripts/ci_env.py et testé dans tests/test_ci_env.py.
"""
import json
import re
from pathlib import Path

import pytest
import yaml

from core.ai_router import REGISTRY

WF_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"
WORKFLOWS = sorted(WF_DIR.glob("*.yml"))
# L'action composite porte le setup-python commun : même version que les workflows.
ACTIONS = sorted((WF_DIR.parent / "actions").glob("*/action.yml"))
TOUTES_CLES_IA = {p.env_key for p in REGISTRY}

# ── Hygiène des workflows (hors IA) ──────────────────────────────────

@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_chaque_job_a_une_borne_de_duree(wf):
    """`deploy.yml` n'en avait pas : un `vercel deploy` pendu aurait consommé
    les 6 h par défaut de GitHub Actions, sur le workflow le plus fréquent."""
    doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
    sans = [nom for nom, job in (doc.get("jobs") or {}).items()
            if "timeout-minutes" not in job]
    assert not sans, f"{wf.name}: jobs sans timeout-minutes : {sans}"


# Le dépôt vit sur DEUX interpréteurs, et c'est SUBI, pas choisi :
#
#   - les crons GitHub Actions et le développement local  → Python 3.11
#   - le dashboard construit par Vercel                   → Python 3.12
#
# Ce n'est pas une incohérence à « réparer ». L'image de build Vercel
# n'embarque tout simplement pas 3.11 :
#
#     Warning: Python version "3.11" detected in .python-version is not
#              installed and will be ignored.
#     Using python version: 3.12
#     error: No interpreter found for Python 3.11 in managed installations
#
# VÉCU LE 2026-08-22 : un premier passage de cet audit a « aligné »
# `.python-version` sur 3.11 en le croyant seul discordant contre les 14
# workflows. Le déploiement suivant a ÉCHOUÉ, et la production est restée
# bloquée sur le commit précédent — donc sans le correctif de sécurité de
# `/api/audit/run`. Un test qui encode la mauvaise règle ne se contente pas
# d'être inutile : il donne l'autorité d'une suite verte à une erreur.
#
# `.python-version` appartient donc à VERCEL. La règle réellement utile est
# que les workflows ne divergent pas ENTRE EUX.
VERSION_VERCEL = "3.12"
VERSION_RUNNERS = "3.11"


def test_python_version_appartient_a_vercel():
    """Ne pas « aligner » ce fichier sur les workflows — voir le commentaire
    ci-dessus : Vercel n'a pas 3.11 et le déploiement casse."""
    lu = (Path(__file__).resolve().parent.parent
          / ".python-version").read_text(encoding="utf-8").strip()
    assert lu == VERSION_VERCEL, (
        f".python-version vaut {lu!r} ; Vercel exige {VERSION_VERCEL!r} — "
        "son image de build n'embarque pas 3.11 et le déploiement échoue, "
        "laissant la production sur le commit précédent.")


def test_vercel_json_annonce_la_meme_version_que_python_version():
    """Deux fichiers de configuration Vercel qui se contredisent, c'est la
    prochaine personne qui « corrige » le mauvais des deux."""
    racine = Path(__file__).resolve().parent.parent
    conf = json.loads((racine / "vercel.json").read_text(encoding="utf-8"))
    annonce = (conf.get("env") or {}).get("PYTHON_VERSION")
    assert annonce == VERSION_VERCEL, (
        f"vercel.json annonce PYTHON_VERSION={annonce!r} et .python-version "
        f"{VERSION_VERCEL!r}.")


@pytest.mark.parametrize("wf", WORKFLOWS + ACTIONS,
                         ids=lambda p: f"{p.parent.name}/{p.name}")
def test_les_workflows_partagent_une_seule_version_de_python(wf):
    """Les runners, eux, doivent rester d'accord entre eux : un workflow en
    3.12 exécuterait un code testé sur 3.11 par tous les autres."""
    versions = set(re.findall(r"python-version:\s*'?\"?([0-9.]+)",
                              wf.read_text(encoding="utf-8")))
    fautives = sorted(v for v in versions if v != VERSION_RUNNERS)
    assert not fautives, (
        f"{wf.name} utilise Python {fautives} ; les workflows du dépôt sont "
        f"sur {VERSION_RUNNERS}.")


def test_la_version_de_python_des_runners_est_declaree_une_seule_fois():
    """Depuis la refonte du 2026-08-26 elle vit dans .github/actions/setup :
    un workflow qui reposerait son propre setup-python rouvrirait la porte à
    la divergence que le test ci-dessus rattrape après coup."""
    fautifs = [wf.name for wf in WORKFLOWS
               if "python-version:" in wf.read_text(encoding="utf-8")]
    assert not fautifs, (
        f"{fautifs} déclarent python-version au lieu d'utiliser "
        "./.github/actions/setup")


# ── La TROISIÈME liste : scripts/ops.py ──────────────────────────────

def _ops_module():
    """Charge scripts/ops.py — ce n'est pas un paquet importable."""
    import importlib.util
    chemin = Path(__file__).resolve().parent.parent / "scripts" / "ops.py"
    spec = importlib.util.spec_from_file_location("_ops_sous_test", chemin)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_secrets_push_couvre_tout_le_registre():
    """`ops.py secrets-push` est le chemin RECOMMANDÉ par .env.example pour
    installer les clés. Une clé absente de sa liste blanche est sautée sans
    un mot : l'opérateur croit avoir tout poussé, et le fournisseur reste
    invisible en production.

    C'était le cas d'OVH_AI_API_KEY — PRODUCTION_SAFE, et absent du tuple
    écrit à la main. Troisième liste de fournisseurs du dépôt, troisième
    divergence le même jour. Elle est désormais dérivée du registre ; ce
    test interdit le retour à une liste figée.
    """
    manquants = sorted(TOUTES_CLES_IA - set(_ops_module()._AI_SECRETS))
    assert not manquants, (
        f"scripts/ops.py::_AI_SECRETS ne couvre pas {manquants} — "
        "`secrets-push` sauterait ces clés en silence.")


def test_secrets_push_nemporte_pas_les_cles_operateur():
    """L'inverse compte aussi : SUPABASE_ACCESS_TOKEN et VERCEL_* ne servent
    qu'aux gestes manuels depuis un poste de dev. Les pousser aux runners
    élargirait la surface d'exposition sans aucun bénéfice — aucun workflow
    ne les lit."""
    interdits = {"SUPABASE_ACCESS_TOKEN", "SUPABASE_SERVICE_KEY",
                 "VERCEL_TOKEN", "VERCEL_PROJECT", "VERCEL_TEAM_ID",
                 "GITHUB_PAT", "BETFAIR_PASSWORD"}
    fuites = sorted(interdits & set(_ops_module()._AI_SECRETS))
    assert not fuites, f"secrets-push pousserait des clés non-IA : {fuites}"


def test_env_example_documente_les_credentials_reellement_lus():
    """`.env.example` dit « copiez ce fichier en .env ». S'il omet un
    credential que le code lit, la fonctionnalité est inaccessible à qui
    part d'une copie propre — c'était le cas des cinq variables Betfair et
    de tout le bloc de scripts/ops.py, alors même que CLAUDE.md présente
    `ops.py` comme LA façon de piloter Supabase et Vercel."""
    exemple = (Path(__file__).resolve().parent.parent
               / ".env.example").read_text(encoding="utf-8")
    attendus = [
        "BETFAIR_USERNAME", "BETFAIR_PASSWORD", "BETFAIR_APP_KEY",
        "BETFAIR_CERT", "BETFAIR_CERT_KEY",
        "SUPABASE_ACCESS_TOKEN", "SUPABASE_PROJECT_REF",
        "VERCEL_TOKEN", "VERCEL_PROJECT", "VERCEL_TEAM_ID",
        "GITHUB_PAT",
    ]
    manquants = [v for v in attendus
                 if not re.search(rf"^{v}=", exemple, re.M)]
    assert not manquants, f".env.example ne documente pas : {manquants}"


def test_tout_fournisseur_du_registre_est_documente_dans_env_example():
    """Un fournisseur qu'on ne sait pas configurer n'existe pas."""
    exemple = (Path(__file__).resolve().parent.parent
               / ".env.example").read_text(encoding="utf-8")
    manquants = sorted(c for c in TOUTES_CLES_IA
                       if not re.search(rf"^{c}=", exemple, re.M))
    assert not manquants, (
        f".env.example ne liste pas {manquants} — l'opérateur n'a aucun "
        "moyen d'apprendre que ces fournisseurs sont supportés.")
