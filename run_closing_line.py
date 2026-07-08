"""
run_closing_line.py — PREDATOR PAIM v9.5 — Real closing-line capture entry point.
Triggered by .github/workflows/closing_line.yml every hour — independent of
run_audit.py's 6h settlement/CLV cadence (Task 3: kickoff ± 5min rarely
lines up with a 6h window).
"""
from core.audit_engine import run_closing_lines

if __name__ == "__main__":
    run_closing_lines()
