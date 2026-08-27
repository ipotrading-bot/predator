"""
core/oracle.py — PAIM v7.6 — Recherche web (Groq/Tavily) → Sharp fair price

⚠️ INERTE PAR DÉFAUT DEPUIS LE 2026-08-27 : `MAX_ORACLE_DEFAULT = 0`. Voir le
commentaire de cette constante pour la raison et pour ce qu'elle ne couvre pas.
Multi-source: Pinnacle → Betfair Exchange → Circa Sports
Non-Pinnacle sources receive a 0.5% reference price penalty (conservative edge).
Returns (price: float | None, team_name: str | None)

2026-07-21 : Gemini remplacé par core/ai_search.py (groq/compound-mini +
fallback Tavily) — le grounding Gemini gratuit est mort (limit: 0 sans
facturation prépayée, vérifié sur 4 clés/projets indépendants).
"""
import logging
import re
from datetime import date as _date

from core.ai_search import ai_available, ai_search_complete
from core.math_engine import calc_dnb

log = logging.getLogger("PREDATOR.oracle")

# ── Budget de repêchage — À ZÉRO PAR DÉFAUT DEPUIS LE 2026-08-27 ─────────
#
# Ce module demande à un LLM de CHERCHER sur le web « la cote Pinnacle » d'un
# match, puis le moteur traite la valeur rendue comme sa RÉFÉRENCE SHARP :
# c'est elle qui fixe `sharp_prob`, donc l'edge, donc la mise. Or rien dans la
# chaîne ne garantit qu'un tel prix ait jamais été affiché par Pinnacle. Un
# modèle de langage à qui l'on demande une cote en rend une ; à l'échelle du
# centième — celle qui décide de l'edge — c'est une génération plausible, pas
# une observation. Le moteur ne peut pas distinguer les deux : une cote
# inventée franchit exactement les mêmes gardes qu'une cote lue.
#
# La conséquence est asymétrique, donc pire qu'un simple bruit : un prix sharp
# SOUS-ESTIMÉ fabrique un edge là où il n'y en a pas, et ces signaux-là sont
# précisément ceux que le moteur émet. Un prix surestimé, lui, ne produit
# aucun signal et ne se voit jamais dans le ledger.
#
# `MAX_ORACLE=3` remet le repêchage en service — le code reste entier et
# fonctionnel. Il n'est pas supprimé parce qu'il redeviendrait légitime le
# jour où sa sortie serait recoupée avec un prix réellement observé.
#
# ⚠️ CE RÉGLAGE NE COUVRE PAS TOUT. Deux autres chemins font encore prixer un
# « sharp » par un LLM, et A4 ne les touche pas :
#   · `core.harvester.fetch_pinnacle_prices` — la recherche GROUPÉE, appelée
#     sur tout match sans prix sharp ; c'est le chemin dominant, MAX_ORACLE
#     n'a aucune prise dessus ;
#   · `core.audit_engine` — qui appelle `get_pinnacle_price` pour estimer la
#     ligne de CLÔTURE (`closing_source='oracle'`), sous son propre budget
#     `CLOSING_LINE_BUDGET`. Ce prix-là alimente le CLV, dont la couche
#     d'apprentissage fait un critère de premier rang.
MAX_ORACLE_DEFAULT = 0

# Fallback chain: (book_name, edge_penalty_pct)
# Penalty inflates the reference price → reduces effective edge → more conservative
_SHARP_BOOKS = [
    ("Pinnacle Sports",  0.0),   # Primary sharp — no penalty
    ("Betfair Exchange", 0.5),   # Sharp exchange — 0.5% conservative penalty
    ("Circa Sports",     0.5),   # Sharp US book — 0.5% penalty
]


def get_pinnacle_price(
    match_name: str,
    sport: str = "soccer",
    api_key: str = None,
    league: str = "",
    match_date: str = "",
) -> tuple[float | None, str | None]:
    """
    Returns (sharp_reference_price, favorite_team_name).
    Tries Pinnacle → Betfair Exchange → Circa Sports in order.
    Non-Pinnacle prices are inflated by penalty% (reduces effective edge shown to engine).
    Both may be None on complete failure.

    `api_key` est ignoré depuis la migration Groq/Tavily (gardé pour
    compatibilité avec les callers existants — audit_engine passait
    l'ancienne clé GEMINI_API_KEY_AUDIT ici).
    """
    if not ai_available():
        log.error("No GROQ_API_KEY — oracle indisponible")
        return None, None

    if not match_date:
        match_date = _date.today().isoformat()

    context = match_name
    if league:
        context += f" ({league})"
    context += f" — {match_date}"

    for book_name, penalty in _SHARP_BOOKS:
        price, team = _query_book(context, sport, book_name)
        if price and price > 1.01:
            if penalty > 0:
                price = round(price * (1 + penalty / 100), 4)
                log.info("%s fallback for %s (+%.1f%% penalty)", book_name, match_name, penalty)
            return price, team

    return None, None


