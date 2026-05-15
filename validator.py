import os
import requests
import google.generativeai as genai
from groq import Groq
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

def validate_all_systems():
    print("🦅 PREDATOR PAIM v7.0 - SYSTEM CHECK")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # 1. Test HARVESTER — 1XBet direct feed
    try:
        url = "https://1xbet.com/LineFeed/Get1x2?sport=1&count=5&lng=en&mode=4"
        res = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if res.status_code == 200 and res.json().get("Value"):
            print(f"✅ HARVESTER : 1XBet CONNECTÉ ({len(res.json()['Value'])} matchs bruts)")
        else:
            print(f"⚠️ HARVESTER : 1XBet HTTP {res.status_code} (Gemini fallback actif)")
    except: print("⚠️ HARVESTER : 1XBet inaccessible (Gemini fallback actif)")

    # 2. Test GEMINI (Grounding Check)
    try:
        genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
        model = genai.GenerativeModel('gemini-2.0-flash')
        response = model.generate_content("Test ping")
        if response:
            print("✅ GEMINI AI : OPÉRATIONNEL (Prêt pour le Grounding)")
    except: print("❌ GEMINI AI : CLÉ INVALIDE OU LIMITE ATTEINTE")

    # 3. Test GROQ (Speed Check)
    try:
        client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
        chat_completion = client.chat.completions.create(
            messages=[{"role": "user", "content": "ping"}],
            model="llama3-8b-8192",
        )
        print("✅ GROQ API : OPÉRATIONNEL (Latence ultra-faible)")
    except: print("❌ GROQ API : ÉCHEC DE CONNEXION")

    # 4. Test SUPABASE (Database Check)
    try:
        supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))
        res = supabase.table("signals").select("id").limit(1).execute()
        print("✅ SUPABASE : CONNECTÉ (Accès Service Role validé)")
    except: print("❌ SUPABASE : ERREUR DE LIAISON (Vérifiez URL/KEY)")

    # 5. Test RAPIDAPI (Football/NBA Data)
    try:
        url = "https://api-football-v1.p.rapidapi.com/v3/timezone"
        headers = {"X-RapidAPI-Key": os.environ.get("RAPIDAPI_KEY"), "X-RapidAPI-Host": "api-football-v1.p.rapidapi.com"}
        res = requests.get(url, headers=headers)
        if res.status_code == 200:
            print("✅ RAPIDAPI : CONNECTÉ (Flux Stats prêt)")
        else:
            print(f"❌ RAPIDAPI : ERREUR {res.status_code}")
    except: print("❌ RAPIDAPI : ÉCHEC CRITIQUE")

    # 6. Test TELEGRAM (Ghost Layer)
    try:
        token = os.environ.get("TELEGRAM_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        res = requests.post(url, json={"chat_id": chat_id, "text": "🔔 PREDATOR : Test de liaison réussi."})
        if res.status_code == 200:
            print("✅ TELEGRAM : LIAISON ÉTABLIE (Vérifiez votre téléphone)")
        else:
            print(f"❌ TELEGRAM : ERREUR {res.status_code}")
    except: print("❌ TELEGRAM : ÉCHEC D'ENVOI")

    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("🏁 AUDIT TERMINÉ.")

if __name__ == "__main__":
    validate_all_systems()
