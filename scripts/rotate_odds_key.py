#!/usr/bin/env python3
"""
scripts/rotate_odds_key.py — pose une nouvelle clé OddsAPI en une commande.

    python scripts/rotate_odds_key.py <nouvelle_cle>
    python scripts/rotate_odds_key.py --show        # état actuel, sans écrire

Écrit dans la table Supabase `app_secrets`, qui est la source de vérité lue
par le moteur ET par le dashboard (core/secret_store.py). Aucun redéploiement
Vercel, aucun secret GitHub à toucher : la clé est prise en compte au
prochain appel.

La clé est VALIDÉE avant d'être stockée, via GET /v4/sports — le seul
endpoint OddsAPI qui ne consomme pas de crédit. Une clé morte ou mal
recopiée est donc refusée à l'écriture plutôt que de faire tomber le
pipeline en silence pendant des heures.

Prérequis : SUPABASE_URL et SUPABASE_SERVICE_KEY dans l'environnement (ou
.env). La table `app_secrets` n'est lisible/écrivable que par service_role —
RLS sans policy, voir sql/migrate_v10_1_app_secrets.sql.
"""
import sys

import requests
from dotenv import load_dotenv

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from core.db import get_db, MissingCredentialsError  # noqa: E402
from core.secret_store import TABLE, get_secret, invalidate  # noqa: E402

load_dotenv()

SPORTS_URL = "https://api.the-odds-api.com/v4/sports/"


def probe(key: str) -> tuple[bool, str]:
    """(ok, description) — ne consomme aucun crédit."""
    try:
        r = requests.get(SPORTS_URL, params={"apiKey": key}, timeout=15)
    except Exception as e:
        return False, f"appel impossible : {e}"
    if r.status_code in (401, 403):
        return False, f"HTTP {r.status_code} — clé invalide, révoquée ou quota épuisé"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    remaining = r.headers.get("x-requests-remaining", "?")
    used = r.headers.get("x-requests-used", "?")
    return True, f"HTTP 200 — restantes={remaining} utilisées={used}"


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    if args[0] == "--show":
        key = get_secret("ODDS_API_KEY", force_refresh=True)
        if not key:
            print("Aucune clé résolue (ni app_secrets, ni environnement).")
            return 1
        ok, desc = probe(key)
        print(f"Clé active …{key[-6:]} → {desc}")
        return 0 if ok else 1

    new_key = args[0].strip()
    if len(new_key) < 16:
        print(f"Refus : « {new_key} » ne ressemble pas à une clé OddsAPI.")
        return 1

    ok, desc = probe(new_key)
    print(f"Vérification de la nouvelle clé : {desc}")
    if not ok:
        print("Refus d'écrire une clé qui ne répond pas.")
        return 1

    try:
        sb = get_db(write=True)
    except MissingCredentialsError as e:
        print(f"Refus : {e}")
        return 1
    if sb is None:
        print("Refus : SUPABASE_URL absent de l'environnement.")
        return 1

    sb.table(TABLE).upsert(
        {"key": "ODDS_API_KEY", "value": new_key,
         "note": "rotation via scripts/rotate_odds_key.py"},
        on_conflict="key",
    ).execute()

    invalidate("ODDS_API_KEY")
    stored = get_secret("ODDS_API_KEY", env_fallback=False, force_refresh=True)
    if stored != new_key:
        print("ÉCHEC : relecture différente de ce qui a été écrit.")
        return 1
    print(f"OK — clé …{new_key[-6:]} posée dans {TABLE} et relue avec succès.")
    print("Le moteur et le dashboard la prendront au prochain appel "
          "(cache de 5 min max).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
