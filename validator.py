"""
validator.py — PREDATOR PAIM — Manual system health-check
Run locally: python validator.py
NOT imported by the engine — standalone diagnostic tool only.
"""
import os
import requests
from dotenv import load_dotenv

from core.db import get_db
from core.secret_store import get_secret

load_dotenv()


def validate_all_systems():
    print("PREDATOR PAIM - SYSTEM CHECK")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # 1. HARVESTER — 1XBet direct feed
    try:
        url = "https://1xbet.com/LineFeed/Get1x2?sport=1&count=5&lng=en&mode=4"
        res = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if res.status_code == 200 and res.json().get("Value"):
            print(f"OK  HARVESTER : 1XBet CONNECTE ({len(res.json()['Value'])} matchs bruts)")
        else:
            print(f"WARN HARVESTER : 1XBet HTTP {res.status_code} (fallback recherche web actif)")
    except Exception:
        print("WARN HARVESTER : 1XBet inaccessible (fallback recherche web actif)")

    # 2. Sources de SCORES du settlement (Groq/Tavily supprimés le 2026-09-02 :
    # le settlement lit des API structurées, plus aucune recherche web).
    try:
        r = requests.get("https://statsapi.mlb.com/api/v1/schedule?sportId=1",
                         timeout=15, headers={"User-Agent": "PREDATOR/1.0"})
        print("OK  MLB STATSAPI : OPERATIONNEL" if r.status_code == 200
              else f"ERR MLB STATSAPI : HTTP {r.status_code}")
    except Exception as e:
        print(f"ERR MLB STATSAPI : {e}")

    try:
        cle = os.environ.get("THESPORTSDB_API_KEY") or "123"
        r = requests.get(f"https://www.thesportsdb.com/api/v1/json/{cle}/searchteams.php?t=Arsenal",
                         timeout=15, headers={"User-Agent": "PREDATOR/1.0"})
        print("OK  THESPORTSDB : OPERATIONNEL" if r.status_code == 200
              else f"ERR THESPORTSDB : HTTP {r.status_code}")
    except Exception as e:
        print(f"ERR THESPORTSDB : {e}")

    # 3. SUPABASE
    try:
        supabase = get_db(write=False)
        if supabase is None:
            raise RuntimeError("SUPABASE_URL/SUPABASE_KEY not set")
        supabase.table("signals").select("id").limit(1).execute()
        print("OK  SUPABASE : CONNECTE")
    except Exception as e:
        print(f"ERR SUPABASE : {e}")

    # 4. ODDS API
    try:
        key = get_secret("ODDS_API_KEY")
        if key:
            r = requests.get(
                "https://api.the-odds-api.com/v4/sports/",
                params={"apiKey": key},
                timeout=10,
            )
            remaining = r.headers.get("x-requests-remaining", "?")
            print(f"OK  ODDS API : HTTP {r.status_code} | quota restant={remaining}")
        else:
            print("WARN ODDS API : ODDS_API_KEY absente de app_secrets ET de l'env")
    except Exception as e:
        print(f"ERR ODDS API : {e}")

    # 5. TELEGRAM
    try:
        token   = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if token and chat_id:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            res = requests.post(
                url,
                json={"chat_id": chat_id, "text": "PREDATOR : Test de liaison."},
                timeout=10,
            )
            if res.status_code == 200:
                print("OK  TELEGRAM : LIAISON ETABLIE")
            else:
                print(f"ERR TELEGRAM : HTTP {res.status_code}")
        else:
            print("WARN TELEGRAM : TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant")
    except Exception as e:
        print(f"ERR TELEGRAM : {e}")

    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("AUDIT TERMINE.")


if __name__ == "__main__":
    validate_all_systems()
