"""
scripts/run_once_scan.py — Scan one-shot pour GitHub Actions
Utilise le pipeline inline de api/scan.py (sans scipy/sklearn)
"""
import asyncio
import os
import sys

# Ensure repo root is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.scan import run_scan


async def main():
    print("🦅 Predator PAIM — Scan one-shot démarré")
    result = await run_scan()
    print(f"✅ Résultat: {result.body.decode()}")


if __name__ == "__main__":
    asyncio.run(main())
