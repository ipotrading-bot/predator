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


def test_la_fraicheur_ne_rattrape_scan_quen_reprice():
    """La voie FRAÎCHEUR reste gratuite. Le scan payant `standard` a sa propre
    voie (CRENEAUX) parce qu'un seuil de fraîcheur est FAUX pour lui : ses
    écarts vont de 2 h à 7 h, et tout seuil supérieur au plus petit écart
    tirerait dans le trou de nuit — aux heures que le recalage du 2026-09-03
    a précisément écartées."""
    scan = next(w for w in _watch_table() if w["file"] == "scan.yml")
    assert scan["inputs"] == {"mode": "reprice"}, (
        "la voie fraîcheur de scan.yml doit rester `reprice` : elle tire dès "
        "75 min de silence, ce qui pour un scan payant serait une dépense non "
        "bornée. Le rattrapage payant passe par CRENEAUX.")
    import importlib
    ci_scan_mode = importlib.import_module("scripts.ci_scan_mode")
    assert "reprice" in ci_scan_mode.MODES


def _creneaux_table() -> list[dict]:
    """La table CRENEAUX du Worker — même méthode que WATCH : on lit le JS,
    on ne l'exécute pas."""
    bloc = re.search(r"const CRENEAUX = \[(.*?)\n\];", WORKER, re.S)
    assert bloc, "table CRENEAUX introuvable dans le Worker"
    entries = []
    for m in re.finditer(
            r'\{\s*file:\s*"([^"]+)",\s*run_name:\s*"([^"]+)",\s*minute:\s*(\d+),\s*'
            r'hours:\s*\[([\d,\s]+)\],\s*grace_min:\s*(\d+),\s*inputs:\s*(\{[^}]*\})',
            bloc.group(1)):
        raw = re.sub(r'(\w+):', r'"\1":', m.group(6))
        entries.append({"file": m.group(1), "run_name": m.group(2),
                        "minute": int(m.group(3)),
                        "hours": [int(h) for h in m.group(4).split(",")],
                        "grace_min": int(m.group(5)),
                        "inputs": json.loads(raw)})
    assert entries, "CRENEAUX vide — le parseur ou le JS a changé"
    return entries


def _cron_du_mode(mode: str) -> str:
    import importlib
    crons = [c for c, m in
             importlib.import_module("scripts.ci_scan_mode").CRON_MODES.items()
             if m == mode]
    assert len(crons) == 1, f"{len(crons)} crons `{mode}` — ce test suppose l'unicité"
    return crons[0]


def _run_name_expr(fichier: str) -> str:
    """La valeur de `run-name:`, débarrassée de l'indicateur de bloc YAML
    (`>-`, `|`, …) et repliée sur une ligne — c'est la chaîne que GitHub
    évalue."""
    yml = (WORKFLOWS / fichier).read_text(encoding="utf-8")
    m = re.search(r"^run-name:\s*(.+?)^(?=\S)", yml, re.S | re.M)
    assert m, f"{fichier} n'a plus de run-name : le chien de garde deviendrait aveugle"
    valeur = re.sub(r"^[>|][-+]?\s*", "", m.group(1).strip())
    return " ".join(valeur.split())


def test_les_creneaux_surveilles_sont_exactement_ceux_du_cron():
    """L'angle mort du 2026-09-04 : la surveillance par FICHIER voyait scan.yml
    éternellement frais (les ticks reprice horaires, y compris ceux du chien de
    garde lui-même), donc un cron `standard` perdu était INDÉTECTABLE — pas
    seulement « non rattrapé ». CRENEAUX le rattrape, encore faut-il qu'il vise
    les bonnes heures : elles sont ici comparées au cron lui-même."""
    for c in _creneaux_table():
        minute, heures = _cron_du_mode(c["inputs"]["mode"]).split()[:2]
        assert c["minute"] == int(minute), (
            f"créneau {c['run_name']} à H+{c['minute']}, le cron dit H+{minute}")
        assert c["hours"] == sorted(int(h) for h in heures.split(",")), (
            f"créneau {c['run_name']} sur {c['hours']}, le cron dit {heures}")


def test_le_delai_de_grace_reste_sous_le_plus_petit_ecart():
    """Une grâce plus longue que l'écart minimal ferait chevaucher deux
    créneaux : on dispatcherait pour un créneau déjà remplacé par le suivant."""
    for c in _creneaux_table():
        hs = sorted(c["hours"])
        ecart = 60 * min([b - a for a, b in zip(hs, hs[1:])] + [24 - hs[-1] + hs[0]])
        assert 0 < c["grace_min"] < ecart, (
            f"grâce {c['grace_min']} min hors de ]0 ; {ecart}[ pour {c['run_name']}")


def test_le_run_name_surveille_est_bien_celui_que_le_workflow_produit():
    """Le chien de garde reconnaît son propre rattrapage par le run-name. Si
    scan.yml cessait de nommer ses runs, il ne verrait jamais son dispatch et
    redispatcherait à chaque passage — un scan PAYANT toutes les 10 min."""
    for c in _creneaux_table():
        expr = _run_name_expr(c["file"])
        prefixe, mode = c["run_name"].rsplit(" ", 1)
        assert expr.strip().startswith(prefixe), (
            f"run-name de {c['file']} ne commence pas par « {prefixe} »")
        assert "inputs.mode" in expr or f"'{mode}'" in expr, (
            f"run-name ne peut jamais valoir « {c['run_name']} »")


def test_les_crons_cites_par_le_run_name_existent_avec_ce_mode():
    """run-name déduit le mode d'un cron écrit EN DUR dans le YAML. Un cron
    recalé sans toucher cette expression nommerait tous les runs `standard` et
    le chien de garde croirait chaque créneau honoré (règle n°6)."""
    import importlib
    CRON_MODES = importlib.import_module("scripts.ci_scan_mode").CRON_MODES
    for c in _creneaux_table():
        cites = re.findall(
            r"github\.event\.schedule\s*==\s*'([^']+)'\s*&&\s*'([^']+)'",
            _run_name_expr(c["file"]))
        assert cites, (
            f"run-name de {c['file']} ne discrimine plus aucun cron : tous les "
            "runs porteraient le même nom")
        for cron, mode in cites:
            assert CRON_MODES.get(cron) == mode, (
                f"run-name de {c['file']} associe {cron!r} à {mode!r}, "
                f"CRON_MODES dit {CRON_MODES.get(cron)!r}")


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
