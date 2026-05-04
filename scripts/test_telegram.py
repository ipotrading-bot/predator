"""
scripts/test_telegram.py — Test d'envoi Telegram pour PREDATOR PAIM
Vérifie la connectivité avec l'API Telegram et envoie un message de test.
"""
import os
import requests
from dotenv import load_dotenv

# Charger les variables d'environnement depuis .env
load_dotenv()

# Configuration (récupérée depuis vos secrets)
TOKEN = "8203266406:AAESLsNZKWU6cyydcU9fisnAI4oyoU7TFXw"
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def send_test_message():
    """Envoie un message de test pour valider la configuration Telegram."""
    if not CHAT_ID:
        print("❌ ERREUR : TELEGRAM_CHAT_ID non trouvé dans les variables d'environnement.")
        print("   Ajoutez TELEGRAM_CHAT_ID=votre_id dans votre fichier .env")
        return

    message = (
        "🦅 *SYSTÈME PREDATOR PAIM v2.0*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✅ **GHOST LAYER : OPÉRATIONNEL**\n"
        "📡 **FLUX DATA : CONNECTÉ**\n"
        "🧠 **MODÈLE : PRÊT POUR SCAN 7/9**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*En attente du prochain cycle d'Alpha...*"
    )
    
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }

    try:
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            print(f"✅ Message de test envoyé avec succès à l'ID : {CHAT_ID}")
            print("   Vérifiez votre Telegram pour confirmer la réception.")
        else:
            print(f"❌ Échec de l'envoi. Code : {response.status_code}")
            print(f"Réponse : {response.text}")
    except Exception as e:
        print(f"❌ Erreur système : {e}")


def send_signal_test():
    """Envoie un faux signal pour tester le format des notifications."""
    if not CHAT_ID:
        print("❌ TELEGRAM_CHAT_ID non configuré")
        return

    signal_message = (
        "🚨 *SIGNAL PAIM DÉTECTÉ*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🏀 *NBA: Lakers vs Nuggets*\n"
        "📊 *Marché:* h2h\n"
        "🎯 *Sélection:* Lakers ML\n"
        "📈 *EV+:* 9.4%\n"
        "🎲 *Probabilité:* 68.2%\n"
        "💰 *Mise:* 150€\n"
        "📱 *Bookmaker:* 1XBet\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*ID Signal:* test_12345*\n"
        "_Scan terminé en 12.3s_"
    )
    
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": signal_message,
        "parse_mode": "Markdown"
    }

    try:
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            print(f"✅ Signal de test envoyé avec succès!")
        else:
            print(f"❌ Échec: {response.status_code}")
    except Exception as e:
        print(f"❌ Erreur: {e}")


if __name__ == "__main__":
    print("🦅 PREDATOR PAIM - Test Telegram")
    print("=" * 40)
    print()
    
    # Test 1: Message de statut
    print("📡 Test 1: Message de statut...")
    send_test_message()
    print()
    
    # Test 2: Signal exemple
    print("🚨 Test 2: Signal exemple...")
    send_signal_test()
    print()
    
    print("=" * 40)
    print("✅ Tests terminés!")