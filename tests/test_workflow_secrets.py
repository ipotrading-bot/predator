"""
tests/test_workflow_secrets.py — un secret non passé rend une capacité INERTE.

POURQUOI CE FICHIER EXISTE (deux occurrences réelles, toutes deux 2026-08-22)

1. `wiz.yml` ne passait AUCUNE clé du routeur IA. Le workflow omet
   volontairement `GROQ_API_KEY_3` pour protéger la réserve settlement du
   moteur — mais le dernier recours du cerveau de Wiz, `ai_complete()`,
   trouvait alors le routeur VIDE et tombait droit sur Groq. Le workflow
   contournait par l'autre bout la réserve qu'il prétendait protéger.

2. `OVH_AI_API_KEY` et `SILICONFLOW_API_KEY` — deux fournisseurs
   PRODUCTION_SAFE du registre, documentés dans `.env.example` — n'étaient
   câblés dans AUCUN des sept workflows IA. Deux fournisseurs sur neuf
   inatteignables en production.

Le trait commun est ce qui rend la panne coûteuse : **une clé absente ne lève
rien**. `core/ai_router.py` ignore silencieusement un fournisseur sans clé,
c'est même sa propriété désirable (un palier gratuit qui ferme ne doit pas
casser un run). La contrepartie, c'est qu'une capacité peut rester morte des
mois sans un seul log, sans un seul test rouge — jusqu'à ce qu'on mesure et
qu'on découvre que tout le trafic partait sur un seul fournisseur.

Aucune assertion ici ne teste du COMPORTEMENT : elles testent que le câblage
existe. C'est précisément ce que la suite ne pouvait pas voir, puisque les
tests tournent sans workflow et que le code, lui, est correct.
"""
import json
import re
from pathlib import Path

import pytest
import yaml

from core.ai_router import PRODUCTION_SAFE, REGISTRY

WF_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"
WORKFLOWS = sorted(WF_DIR.glob("*.yml"))

# Clés d'environnement de tous les fournisseurs du registre, et celles du
# seul sous-ensemble que les lanes de production utilisent réellement.
TOUTES_CLES_IA = {p.env_key for p in REGISTRY}
CLES_PRODUCTION = {p.env_key for p in PRODUCTION_SAFE}

# Cloudflare Workers AI est le seul fournisseur dont l'appel a besoin d'une
# SECONDE variable : l'URL contient l'identifiant de compte. Un jeton sans
# account id ne produit pas une erreur d'authentification mais une URL
# malformée — panne plus obscure que l'absence pure et simple.
COMPAGNONS = {"CLOUDFLARE_API_TOKEN": "CLOUDFLARE_ACCOUNT_ID"}


def _secrets(path: Path) -> set:
    """Les noms de secrets référencés par un workflow (`${{ secrets.X }}`)."""
    return set(re.findall(r"secrets\.([A-Z0-9_]+)", path.read_text(encoding="utf-8")))


def _est_workflow_ia(path: Path) -> bool:
    """Un workflow « fait de l'IA » s'il câble au moins un fournisseur.

    Définition volontairement auto-cohérente : dès qu'un workflow en passe
    UN, il doit les passer TOUS. C'est ce qui transforme la règle en
    invariant vérifiable, plutôt qu'en liste à tenir à jour à la main —
    laquelle serait exactement le genre de liste qui dérive.
    """
    return bool(_secrets(path) & TOUTES_CLES_IA)


WORKFLOWS_IA = [p for p in WORKFLOWS if _est_workflow_ia(p)]


def test_il_y_a_bien_des_workflows_a_verifier():
    # Garde-fou du garde-fou : si le repérage casse, tout ce module
    # passerait à vide en prouvant zéro chose.
    assert len(WORKFLOWS) >= 10
    assert len(WORKFLOWS_IA) >= 5


