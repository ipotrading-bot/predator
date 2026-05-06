import asyncio
import logging
import os
from dotenv import load_dotenv

# Load .env explicitly
load_dotenv()

from signals.scanner import MarketScanner

# Configuration du logging pour mieux voir ce qui se passe
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("test_pipeline")

async def test_pipeline():
    logger.info("🚀 Initialisation du test de stabilité du pipeline...")
    
    # Validation basique des configs
    if not os.environ.get("GEMINI_API_KEY"):
        logger.error("❌ GEMINI_API_KEY manquante!")
        # Print environment to debug
        logger.debug(f"Env: {os.environ}")
        return
        
    try:
        scanner = MarketScanner()
        logger.info("✅ MarketScanner initialisé.")
        
        logger.info("📡 Lancement du scan...")
        result = await scanner.run_scan()
        
        logger.info("🎉 Scan terminé avec succès!")
        logger.info(f"Résultats: {result.events_analyzed} événements, {result.signals_validated} signaux validés.")
        
    except Exception as e:
        logger.error(f"💥 Le pipeline a crashé: {e}", exc_info=True)
    finally:
        logger.info("🏁 Fin du test.")

if __name__ == "__main__":
    asyncio.run(test_pipeline())
