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

COMMENT. Le workflow expose TOUS ses secrets en un seul bloc masqué :

    env:
      SECRETS_JSON: ${{ toJSON(secrets) }}

et chaque commande passe par ce script, qui ne laisse atteindre le process
QUE les clés du pool demandé :

    python scripts/ci_env.py --pool scan --check                 # préflight : sort 1 si KO
    python scripts/ci_env.py --pool scan -- python run_engine.py # exec avec l'env du pool

Rien n'est écrit dans $GITHUB_ENV ni sur disque : SECRETS_JSON reste confiné à
l'env du step, retiré de l'env du process lancé. Aucune valeur n'est jamais
imprimée. ⚠️ NE JAMAIS ajouter un `echo` de debug dans ces steps : le masquage
de GitHub reconnaît mal une valeur multi-lignes ré-encodée par toJSON (le PEM
de BETFAIR_CERT devient des `\n` littéraux).

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
def _load_secrets() -> dict:
    raw = os.environ.get("SECRETS_JSON", "")
    if not raw:
        sys.stderr.write("::error::SECRETS_JSON absent — le job doit poser "
                         "`SECRETS_JSON: ${{ toJSON(secrets) }}` dans son env.\n")
        sys.exit(2)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        sys.stderr.write("::error::SECRETS_JSON n'est pas du JSON valide.\n")
        sys.exit(2)
    return {k: ("" if v is None else str(v)) for k, v in data.items()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pool", required=True, choices=sorted(POOLS))
    ap.add_argument("--check", action="store_true",
                    help="préflight seul : annotations GitHub, sort 1 si une erreur")
    ap.add_argument("cmd", nargs="*", help="commande à exécuter avec l'env du pool (après --)")
    args = ap.parse_args(argv)

    secrets = _load_secrets()
    findings = check(args.pool, secrets)
    for level, msg in findings:
        print(f"::{level}::{msg}")
    if any(lvl == "error" for lvl, _ in findings):
        return 1
    if args.check:
        print(f"Préflight pool={args.pool} OK — {len(POOLS[args.pool]['passthrough'])} variables "
              f"transmises, aucune valeur affichée.")
        return 0
    if not args.cmd:
        ap.error("aucune commande : `--check` ou `-- <commande>`")

    env = {k: v for k, v in os.environ.items() if k != "SECRETS_JSON"}
    env.update(env_for(args.pool, secrets))
    sys.stdout.flush()
    os.execvpe(args.cmd[0], args.cmd, env)
    return 0  # jamais atteint


if __name__ == "__main__":
    sys.exit(main())