@pytest.mark.parametrize("wf", WORKFLOWS_IA, ids=lambda p: p.name)
def test_tout_fournisseur_de_production_est_cable(wf):
    """Chaque fournisseur sans terms_flag doit atteindre le runner."""
    presents = _secrets(wf)
    manquants = sorted(CLES_PRODUCTION - presents)
    assert not manquants, (
        f"{wf.name} ne passe pas {manquants}. Ces fournisseurs sont dans "
        "PRODUCTION_SAFE : sans la ligne `${{ secrets.X }}`, le routeur ne "
        "les verra jamais et la capacité restera morte SANS ERREUR.")


@pytest.mark.parametrize("wf", WORKFLOWS_IA, ids=lambda p: p.name)
def test_cloudflare_a_son_identifiant_de_compte(wf):
    presents = _secrets(wf)
    for jeton, compagnon in COMPAGNONS.items():
        if jeton in presents:
            assert compagnon in presents, (
                f"{wf.name} passe {jeton} sans {compagnon} — l'URL de "
                "Workers AI contient l'identifiant de compte, le jeton seul "
                "ne suffit pas.")


def test_les_workflows_ia_ne_divergent_pas_entre_eux():
    """Deux listes qui divergent, c'est la panne 1 ci-dessus.

    On compare le sous-ensemble IA de chaque workflow. Les clés HORS registre
    (Groq numérotées, Mistral, Tavily…) sont volontairement exclues : wiz.yml
    omet GROQ_API_KEY_3 pour de bonnes raisons, et c'est une divergence
    légitime que ce test ne doit pas interdire.
    """
    vues = {wf.name: _secrets(wf) & TOUTES_CLES_IA for wf in WORKFLOWS_IA}
    reference = max(vues.values(), key=len)
    ecarts = {nom: sorted(reference - s) for nom, s in vues.items() if reference - s}
    assert not ecarts, (
        f"Listes de fournisseurs IA divergentes : {ecarts}. Un workflow qui "
        "en câble moins que les autres concentre son trafic sur les rares "
        "fournisseurs qu'il voit — c'est ce qui a envoyé tout le repli de "
        "Wiz sur Groq.")


def test_aucune_cle_ia_inconnue_du_registre():
    """Attrape la faute de frappe : `NVIDIA_NIN_API_KEY` ne lèverait rien."""
    suspects = set()
    for wf in WORKFLOWS:
        for s in _secrets(wf):
            if s.endswith("_API_KEY") and s not in TOUTES_CLES_IA:
                suspects.add(s)
    # Clés légitimes hors registre IA : sources de cotes, Telegram, Mistral
    # (volontairement HORS registre — domaine de panne séparé pour Wiz),
    # Groq numérotées (pool géré par core/ai_search.py).
    connues = {
        "ODDS_API_KEY", "ODDS_API_KEYS", "API_FOOTBALL_KEY", "API_SPORTS_KEY",
        "MISTRAL_API_KEY", "TAVILY_API_KEY", "TELEGRAM_BOT_TOKEN",
        "GROQ_API_KEY_2", "GROQ_API_KEY_3", "GROQ_API_KEY_4", "GROQ_API_KEY_5",
        "BETFAIR_APP_KEY", "VERCEL_TOKEN",
    }
    inconnues = sorted(suspects - connues)
    assert not inconnues, (
        f"Secrets d'API inconnus du registre et non listés ici : {inconnues}. "
        "Soit c'est une faute de frappe (le fournisseur ne sera jamais vu), "
        "soit un nouveau fournisseur à ajouter à core/ai_router.py.")


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


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_les_workflows_partagent_une_seule_version_de_python(wf):
    """Les runners, eux, doivent rester d'accord entre eux : un workflow en
    3.12 exécuterait un code testé sur 3.11 par tous les autres."""
    versions = set(re.findall(r"python-version:\s*'?\"?([0-9.]+)",
                              wf.read_text(encoding="utf-8")))
    fautives = sorted(v for v in versions if v != VERSION_RUNNERS)
    assert not fautives, (
        f"{wf.name} utilise Python {fautives} ; les workflows du dépôt sont "
        f"sur {VERSION_RUNNERS}.")


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
