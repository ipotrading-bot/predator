"""
run_audit.py — PREDATOR PAIM v8.5 — Audit entry point
Triggered by .github/workflows/audit.yml every 6h.
"""
from core.ai_search import prioriser_settlement
from core.audit_engine import run

if __name__ == "__main__":
    # Ce process règle des signaux : il a le droit d'entamer la réserve de
    # crédits de recherche que les scans ne peuvent pas toucher. Un scan de
    # plus vaut moins qu'un résultat de moins (core/ai_search).
    prioriser_settlement()
    run()
