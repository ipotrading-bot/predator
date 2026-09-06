"""
tests/test_audit_cadence.py — la cadence de l'audit vit UNE fois dans le code
(`core.constants.AUDIT_INTERVAL_H`) et doit valoir ce que le cron tire.

Pourquoi (2026-09-06) : le 05/09 l'audit est passé de 6 h à 3 h. La fenêtre
TheSportsDB (`TSDB_RETRY_WINDOW_H`) a suivi à la main ; le lot de la relance
des expirés (12 par run, « 12 × 4 audits/jour = 48 ») non — 96 relances par
jour, TheSportsDB 150/150 à 14:41. Deux nombres qui se multiplient par la
cadence, un seul recalé : c'est la panne « listes qui divergent » (règle 6).
Depuis, ce qui dépend de la cadence se DÉRIVE de la constante, et ce test
compare la constante au cron réel. La doc opérateur est comparée de même.
"""
from __future__ import annotations

import re
from pathlib import Path

from core import relance_expires
from core.constants import AUDIT_INTERVAL_H

ROOT = Path(__file__).resolve().parent.parent
AUDIT_YML = ROOT / ".github" / "workflows" / "audit.yml"
DOC_OPERATEUR = ROOT / "docs" / "systeme_de_scan.md"


def _cron_audit() -> str:
    crons = re.findall(r"-\s*cron:\s*['\"]([^'\"]+)['\"]",
                       AUDIT_YML.read_text(encoding="utf-8"))
    assert len(crons) == 1, f"{len(crons)} crons dans audit.yml — ce test suppose l'unicité"
    return crons[0]


def test_la_constante_vaut_la_cadence_du_cron():
    minute, heure = _cron_audit().split()[:2]
    assert heure.startswith("*/"), (
        f"cron `{_cron_audit()}` : ce test ne lit que la forme `*/N` (cadence "
        "régulière, exigée aussi par le chien de garde) — l'étendre en "
        "connaissance de cause")
    assert AUDIT_INTERVAL_H == int(heure[2:]), (
        f"AUDIT_INTERVAL_H = {AUDIT_INTERVAL_H} h, audit.yml tire `{_cron_audit()}` — "
        "recaler la constante, jamais ce qui en dérive")


def test_le_lot_de_relance_suit_la_cadence():
    """Lot × audits/jour ≤ relances/jour voulues, quelle que soit la cadence."""
    audits_par_jour = 24 // AUDIT_INTERVAL_H
    assert relance_expires.RELANCE_BUDGET * audits_par_jour <= relance_expires.RELANCES_PAR_JOUR + audits_par_jour - 1, (
        f"{relance_expires.RELANCE_BUDGET} × {audits_par_jour} audits dépasse les "
        f"{relance_expires.RELANCES_PAR_JOUR} relances/jour voulues")
    assert relance_expires.RELANCE_BUDGET >= 1


def test_la_doc_operateur_annonce_les_heures_du_cron():
    minute, heure = _cron_audit().split()[:2]
    pas = int(heure[2:])
    attendues = {f"{h:02d}:{int(minute):02d}" for h in range(0, 24, pas)}
    ligne = next((l for l in DOC_OPERATEUR.read_text(encoding="utf-8").splitlines()
                  if "`audit.yml`" in l and l.startswith("|")), None)
    assert ligne, "la ligne « Audit … (`audit.yml`) » a disparu du tableau « Quand (UTC) »"
    annoncees = set(re.findall(r"\b(\d{2}:\d{2})\b", ligne))
    assert annoncees == attendues, (
        f"docs/systeme_de_scan.md annonce {sorted(annoncees)}, le cron dit "
        f"{sorted(attendues)} ({_cron_audit()})")
    assert f"| {len(attendues)} |" in ligne, "la colonne « Par jour » ne suit pas"
