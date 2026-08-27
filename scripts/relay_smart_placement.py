#!/usr/bin/env python3
"""
scripts/relay_smart_placement.py — sortir le relais des colos américains.

LE BLOCAGE, TEL QU'IL EST ÉTABLI
--------------------------------
odds500 n'est pas en panne, elle est filtrée par IP. Tranché le 2026-08-26
(run engine 32994959190) : depuis un runner GitHub, le relais rend « 403 de
l'AMONT via le relais (colo Cloudflare IAD) ». Un Worker s'exécute au colo le
plus proche de l'APPELANT — Londres depuis un poste de dev, où 500.com répond
200 ; Washington depuis les runners GitHub, où 500.com REFUSE l'IP de sortie.
Ce n'est ni le jeton, ni le code, ni la liste blanche : `net.describe_failure`
le dit en clair, un 403 AVEC `X-Relay-By` vient de l'amont, et il nomme le colo.

CE QUE CE SCRIPT TENTE, ET CE QU'IL NE PROMET PAS
-------------------------------------------------
Le Smart Placement de Cloudflare inverse la règle : au lieu de s'exécuter près
de l'appelant, le Worker s'exécute près de l'ORIGINE qu'il appelle. Vérifié le
2026-08-27, il n'a jamais été activé sur `predator-relay` — l'endpoint
`settings` rend `placement: {}`. C'est le seul levier de CODE jamais essayé
sur ce blocage, et il est gratuit.

⚠️ CE N'EST PAS UNE GARANTIE, et il ne faut pas le vendre comme telle.
Cloudflare optimise la LATENCE, pas la géographie : il choisit lui-même le
colo, a besoin de trafic pour apprendre, et rien ne dit qu'il retiendra un
colo dont 500.com accepte l'IP de sortie. Si le 403 persiste avec un colo
toujours américain, la conclusion d'INCIDENTS.md tient sans changement : il
faut une sortie hors des colos US (relais épinglé en Europe sur Fly.io/Render,
proxy à IP dédiée, ou runner auto-hébergé). Ce script ferme une hypothèse
gratuite avant de payer pour une infrastructure.

VÉRIFICATION APRÈS COUP — la seule qui tranche
-----------------------------------------------
Un `placement.mode = smart` posé ne prouve RIEN sur le résultat : c'est le
même piège que le sous-domaine workers.dev désactivé, où le Worker était
listé mais rendait 404 sur tout. Ce qui tranche est un run DEPUIS UN RUNNER
GitHub, et le message d'erreur nomme le colo :

    gh workflow run scan.yml
    gh run view <id> --log | grep odds500

Credentials (`.env` à la racine, ou l'environnement) :
    CLOUDFLARE_API_TOKEN — il lui faut « Account | Workers Scripts | Edit ».
      Le jeton de ce dépôt a été trouvé en LECTURE SEULE le 2026-08-27 : il
      liste les Workers mais rend 403 en écriture. Un 403 ici ne veut donc pas
      dire « jeton expiré ».
    CLOUDFLARE_ACCOUNT_ID
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

NAME = "predator-relay"
TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")
ACCOUNT = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
BASE = (f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}"
        f"/workers/scripts/{NAME}/settings")
HEAD = {"Authorization": f"Bearer {TOKEN}"}


def _die(msg: str) -> None:
    print(f"ERREUR : {msg}", file=sys.stderr)
    sys.exit(1)


def _settings() -> dict:
    r = requests.get(BASE, headers=HEAD, timeout=20)
    body = r.json() if r.content else {}
    if not (r.ok and body.get("success")):
        _die(f"lecture des réglages : HTTP {r.status_code} "
             f"{json.dumps(body.get('errors', body))[:300]}")
    return body.get("result") or {}


def main() -> int:
    for var, val in (("CLOUDFLARE_API_TOKEN", TOKEN),
                     ("CLOUDFLARE_ACCOUNT_ID", ACCOUNT)):
        if not val:
            _die(f"{var} absente (.env à la racine ou environnement).")

    avant = _settings()
    mode = (avant.get("placement") or {}).get("mode")
    print(f"{NAME} — placement actuel : {mode or '(aucun — colo de l’APPELANT)'}")
    if mode == "smart":
        print("Déjà activé. Si odds500 rend toujours 403, c'est que Cloudflare "
              "n'a pas retenu de colo acceptable : voir INCIDENTS.md, il faut "
              "une sortie hors des colos US.")
        return 0

    if "--oui" not in sys.argv:
        print("\nRelancer avec --oui pour activer le Smart Placement.")
        print("Réversible : ce même script avec --annuler.")
        return 0

    # Les réglages existants sont RENVOYÉS tels quels : un PATCH qui ne
    # reposerait que `placement` effacerait les bindings du Worker — dont
    # RELAY_TOKEN, dont la valeur est ILLISIBLE une fois posée. La perdre
    # obligerait à faire tourner le jeton des deux côtés.
    payload = dict(avant)
    payload["placement"] = {"mode": "smart"}
    # L'endpoint `settings` n'accepte QUE du multipart/form-data : un PATCH
    # JSON rend 415 « Content-Type must be one of: multipart/form-data ».
    r = requests.patch(BASE, headers=HEAD,
                       files={"settings": (None, json.dumps(payload))}, timeout=30)
    body = r.json() if r.content else {}
    if not (r.ok and body.get("success")):
        if r.status_code == 403:
            _die("HTTP 403 — le jeton Cloudflare est en LECTURE SEULE sur les "
                 "Workers. Il lui faut « Account | Workers Scripts | Edit » "
                 "(https://dash.cloudflare.com/profile/api-tokens, modèle "
                 "« Edit Cloudflare Workers »), puis rejouer ce script.")
        _die(f"écriture : HTTP {r.status_code} "
             f"{json.dumps(body.get('errors', body))[:300]}")

    apres = (_settings().get("placement") or {}).get("mode")
    print(f"placement : {apres}")
    print("\n⚠️ Rien n'est prouvé tant qu'un run n'a pas tourné DEPUIS UN "
          "RUNNER GitHub — le message d'erreur d'odds500 nomme le colo :")
    print("   gh workflow run scan.yml")
    print("   gh run view <id> --log | grep odds500")
    return 0


if __name__ == "__main__":
    if "--annuler" in sys.argv:
        r = requests.patch(
            BASE, headers=HEAD,
            files={"settings": (None, json.dumps(
                {**_settings(), "placement": {"mode": "off"}}))},
            timeout=30)
        print("annulé" if r.ok else f"HTTP {r.status_code} {r.text[:200]}")
        sys.exit(0)
    sys.exit(main())
