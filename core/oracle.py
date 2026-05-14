"""
core/oracle.py — Gemini 2.0 Flash + Google Search to find Pinnacle fair price.
Uses raw HTTP (no SDK dependency).
"""
import os
import re
import requests

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"


def get_pinnacle_price(match_name: str, api_key: str = None) -> float | None:
    """
    Ask Gemini (with Google Search grounding) for Pinnacle's current price.
    Returns the decimal odd of the favorite, or None if not found.
    """
    if not api_key:
        api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[Oracle] No GEMINI_API_KEY")
        return None

    prompt = (
        f"Use Google Search to find the current Pinnacle Sports betting odds for this match: {match_name}\n"
        f"Return ONLY valid JSON: {{\"price\": 1.85, \"team\": \"FavoriteTeam\"}}\n"
        f"If not found: {{\"price\": null}}"
    )

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 128},
    }

    try:
        import time
        for attempt in range(2):
            r = requests.post(f"{GEMINI_URL}?key={api_key}", json=payload, timeout=20)
            if r.status_code == 429:
                print(f"[Oracle] Rate limit for {match_name} — waiting 65s")
                time.sleep(65)
                continue
            break
        if r.status_code != 200:
            print(f"[Oracle] Gemini error {r.status_code} for {match_name}")
            return None

        parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = next((p["text"] for p in reversed(parts) if p.get("text", "").strip()), "")
        text = re.sub(r'```(?:json)?|```', '', text)

        # Try JSON price field first
        m = re.search(r'"price"\s*:\s*(\d+\.\d+)', text)
        if m:
            return float(m.group(1))

        # Fallback: first plausible decimal number
        nums = re.findall(r'\b(\d+\.\d{2})\b', text)
        valid = [float(n) for n in nums if 1.05 < float(n) < 20.0]
        return valid[0] if valid else None

    except Exception as e:
        print(f"[Oracle] Error for {match_name}: {e}")
        return None
