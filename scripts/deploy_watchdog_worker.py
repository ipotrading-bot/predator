#!/usr/bin/env python3
"""Déploie le chien de garde des crons (scripts/cloudflare_watchdog_worker.js)
sur Cloudflare Workers. Idempotent : rejouable à volonté.

Pourquoi il existe : le relais (predator-relay) avait été déployé à la main et
le piège du sous-domaine désactivé a coûté une soirée (voir INCIDENTS.md,
« odds500 n'est pas en panne »). Ce script rend le déploiement reproductible.
Le chien de garde n'a PAS besoin de sous-domaine : les crons Cloudflare
s'exécutent sans URL publique — pas de piège workers.dev ici.

Credentials (``.env`` à la racine, ou l'environnement) :
  CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID — compte Workers ;
  GITHUB_PAT — posé comme secret WATCHDOG_PAT du Worker (c'est le même PAT
  que Vercel utilise pour /api/audit/run ; il ne quitte jamais ce fichier ni
  le stockage chiffré de Cloudflare).

Étapes : upload du module ES (en gardant les secrets existants), pose du
secret WATCHDOG_PAT, pose du cron (*/10), puis relecture de contrôle.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

NAME = "predator-watchdog"
CRON = "*/10 * * * *"
SOURCE = ROOT / "scripts" / "cloudflare_watchdog_worker.js"

TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")
ACCOUNT = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
PAT = os.environ.get("GITHUB_PAT", "")
BASE = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/workers/scripts/{NAME}"
HEAD = {"Authorization": f"Bearer {TOKEN}"}


def _die(msg: str) -> None:
    print(f"ERREUR : {msg}", file=sys.stderr)
    sys.exit(1)


def _check(r: requests.Response, quoi: str) -> dict:
    body = r.json() if r.content else {}
    if not (r.ok and body.get("success", True)):
        # Le cas VÉCU le 2026-08-27 : le jeton du .env lit les Workers
        # (il liste predator-relay) mais n'a pas la permission d'écriture,
        # d'où un 403 « Authentication error » qui ressemble à un jeton
        # invalide alors qu'il est actif. Le dire en clair évite de partir
        # chercher un jeton expiré qui ne l'est pas.
        if r.status_code == 403:
            _die(f"{quoi} : HTTP 403 — le jeton Cloudflare est probablement en "
                 "LECTURE SEULE sur les Workers. Il lui faut la permission "
                 "« Account | Workers Scripts | Edit ». Créer un jeton sur "
                 "https://dash.cloudflare.com/profile/api-tokens (modèle « Edit "
                 "Cloudflare Workers »), puis remplacer CLOUDFLARE_API_TOKEN "
                 "dans .env et rejouer ce script.")
        _die(f"{quoi} : HTTP {r.status_code} {json.dumps(body.get('errors', body))[:400]}")
    return body


def main() -> int:
    for var, val in (("CLOUDFLARE_API_TOKEN", TOKEN),
                     ("CLOUDFLARE_ACCOUNT_ID", ACCOUNT),
                     ("GITHUB_PAT", PAT)):
        if not val:
            _die(f"{var} absent (.env ou environnement)")

    code = SOURCE.read_text(encoding="utf-8")

    # 1. Upload du module. keep_bindings préserve un WATCHDOG_PAT déjà posé
    #    (on le re-pose ensuite de toute façon — l'étape 2 fait foi).
    metadata = {"main_module": "worker.js",
                "compatibility_date": "2026-08-01",
                "keep_bindings": ["secret_text"]}
    r = requests.put(
        BASE, headers=HEAD, timeout=30,
        files={
            "metadata": (None, json.dumps(metadata), "application/json"),
            "worker.js": ("worker.js", code, "application/javascript+module"),
        })
    _check(r, "upload du script")
    print(f"script {NAME} : uploadé ({len(code)} octets)")

    # 2. Secret : le PAT GitHub, sous le nom que le Worker lit.
    r = requests.put(f"{BASE}/secrets", headers=HEAD, timeout=30,
                     json={"name": "WATCHDOG_PAT", "text": PAT, "type": "secret_text"})
    _check(r, "pose du secret WATCHDOG_PAT")
    print("secret WATCHDOG_PAT : posé")

    # 3. Cron.
    r = requests.put(f"{BASE}/schedules", headers=HEAD, timeout=30,
                     json=[{"cron": CRON}])
    _check(r, "pose du cron")

    # 4. Relecture de contrôle — un upload qui « passe » ne prouve pas le cron.
    r = requests.get(f"{BASE}/schedules", headers=HEAD, timeout=30)
    body = _check(r, "relecture des crons")
    crons = [s.get("cron") for s in body.get("result", {}).get("schedules", [])]
    if CRON not in crons:
        _die(f"cron absent après pose : {crons!r}")
    print(f"cron : {crons} — OK")
    print("Déployé. Premier passage dans les 10 minutes ; les logs se lisent "
          "dans le dashboard Cloudflare (Workers → predator-watchdog → Logs).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
