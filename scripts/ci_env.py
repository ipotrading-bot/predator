"""
scripts/ci_env.py — L'UNIQUE liste des secrets par « pool », dérivée du registre IA.

POURQUOI. Chaque workflow recopiait ~60 lignes `X: ${{ secrets.X }}`, deux fois.
Trois occurrences de « liste qui diverge » le 2026-08-22 (CLAUDE.md) venaient
de là : un fournisseur PRODUCTION_SAFE absent d'un workflow est ignoré SANS
ERREUR par core/ai_router.py. Ici la liste des clés IA se CALCULE depuis
`core.ai_router.REGISTRY` — même dérivation que `scripts/ops.py::_ai_secrets`
— et les règles d'isolation (GROQ_API_KEY_3 réservée au settlement, REPRICE
sans aucune clé payante, rapports sans clé d'écriture) sont du code testable
(tests/test_ci_env.py), plus des blocs YAML à comparer à la main.

Mesuré le 2026-08-26 : le registre porte 18 fournisseurs, les workflows n'en
câblaient que 15 (CEREBRAS/CHUTES/SAMBANOVA/ZHIPU manquaient partout). La
divergence était déjà là au moment d'écrire ce fichier.

COMMENT — ET POURQUOI PAS `toJSON(secrets)`. La première version de ce
fichier (2026-08-26) exposait tout d'un coup :

    env:
      SECRETS_JSON: ${{ toJSON(secrets) }}

GITHUB REFUSE DE FAIRE TOURNER UN WORKFLOW QUI FAIT ÇA. Message exact, relevé
sur la page des runs : « GitHub detected that this workflow file may be
malicious. It will not run until someone with write access approves it. »
Conclusion `action_required`, ZÉRO job créé, aucun log, aucune annotation —
sur TOUT événement, cron comme dispatch. Cinq des six workflows du dépôt sont
restés muets ainsi ; seul `ci.yml`, qui ne contient pas cette expression,
tournait.

Et GitHub a raison : verser l'intégralité des secrets dans une variable
d'environnement est la signature d'un workflow d'exfiltration, et cette
variable était de toute façon lisible par chaque step du job, `actions/checkout`
et `pip install` compris. Ne JAMAIS chercher à contourner cette détection : ce
serait évader un contrôle de sécurité pour rétablir une pratique qui était
réellement dangereuse.

Les blocs de secrets sont donc de nouveau ÉCRITS dans les YAML — mais plus
JAMAIS à la main : ils sont GÉNÉRÉS depuis les pools ci-dessous et un test
compare chaque bloc à sa source (tests/test_ci_env.py). C'est la parade que
CLAUDE.md prescrit pour toute liste dupliquée : « soit on la dérive, soit un
test la compare à sa source ». Ici, les deux.

    python scripts/ci_env.py --write                # régénère les blocs des workflows
    python scripts/ci_env.py --pool scan --render   # imprime le bloc d'un pool
    python scripts/ci_env.py --pool scan --check    # préflight : sort 1 si KO

Le préflight lit l'environnement du step, jamais un dump. Aucune valeur n'est
imprimée, et il ne faut pas ajouter d'`echo` de debug dans ces steps.

POOLS.
    scan        moteur de scan (run_engine.py) — tout sauf GROQ_API_KEY_3
    closing     capture closing line — Supabase RW + pool Groq scans + IA
    settlement  audit (run_audit.py) — GROQ_API_KEY = secret GROQ_API_KEY_3,
                et AUCUNE autre clé Groq
    reprice     Matchbook vs slate soft — Supabase RW + Telegram, RIEN d'autre
    readonly    rapports, Monte Carlo — clé anon, jamais SUPABASE_SERVICE_KEY
    backfill    backfill_ledger.py — Supabase RW seul
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.ai_router import PRODUCTION_SAFE, REGISTRY  # noqa: E402

# ── Briques ───────────────────────────────────────────────────────────
AI_KEYS = tuple(sorted(p.env_key for p in REGISTRY))
PRODUCTION_KEYS = tuple(sorted(p.env_key for p in PRODUCTION_SAFE))
# Cloudflare Workers AI : l'URL contient l'id de compte, le jeton seul donne
# une URL malformée — panne plus obscure qu'une clé absente.
COMPANIONS = {"CLOUDFLARE_API_TOKEN": ("CLOUDFLARE_ACCOUNT_ID",)}

SUPABASE_RO = ("SUPABASE_URL", "SUPABASE_KEY")
SUPABASE_RW = SUPABASE_RO + ("SUPABASE_SERVICE_KEY",)
TELEGRAM = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
# ODDS_API_KEY(S) restent câblées mais ne sont plus REQUISES (OddsAPI obsolète
# depuis le 2026-08-26) : ne jamais les remettre en garde bloquante.
ODDS_SOURCES = ("ODDS_API_KEY", "ODDS_API_KEYS", "API_FOOTBALL_KEY")
# Clés api-sports du SETTLEMENT (2026-08-26). Le score final est un champ de
# `/fixtures?date=` : sans ces clés dans le pool, core/settlement retomberait
# sur la recherche web — exactement le chemin dont la panne a fait tomber le
# taux de résolution à 11 %. Une capacité non câblée est une capacité morte,
# et elle meurt SANS ERREUR.
RESULTS_SOURCES = ("API_FOOTBALL_KEY", "API_SPORTS_KEY",
                   "API_BASKETBALL_KEY", "API_BASEBALL_KEY")
BETFAIR = ("BETFAIR_APP_KEY", "BETFAIR_USERNAME", "BETFAIR_PASSWORD",
           "BETFAIR_CERT", "BETFAIR_CERT_KEY")
# Sources filtrées par IP (core/net.py) : proxy OU relais Cloudflare Worker.
RELAYS = ("FREE_SOURCES_PROXY", "ODDS500_PROXY", "SEVENM_PROXY",
          "FREE_SOURCES_RELAY", "FREE_SOURCES_RELAY_TOKEN",
          "ODDS500_RELAY", "SEVENM_RELAY")
# Pool Groq des SCANS. _3 est la réserve du settlement (cloisonnement du
# 2026-08-02 : les scans, 10x plus nombreux, vidaient le TPD avant qu'un
# seul WIN/LOSS soit écrit).
GROQ_SCAN = ("GROQ_API_KEY", "GROQ_API_KEY_2", "GROQ_API_KEY_4", "GROQ_API_KEY_5")
GROQ_SETTLEMENT_SOURCE = "GROQ_API_KEY_3"
SEARCH = ("TAVILY_API_KEY",)
FALLBACK_SOURCES = ("API_FOOTBALL_KEY", "GROQ_API_KEY", "TAVILY_API_KEY")


def _companions_of(keys) -> tuple:
    out = []
    for k in keys:
        out.extend(COMPANIONS.get(k, ()))
    return tuple(out)


def _uniq(*groups) -> tuple:
    seen, out = set(), []
    for g in groups:
        for k in g:
            if k not in seen:
                seen.add(k)
                out.append(k)
    return tuple(out)


AI_FULL = _uniq(AI_KEYS, _companions_of(AI_KEYS))
AI_NO_GROQ = tuple(k for k in AI_FULL if k != "GROQ_API_KEY")

# ── Pools ─────────────────────────────────────────────────────────────
# passthrough : nom d'env ← secret du même nom (vide si absent, comme
#               `${{ secrets.X }}` l'aurait fait)
# rename      : nom d'env ← secret d'un AUTRE nom (et le secret homonyme
#               n'est PAS transmis)
# required    : secrets dont l'absence est une erreur
# service_role: vérifier que SUPABASE_SERVICE_KEY porte bien le rôle service_role
# warn_missing: avertissement (pas d'erreur) si absent
POOLS: dict[str, dict] = {
    "scan": dict(
        passthrough=_uniq(SUPABASE_RW, TELEGRAM, ODDS_SOURCES, BETFAIR, RELAYS,
                          RESULTS_SOURCES, GROQ_SCAN, SEARCH, AI_FULL),
        required=SUPABASE_RW, service_role=True, warn_missing=FALLBACK_SOURCES,
        groq_pool=True, groq_fingerprint=True),
    "closing": dict(
        passthrough=_uniq(SUPABASE_RW, GROQ_SCAN, SEARCH, AI_FULL),
        required=SUPABASE_RW, service_role=True),
    "settlement": dict(
        passthrough=_uniq(SUPABASE_RW, TELEGRAM, RESULTS_SOURCES, SEARCH, AI_NO_GROQ),
        rename={"GROQ_API_KEY": GROQ_SETTLEMENT_SOURCE},
        # ⚠️ EN NOMS D'ENV, PAS DE SECRETS. Le bloc généré pose la valeur du
        # secret GROQ_API_KEY_3 sous le nom GROQ_API_KEY ; le nom
        # GROQ_API_KEY_3 n'existe nulle part dans l'environnement du job.
        # Exiger l'ancien nom faisait échouer l'audit sur un secret pourtant
        # présent — vécu le 2026-08-26, run 33008750419.
        required=SUPABASE_RW + ("GROQ_API_KEY",), service_role=True,
        groq_fingerprint=True, warn_missing=SEARCH),
    "reprice": dict(
        passthrough=_uniq(SUPABASE_RW, TELEGRAM),
        required=SUPABASE_RW, service_role=True),
    "readonly": dict(
        passthrough=_uniq(SUPABASE_RO, TELEGRAM),
        required=SUPABASE_RO),
    "backfill": dict(
        passthrough=SUPABASE_RW,
        required=SUPABASE_RW, service_role=True),
}


# ── Construction de l'env ─────────────────────────────────────────────
def env_for(pool: str, secrets: dict) -> dict:
    """L'env (nom → valeur) que le process du pool verra. Pas d'effet de bord."""
    spec = POOLS[pool]
    env = {k: secrets.get(k, "") for k in spec["passthrough"]}
    for dst, src in (spec.get("rename") or {}).items():
        env[dst] = secrets.get(src, "")
    return env


def bootstrap_keys(pool: str) -> tuple:
    """Ce qu'un step de PRÉPARATION reçoit : les clés Supabase du pool, et
    rien d'autre.

    POURQUOI CE BLOC RÉDUIT EXISTE (C5, 2026-08-27)
    -----------------------------------------------
    L'action composite `.github/actions/setup` recevait le pool ENTIER. Or
    elle ne fait pas que le préflight : elle restaure un cache et lance
    `pip install -r requirements.txt`. Toutes les clés IA, de cotes, de
    Telegram et de Betfair étaient donc dans l'environnement d'un `pip`, qui
    exécute du code arbitraire de dizaines de paquets tiers. C'est exactement
    le reproche que CLAUDE.md fait au dump `toJSON(secrets)` — « lisible par
    chaque step du job, `actions/checkout` et `pip install` compris » — sous
    une autre forme.

    Même chose pour le step « Résoudre le mode », qui lance
    `scripts/ci_scan_mode.py` : celui-ci ne lit que SUPABASE_URL,
    SUPABASE_KEY et SUPABASE_SERVICE_KEY (vérifié), et recevait pourtant les
    quarante clés du pool `scan`.

    DÉRIVÉ, jamais listé à la main : on filtre le passthrough du pool. Le pool
    `readonly` n'a pas de SUPABASE_SERVICE_KEY, son bloc d'amorçage n'en aura
    donc pas non plus — l'invariant « readonly ne détient aucun jeton
    d'écriture » tient sans qu'on ait à y penser.
    """
    return tuple(k for k in POOLS[pool]["passthrough"] if k.startswith("SUPABASE_"))


def secret_names_for(pool: str) -> set:
    """Les SECRETS GitHub lus par le pool (≠ noms d'env : cf. rename)."""
    spec = POOLS[pool]
    names = set(spec["passthrough"])
    for dst, src in (spec.get("rename") or {}).items():
        names.discard(dst)
        names.add(src)
    return names



# ── Rendu des blocs YAML (l'ancienne voie toJSON est interdite, cf. en-tête) ──
MARQUE_DEBUT = ("# ▼ GÉNÉRÉ par `python scripts/ci_env.py --write` — pool `{pool}`"
                "{suffixe}. NE PAS ÉDITER.")
MARQUE_FIN = "# ▲ fin du bloc généré (pool `{pool}`{suffixe})"
# Suffixe des blocs RÉDUITS. Il est dans le marqueur pour que `--write` sache
# quoi régénérer sans deviner, et pour qu'un relecteur du YAML voie du premier
# coup d'œil qu'un step est volontairement privé de ses clés.
SUFFIXE_AMORCAGE = ", amorçage (Supabase seul)"
_RE_BLOC = None  # compilé à la volée dans _blocs_de


def render(pool: str, indent: int = 10, amorcage: bool = False) -> str:
    """Le bloc `env:` YAML du pool — une ligne `NOM: ${{ secrets.SOURCE }}`
    par clé, marqueurs compris. Déterministe : l'ordre vient des pools.

    `amorcage=True` rend le bloc RÉDUIT de `bootstrap_keys()` : Supabase seul,
    pour les steps qui préparent le runner sans faire le travail.
    """
    pad = " " * indent
    spec = POOLS[pool]
    suffixe = SUFFIXE_AMORCAGE if amorcage else ""
    lignes = [pad + MARQUE_DEBUT.format(pool=pool, suffixe=suffixe)]
    if amorcage:
        for nom in bootstrap_keys(pool):
            lignes.append(f"{pad}{nom}: ${{{{ secrets.{nom} }}}}")
        lignes.append(pad + MARQUE_FIN.format(pool=pool, suffixe=suffixe))
        return "\n".join(lignes)
    renames = spec.get("rename") or {}
    for nom in spec["passthrough"]:
        if nom in renames:
            continue
        lignes.append(f"{pad}{nom}: ${{{{ secrets.{nom} }}}}")
    for dst, src in renames.items():
        lignes.append(f"{pad}{dst}: ${{{{ secrets.{src} }}}}   "
                      f"# cloisonnement : {dst} du process = secret {src}")
    lignes.append(pad + MARQUE_FIN.format(pool=pool, suffixe=suffixe))
    return "\n".join(lignes)


def _blocs_de(texte: str):
    """[(pool, indent, amorcage, bloc_tel_qu_ecrit)] trouvés dans un YAML."""
    import re
    trouves = []
    debut = re.compile(r"^([ ]*)# \u25bc GÉNÉRÉ par `python scripts/ci_env\.py --write` "
                       r"— pool `([a-z]+)`(, amorçage \(Supabase seul\))?\. "
                       r"NE PAS ÉDITER\.$")
    lignes = texte.split("\n")
    i = 0
    while i < len(lignes):
        m = debut.match(lignes[i])
        if not m:
            i += 1
            continue
        indent, pool = len(m.group(1)), m.group(2)
        amorcage = m.group(3) is not None
        fin = MARQUE_FIN.format(pool=pool,
                                suffixe=SUFFIXE_AMORCAGE if amorcage else "")
        j = i + 1
        while j < len(lignes) and lignes[j].strip() != fin:
            j += 1
        trouves.append((pool, indent, amorcage, "\n".join(lignes[i:j + 1])))
        i = j + 1
    return trouves


def reecrire(texte: str) -> str:
    """Remplace chaque bloc marqué par son rendu à jour."""
    for pool, indent, amorcage, ancien in _blocs_de(texte):
        texte = texte.replace(ancien, render(pool, indent, amorcage), 1)
    return texte

# ── Préflight ─────────────────────────────────────────────────────────
def supabase_role(key: str) -> str:
    """Rôle porté par une clé Supabase, décodé LOCALEMENT (aucun appel réseau).

    Incident 2026-07-07 : SUPABASE_SERVICE_KEY contenait la clé anon. Elle
    s'authentifie (pas de 401) puis chaque écriture échoue en RLS 42501, en
    silence. Deux formats : JWT historique (claim `role`) et clés récentes
    sb_secret_… / sb_publishable_… (le rôle est le préfixe).
    """
    if key.startswith("sb_secret_"):
        return "service_role"
    if key.startswith("sb_publishable_"):
        return "anon"
    parts = key.split(".")
    if len(parts) < 2:
        return "?"
    try:
        pad = "=" * (-len(parts[1]) % 4)
        return str(json.loads(base64.urlsafe_b64decode(parts[1] + pad)).get("role", "?"))
    except Exception:
        return "?"


def check(pool: str, secrets: dict, amorcage: bool = False) -> list[tuple[str, str]]:
    """Retourne [(niveau, message)] avec niveau ∈ {error, warning, notice}.
    N'imprime rien, ne lit pas l'env : testable purement.

    `amorcage=True` : le step ne porte QUE les clés Supabase (C5), on ne
    vérifie donc que les fondations — présence des clés requises visibles et
    rôle de la clé de service. Poursuivre au-delà ferait crier au loup sur des
    secrets absents de CE step mais bien présents dans celui qui travaille :
    un préflight qui alerte à chaque run n'est plus lu, et c'est ainsi qu'on
    perd une vraie alerte.
    """
    spec = POOLS[pool]
    out: list[tuple[str, str]] = []

    exigees = spec["required"]
    if amorcage:
        visibles = set(bootstrap_keys(pool))
        exigees = tuple(k for k in exigees if k in visibles)

    missing = [k for k in exigees if not secrets.get(k)]
    if missing:
        out.append(("error", f"Secrets manquants pour le pool {pool} : {' '.join(missing)} "
                             "— le job sortirait en 0 sans rien avoir fait"))
        return out  # inutile d'aller plus loin sans les fondations

    if spec.get("service_role"):
        role = supabase_role(secrets.get("SUPABASE_SERVICE_KEY", ""))
        if role != "service_role":
            out.append(("error", f"SUPABASE_SERVICE_KEY décode en role='{role}', pas 'service_role' — "
                                 "c'est la clé anon/publishable. Toute écriture sera rejetée par RLS "
                                 "(42501). Correctif : Supabase → Project Settings → API Keys → copier "
                                 "la clé 'service_role' dans ce secret GitHub."))

    if amorcage:
        # Le reste (empreinte Groq, pool Groq, sources de repli) porte sur des
        # secrets que ce step ne reçoit PAS. Le préflight complet tourne dans
        # le step qui travaille — voir `--check` sans `--amorcage`.
        return out

    if spec.get("groq_fingerprint"):
        # LE CLOISONNEMENT NE PEUT PLUS SE VÉRIFIER DANS UN SEUL JOB, et c'est
        # voulu : depuis que chaque step ne reçoit que son pool, aucun process
        # ne voit à la fois la clé des scans et celle du settlement. On publie
        # donc une empreinte irréversible (8 hex d'un SHA-256) : si celle du
        # pool `scan` et celle du pool `settlement` coïncident dans les logs,
        # c'est la MÊME organisation Groq, donc le même quota journalier, donc
        # un cloisonnement fictif — la panne du 2026-08-02 (les scans, 10x plus
        # nombreux, vidaient le TPD avant qu'un seul WIN/LOSS soit écrit).
        cle = secrets.get("GROQ_API_KEY", "")
        if cle:
            out.append(("notice", f"Empreinte GROQ_API_KEY (pool {pool}) : "
                                  f"{hashlib.sha256(cle.encode()).hexdigest()[:8]} — elle doit "
                                  "DIFFÉRER de celle du pool settlement/scan, sinon les deux "
                                  "partagent une organisation Groq et le cloisonnement est fictif."))
        else:
            out.append(("warning", f"GROQ_API_KEY absente du pool {pool}."))

    if spec.get("groq_pool"):
        if not secrets.get("GROQ_API_KEY_2"):
            out.append(("warning", "GROQ_API_KEY_2 non définie — les scans tournent sur une seule "
                                   "organisation Groq (100k TPD)."))
        elif secrets.get("GROQ_API_KEY_2") == secrets.get("GROQ_API_KEY"):
            out.append(("warning", "GROQ_API_KEY_2 identique à GROQ_API_KEY — même organisation, aucun gain."))
        k4 = secrets.get("GROQ_API_KEY_4")
        if not k4:
            out.append(("notice", "GROQ_API_KEY_4 non définie — une 4e clé sur un nouveau compte donnerait "
                                  "300k TPD aux scans sans toucher au settlement."))
        elif k4 in (secrets.get("GROQ_API_KEY"), secrets.get("GROQ_API_KEY_2")):
            out.append(("warning", "GROQ_API_KEY_4 duplique une clé de scan — aucun quota supplémentaire."))

    for k in spec.get("warn_missing") or ():
        if not secrets.get(k):
            hint = " (clé gratuite sur app.tavily.com)" if k == "TAVILY_API_KEY" else ""
            out.append(("warning", f"{k} non configurée — source de repli inerte{hint}. "
                                   "Incident 2026-08-10 → 08-20 : dix jours de « 0 matchs, 0 signaux »."))
    return out


# ── CLI ───────────────────────────────────────────────────────────────
def _load_secrets(pool: str, amorcage: bool = False) -> dict:
    """Les secrets du pool, lus dans l'ENVIRONNEMENT du step.

    C'est le bloc généré du workflow qui les y a mis, un par un. Il n'existe
    plus de dump JSON : GitHub refuse de faire tourner un workflow qui en
    fabrique un (cf. l'en-tête de ce fichier)."""
    noms = (set(bootstrap_keys(pool)) if amorcage
            else set(POOLS[pool]["passthrough"]) | set((POOLS[pool].get("rename") or {})))
    return {k: os.environ.get(k, "") for k in noms}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pool", choices=sorted(POOLS))
    ap.add_argument("--check", action="store_true",
                    help="préflight seul : annotations GitHub, sort 1 si une erreur")
    ap.add_argument("--amorcage", action="store_true",
                    help="le step ne porte que les clés Supabase (C5) : ne "
                         "vérifier que les fondations, sans crier au loup sur "
                         "les secrets qu'il ne reçoit délibérément pas")
    ap.add_argument("--render", action="store_true",
                    help="imprime le bloc `env:` YAML du pool")
    ap.add_argument("--write", action="store_true",
                    help="régénère les blocs marqués de .github/workflows/*.yml")
    args = ap.parse_args(argv)

    if args.write:
        change = []
        for wf in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            avant = wf.read_text(encoding="utf-8")
            apres = reecrire(avant)
            if apres != avant:
                wf.write_text(apres, encoding="utf-8")
                change.append(wf.name)
        print("Blocs régénérés :", ", ".join(change) if change else "aucun changement")
        return 0
    if not args.pool:
        ap.error("--pool est requis (sauf avec --write)")
    if args.render:
        print(render(args.pool, amorcage=args.amorcage))
        return 0

    secrets = _load_secrets(args.pool, args.amorcage)
    findings = check(args.pool, secrets, args.amorcage)
    for level, msg in findings:
        print(f"::{level}::{msg}")
    if any(lvl == "error" for lvl, _ in findings):
        return 1
    attendues = (len(bootstrap_keys(args.pool)) if args.amorcage
                 else len(POOLS[args.pool]["passthrough"]))
    portee = "amorçage (Supabase seul)" if args.amorcage else "complet"
    print(f"Préflight pool={args.pool} [{portee}] OK — {attendues} variables "
          f"attendues, aucune valeur affichée.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