def _query_book(
    context: str,
    sport: str,
    book_name: str,
) -> tuple[float | None, str | None]:
    """Un appel recherche+LLM pour un sportsbook. Returns (dnb_price, team) or (None, None)."""
    if sport == "soccer":
        prompt = (
            f"Search the web to find {book_name} current 1X2 odds for this soccer match:\n"
            f"{context}\n"
            f"Include both team names. Return ONLY valid JSON:\n"
            f'{{"home_team":"PSG","home":1.60,"draw":3.80,"away_team":"Lyon","away":9.00}}\n'
            f'If not found on {book_name}: {{"home":null}}'
        )
    else:
        sport_ctx = {"tennis": "tennis", "basketball": "NBA basketball"}.get(sport, sport)
        prompt = (
            f"Search the web to find {book_name} current Moneyline odds for this {sport_ctx}:\n"
            f"{context}\n"
            f"Return ONLY valid JSON with favorite decimal odd and team name:\n"
            f'{{"price":1.85,"team":"FavoriteTeam"}}\n'
            f'If not found on {book_name}: {{"price":null}}'
        )

    text = ai_search_complete(
        prompt,
        queries=[f"{book_name} odds {context}"],
        label=f"Oracle/{book_name}",
        max_tokens=300, temperature=0.1, timeout=45,
    )
    if not text:
        return None, None
    text = re.sub(r'```(?:json)?|```', '', text)

    if sport == "soccer":
        return _parse_soccer(text)
    return _parse_moneyline(text)


def _parse_soccer(text: str) -> tuple[float | None, str | None]:
    """Extract 1X2 odds + team names → compute AH 0.0 for the favorite."""
    # `\d+(?:\.\d+)?` et non `\d+\.\d+` : une cote ronde est très souvent
    # sérialisée sans décimale par le modèle (`"draw": 3`, `"away": 12`).
    # L'ancien motif exigeait le point et rendait alors (None, None) — un
    # prix sharp perdu en silence sur le chemin du settlement, pour la seule
    # raison que la cote tombait juste.
    m_ht = re.search(r'"home_team"\s*:\s*"([^"]+)"', text)
    m_at = re.search(r'"away_team"\s*:\s*"([^"]+)"', text)
    m_h  = re.search(r'"home"\s*:\s*(\d+(?:\.\d+)?)', text)
    m_d  = re.search(r'"draw"\s*:\s*(\d+(?:\.\d+)?)', text)
    m_a  = re.search(r'"away"\s*:\s*(\d+(?:\.\d+)?)', text)

    if m_h and m_d and m_a:
        home_odd = float(m_h.group(1))
        draw_odd = float(m_d.group(1))
        away_odd = float(m_a.group(1))
        dnb_h = calc_dnb(home_odd, away_odd, draw_odd)
        dnb_a = calc_dnb(away_odd, home_odd, draw_odd)

        home_name = m_ht.group(1) if m_ht else ""
        away_name = m_at.group(1) if m_at else ""

        # Use raw odds to identify favorite (not DNB which could still fail)
        if home_odd <= away_odd and dnb_h > 1.01:
            return (dnb_h, home_name) if home_name else (None, None)
        elif dnb_a > 1.01:
            return (dnb_a, away_name) if away_name else (None, None)

    return None, None


def _parse_moneyline(text: str) -> tuple[float | None, str | None]:
    """Extract the sharp Moneyline price and team name."""
    # Idem : `"price": 2` est une cote valide de 2.00 (voir _parse_soccer).
    m_p = re.search(r'"price"\s*:\s*(\d+(?:\.\d+)?)', text)
    m_t = re.search(r'"team"\s*:\s*"([^"]+)"', text)
    price = float(m_p.group(1)) if m_p else None
    team  = m_t.group(1) if m_t else None
    if price and 1.05 < price < 20.0:
        return price, team
    nums = re.findall(r'\b(\d+\.\d{2})\b', text)
    valid = [float(n) for n in nums if 1.05 < float(n) < 20.0]
    return (valid[0], None) if valid else (None, None)
