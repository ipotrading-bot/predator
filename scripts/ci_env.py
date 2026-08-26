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
                          GROQ_SCAN, SEARCH, AI_FULL),
        required=SUPABASE_RW, service_role=True, warn_missing=FALLBACK_SOURCES),
    "closing": dict(
        passthrough=_uniq(SUPABASE_RW, GROQ_SCAN, SEARCH, AI_FULL),
        required=SUPABASE_RW, service_role=True),
    "settlement": dict(
        passthrough=_uniq(SUPABASE_RW, TELEGRAM, SEARCH, AI_NO_GROQ),
        rename={"GROQ_API_KEY": GROQ_SETTLEMENT_SOURCE},
        required=SUPABASE_RW + (GROQ_SETTLEMENT_SOURCE,), service_role=True,
        groq_isolation=True, warn_missing=SEARCH),
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


def secret_names_for(pool: str) -> set:
    """Les SECRETS GitHub lus par le pool (≠ noms d'env : cf. rename)."""
    spec = POOLS[pool]
    names = set(spec["passthrough"])
    for dst, src in (spec.get("rename") or {}).items():
        names.discard(dst)
        names.add(src)
    return names



# ── Rendu des blocs YAML (l'ancienne voie toJSON est interdite, cf. en-tête) ──
MARQUE_DEBUT = "# ▼ GÉNÉRÉ par `python scripts/ci_env.py --write` — pool `{pool}`. NE PAS ÉDITER."
MARQUE_FIN = "# ▲ fin du bloc généré (pool `{pool}`)"
_RE_BLOC = None  # compilé à la volée dans _blocs_de


def render(pool: str, indent: int = 10) -> str:
    """Le bloc `env:` YAML du pool — une ligne `NOM: ${{ secrets.SOURCE }}`
    par clé, marqueurs compris. Déterministe : l'ordre vient des pools."""
    pad = " " * indent
    spec = POOLS[pool]
    renames = spec.get("rename") or {}
    lignes = [pad + MARQUE_DEBUT.format(pool=pool)]
    for nom in spec["passthrough"]:
        if nom in renames:
            continue
        lignes.append(f"{pad}{nom}: ${{{{ secrets.{nom} }}}}")
    for dst, src in renames.items():
        lignes.append(f"{pad}{dst}: ${{{{ secrets.{src} }}}}   "
                      f"# cloisonnement : {dst} du process = secret {src}")
    lignes.append(pad + MARQUE_FIN.format(pool=pool))
    return "\n".join(lignes)


def _blocs_de(texte: str):
    """[(pool, indent, bloc_tel_qu_ecrit)] trouvés dans un YAML."""
    import re
    trouves = []
    debut = re.compile(r"^([ ]*)# \u25bc GÉNÉRÉ par `python scripts/ci_env\.py --write` "
                       r"— pool `([a-z]+)`\. NE PAS ÉDITER\.$")
    lignes = texte.split("\n")
    i = 0
    while i < len(lignes):
        m = debut.match(lignes[i])
        if not m:
            i += 1
            continue
        indent, pool = len(m.group(1)), m.group(2)
        fin = MARQUE_FIN.format(pool=pool)
        j = i + 1
        while j < len(lignes) and lignes[j].strip() != fin:
            j += 1
        trouves.append((pool, indent, "\n".join(lignes[i:j + 1])))
        i = j + 1
    return trouves


def reecrire(texte: str) -> str:
    """Remplace chaque bloc marqué par son rendu à jour."""
    for pool, indent, ancien in _blocs_de(texte):
        texte = texte.replace(ancien, render(pool, indent), 1)
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


def check(pool: str, secrets: dict) -> list[tuple[str, str]]:
    """Retourne [(niveau, message)] avec niveau ∈ {error, warning, notice}.
    N'imprime rien, ne lit pas l'env : testable purement."""
    spec = POOLS[pool]
    out: list[tuple[str, str]] = []

    missing = [k for k in spec["required"] if not secrets.get(k)]
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

    if spec.get("groq_isolation"):
        settle = secrets.get(GROQ_SETTLEMENT_SOURCE, "")
        for k in GROQ_SCAN:
            if secrets.get(k) and secrets[k] == settle:
                out.append(("error", f"{GROQ_SETTLEMENT_SOURCE} est identique à {k} — même organisation "
                                     "Groq, même quota journalier : le cloisonnement est fictif. "
                                     "Créer la clé sur un AUTRE compte console.groq.com."))
        if not secrets.get("GROQ_API_KEY"):
            out.append(("warning", "GROQ_API_KEY non définie — les scans n'ont plus de clé Groq."))
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
def _load_secrets(pool: str) -> dict:
    """Les secrets du pool, lus dans l'ENVIRONNEMENT du step.

    C'est le bloc généré du workflow qui les y a mis, un par un. Il n'existe
    plus de dump JSON : GitHub refuse de faire tourner un workflow qui en
    fabrique un (cf. l'en-tête de ce fichier)."""
    noms = set(POOLS[pool]["passthrough"]) | set((POOLS[pool].get("rename") or {}))
    return {k: os.environ.get(k, "") for k in noms}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pool", choices=sorted(POOLS))
    ap.add_argument("--check", action="store_true",
                    help="préflight seul : annotations GitHub, sort 1 si une erreur")
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
        print(render(args.pool))
        return 0

    secrets = _load_secrets(args.pool)
    findings = check(args.pool, secrets)
    for level, msg in findings:
        print(f"::{level}::{msg}")
    if any(lvl == "error" for lvl, _ in findings):
        return 1
    print(f"Préflight pool={args.pool} OK — {len(POOLS[args.pool]['passthrough'])} variables "
          f"attendues, aucune valeur affichée.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
