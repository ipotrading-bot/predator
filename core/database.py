import os
import asyncio
from supabase import create_client, Client

# Initialisation avec la Service Role Key pour bypasser le RLS
url: str = os.environ.get("SUPABASE_URL")
key: str = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(url, key)

# Queue pour le batch processing
signal_queue = asyncio.Queue()

async def batch_processor():
    """Worker asynchrone pour traiter les inserts par lots."""
    while True:
        batch = []
        # Attendre le premier élément
        item = await signal_queue.get()
        batch.append(item)
        
        # Tenter de collecter jusqu'à 20 éléments supplémentaires sans bloquer
        while len(batch) < 20:
            try:
                item = signal_queue.get_nowait()
                batch.append(item)
            except asyncio.QueueEmpty:
                break
        
        # Ingestion en batch
        try:
            supabase.table("signals").insert(batch).execute()
        except Exception as e:
            print(f"Erreur d'insertion batch : {e}")
        finally:
            for _ in batch:
                signal_queue.task_done()

def enqueue_signal(payload: dict):
    """Met un signal en queue pour ingestion asynchrone."""
    signal_queue.put_nowait(payload)

def insert_signal(payload: dict):
    """
    Insère un signal d'arbitrage PAIM dans la table 'signals'.
    Le client utilise la service_role_key pour garantir l'écriture.
    """
    try:
        response = supabase.table("signals").insert(payload).execute()
        return response
    except Exception as e:
        print(f"Erreur d'insertion : {e}")
        return None

def update_signal_settlement(signal_id: str, outcome: int, closing_price: float):
    """
    Mise à jour post-match (Audit PAIM).
    signal_id: UUID
    outcome: 1 (Win), 0 (Loss), ou None (Void)
    closing_price: float (Cote de clôture Pinnacle)
    """
    try:
        # Préparation du payload aligné sur la DB réelle
        payload = {
            "result": outcome,
            "clv": closing_price, # On utilise 'clv' (float8) identifié en DB
            "status": "settled" if outcome is not None else "void"
        }
        
        # L'UUID doit être passé en string, Supabase gère le cast
        response = supabase.table("signals").update(payload).eq("id", signal_id).execute()
        return response
    except Exception as e:
        print(f"🚨 Erreur Settlement (PhD Audit) : {e}")
        return None
