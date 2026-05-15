"""
run_audit.py — PREDATOR PAIM v8.5 — Audit entry point
Triggered by .github/workflows/audit.yml every 6h.
"""
from core.audit_engine import run

if __name__ == "__main__":
    run()
