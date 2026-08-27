"""Le chien de garde Cloudflare (scripts/cloudflare_watchdog_worker.js) est
une COPIE de faits qui vivent ailleurs : les fichiers de workflows, leurs
crons, leurs inputs de dispatch. La règle du dépôt (CLAUDE.md §6, « listes
qui divergent ») exige qu'une copie soit comparée à sa source par un test.

C'est ce fichier. Il parse la table WATCH du Worker et vérifie :
  - chaque workflow surveillé existe et accepte workflow_dispatch ;
  - chaque seuil de rattrapage est STRICTEMENT au-dessus de la cadence
    nominale du cron le plus fréquent du workflow — le chien de garde ne
    peut jamais tirer plus vite que le schedule qu'il supplée ;
  - scan.yml n'est rattrapé qu'en mode `golden` (les autres modes dépensent
    les budgets journaliers des sources gratuites — les doubler casserait
    « l'arbitrage de cadence » du 2026-08-22) et ce mode existe bien dans
    scripts/ci_scan_mode.py ;
  - l'input de reports.yml est une option déclarée du workflow ;
  - aucun secret n'est écrit dans le JS (le PAT est un secret du Worker).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKER = (ROOT / "scripts" / "cloudflare_watchdog_worker.js").read_text(encoding="utf-8")
WORKFLOWS = ROOT / ".github" / "workflows"


def _watch_table() -> list[dict]:
    """La table WATCH du Worker, parsée sans moteur JS : une entrée par ligne."""
    entries = []
    for m in re.finditer(
            r'\{\s*file:\s*"([^"]+)",\s*stale_min:\s*(\d+),\s*inputs:\s*(\{[^}]*\})',
            WORKER):
        raw = re.sub(r'(\w+):', r'"\1":', m.group(3))
        entries.append({"file": m.group(1), "stale_min": int(m.group(2)),
                        "inputs": json.loads(raw)})
    assert entries, "table WATCH introuvable dans le Worker — le parseur ou le JS a changé"
    return entries


def _crons(yml: str) -> list[str]:
    return re.findall(r"-\s*cron:\s*['\"]([^'\"]+)['\"]", yml)


def _nominal_interval_min(cron: str) -> int:
    """Cadence nominale (minutes) des formes de cron utilisées par ce dépôt.

    Volontairement limité aux formes présentes : minutes en liste (a,b,c) =
    heure divisée ; minute fixe + heures */N ; minute fixe + liste d'heures ;
    hebdomadaire. Une forme nouvelle fait échouer le test — c'est voulu, il
    faudra étendre CE parseur en connaissance de cause.
    """
    minute, hour, dom, month, dow = cron.split()
    if "," in minute and hour == "*":
        return 60 // len(minute.split(","))
    if hour == "*":
        return 60
    if hour.startswith("*/"):
        return 60 * int(hour[2:])
    if "," in hour:
        hs = sorted(int(h) for h in hour.split(","))
        gaps = [b - a for a, b in zip(hs, hs[1:])] + [24 - hs[-1] + hs[0]]
        return 60 * min(gaps)
    return 7 * 24 * 60  # heure unique : au mieux quotidien/hebdomadaire


def test_chaque_workflow_surveille_existe_et_se_dispatche():
    for w in _watch_table():
        path = WORKFLOWS / w["file"]
        assert path.exists(), f"{w['file']} surveillé mais absent de .github/workflows/"
        assert "workflow_dispatch" in path.read_text(encoding="utf-8"), (
            f"{w['file']} ne porte pas workflow_dispatch : le rattrapage rendrait 422")


def test_les_seuils_restent_au_dessus_de_la_cadence_des_crons():
    for w in _watch_table():
        yml = (WORKFLOWS / w["file"]).read_text(encoding="utf-8")
        crons = _crons(yml)
        assert crons, f"{w['file']} n'a plus de cron — le surveiller n'a plus de sens"
        cadence = min(_nominal_interval_min(c) for c in crons)
        assert w["stale_min"] > cadence, (
            f"{w['file']} : seuil {w['stale_min']} min ≤ cadence nominale {cadence} min — "
            "le chien de garde tirerait plus vite que le schedule qu'il supplée")


def test_scan_nest_rattrape_quen_golden_et_le_mode_existe():
    scan = next(w for w in _watch_table() if w["file"] == "scan.yml")
    assert scan["inputs"] == {"mode": "golden"}, (
        "rattraper un autre mode que golden dépense les budgets journaliers "
        "des sources gratuites en DOUBLE du cron GitHub quand il finit par tirer")
    import importlib
    ci_scan_mode = importlib.import_module("scripts.ci_scan_mode")
    assert "golden" in ci_scan_mode.MODES


def test_linput_de_reports_est_une_option_declaree():
    rep = next(w for w in _watch_table() if w["file"] == "reports.yml")
    yml = (WORKFLOWS / "reports.yml").read_text(encoding="utf-8")
    m = re.search(r"options:\s*\[([^\]]+)\]", yml)
    assert m, "options du dispatch de reports.yml introuvables"
    options = [o.strip() for o in m.group(1).split(",")]
    assert rep["inputs"].get("report") in options


def test_aucun_secret_dans_le_js():
    assert "WATCHDOG_PAT" in WORKER, "le Worker doit lire le PAT depuis env.WATCHDOG_PAT"
    assert not re.search(r"gh[pousr]_[A-Za-z0-9]{20,}", WORKER), (
        "un jeton GitHub est écrit en clair dans le Worker")
