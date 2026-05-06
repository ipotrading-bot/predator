import asyncio
from signals.scanner import MarketScanner
import logging

logging.basicConfig(level=logging.INFO)

async def main():
    scanner = MarketScanner()
    print("Starting MarketScanner...")
    result = await scanner.run_scan()
    print(f"Scan complete: {result}")

if __name__ == "__main__":
    asyncio.run(main())
