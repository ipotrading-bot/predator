import os
import asyncio
import logging
from typing import Optional
from supabase import create_client, Client

logger = logging.getLogger(__name__)

# Queue pour le batch processing
signal_queue = asyncio.Queue()

# Lazy-loaded Supabase client — évite crash à l'import si pas d'env vars
_supabase_instance: Optional[Client] = None


def _get_supabase() -> Optional[Client]:
    """Retourne le client Supabase (lazy init). Retourne None si non configuré."""
    global _supabase_instance
    if _supabase_instance is None:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_KEY")
        if url and key:
            _supabase_instance = create_client(url, key)
        else:
            logger.warning("SUPABASE_URL ou SUPABASE_KEY manquant — DB non disponible")
    return _supabase_instance


# Compatibilité ascendante : certaines parties importent 'supabase' comme variable
def supabase():
    """Retourne le client Supabase. Appelable comme supabase.table(...)"""
    client = _get_supabase()
    if client is None:
        raise RuntimeError("Supabase non configuré. Vérifiez SUPABASE_URL et SUPABASE_KEY.")
    return client


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
            sb = _get_supabase()
            if sb:
                sb.table("signals").insert(batch).execute()
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
        sb = _get_supabase()
        if not sb:
            return None
        response = sb.table("signals").insert(payload).execute()
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
        sb = _get_supabase()
        if not sb:
            return None
        payload = {
            "result": outcome,
            "clv": closing_price,
            "status": "settled" if outcome is not None else "void"
        }
        response = sb.table("signals").update(payload).eq("id", signal_id).execute()
        return response
    except Exception as e:
        print(f"🚨 Erreur Settlement (PhD Audit) : {e}")
        return None
