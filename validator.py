"""
validator.py — PREDATOR PAIM v8.5 — Manual system health-check
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
    print("PREDATOR PAIM v8.5 - SYSTEM CHECK")
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

    # 2. IA (Groq + Tavily — remplace Gemini depuis 2026-07-21)
    try:
        groq_key = os.environ.get("GROQ_API_KEY")
        if not groq_key:
            raise RuntimeError("GROQ_API_KEY not set")
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {groq_key}"},
            json={"model": "llama-3.3-70b-versatile",
                  "messages": [{"role": "user", "content": "ping"}],
                  "max_tokens": 5},
            timeout=15,
        )
        if r.status_code == 200:
            print("OK  GROQ AI : OPERATIONNEL")
        else:
            print(f"ERR GROQ AI : HTTP {r.status_code}")
    except Exception as e:
        print(f"ERR GROQ AI : {e}")

    try:
        tavily_key = os.environ.get("TAVILY_API_KEY")
        if not tavily_key:
            print("WARN TAVILY : TAVILY_API_KEY not set (fallback recherche désactivé)")
        else:
            r = requests.post("https://api.tavily.com/search",
                              json={"api_key": tavily_key, "query": "ping", "max_results": 1},
                              timeout=15)
            print("OK  TAVILY : OPERATIONNEL" if r.status_code == 200
                  else f"ERR TAVILY : HTTP {r.status_code}")
    except Exception as e:
        print(f"ERR TAVILY : {e}")

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
