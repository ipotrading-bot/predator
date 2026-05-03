import sys
import os
# Ajouter le répertoire racine au PYTHONPATH
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
from signals.scanner import MarketScanner
from config import settings

async def run():
    print("Starting one-off scan...")
    scanner = MarketScanner(bankroll=settings.starting_bankroll)
    result = await scanner.run_scan()
    print(f"Scan finished. Signals validated: {result.signals_validated}")

if __name__ == "__main__":
    asyncio.run(run())
