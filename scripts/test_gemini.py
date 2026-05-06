import sys
import os
from dotenv import load_dotenv

# Load .env
load_dotenv()

# Add current directory to path
sys.path.append(os.getcwd())

from api.index import gemini_risk_check

def test_gemini_risk_check():
    print("\n--- Testing gemini_risk_check ---")
    try:
        # A scenario that definitely needs searching
        result = gemini_risk_check("Real Madrid vs Bayern Munich (injury concerns)", "Moneyline")
        print(f"Result: {result}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_gemini_risk_check()
