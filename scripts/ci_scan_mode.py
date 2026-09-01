"""
scripts/ci_scan_mode.py — cron qui a tiré → mode de scan → variables de run_engine.py.

Appelé par .github/workflows/scan.yml (step « Résoudre le mode ») à travers
scripts/ci_env.py --pool scan, donc avec les secrets Supabase du pool dans l'env.

    python scripts/ci_scan_mode.py            # écrit $GITHUB_ENV / $GITHUB_OUTPUT / $GITHUB_STEP_SUMMARY
    python scripts/ci_scan_mode.py --dry-run  # n'écrit rien, n'appelle pas Supabase, imprime la décision

Entrées (env, posées par le workflow — aucune n'est un secret) :
    GITHUB_EVENT_NAME   schedule | workflow_dispatch
    SCHEDULE            ${{ github.event.schedule }}  (vide hors schedule)
    INPUT_MODE          ${{ inputs.mode }}            (vide hors dispatch)
    INPUT_HOURS         ${{ inputs.hours_ahead }}     (vide = défaut du mode)

La table CRON_MODES est la SEULE correspondance cron → mode du dépôt :
tests/test_ci_env.py vérifie qu'elle est exactement l'ensemble des `cron` de
scan.yml. Ajouter un cron sans sa ligne ici fait échouer le test ET le run.
"""
from __future__ import annotations

import argparse
import os
import sys

# ── Cron → mode. Les minutes sont décalées entre elles et vis-à-vis de
# closing_line.yml (H+14/34/54) — voir les commentaires de scan.yml. ──
CRON_MODES = {
    "3 2,6,9,12,17,19,21,23 * * *": "standard",   # fenêtres favorables (core/scan_windows.py)
    "25 * * * *":                    "golden",     # T-120 min, horaire, + REPRICE + closing pass
    "33 5,17 * * *":                 "deep",       # MAX_MATCHES=100, quotas élargis, 24 h
    "47 9,21 * * *":                 "guerrilla",  # recherche web renforcée, 48 h
}
MODES = ("standard", "golden", "deep", "guerrilla", "reprice")

# Variables lues par run_engine.py (évaluées à l'import du module).
# Valeurs reportées telles quelles depuis engine/golden_hour/deep_scan/
# guerrilla.yml au 2026-08-26 — vérifiées une à une, aucune n'a été inventée.
# OddsAPI (Tier 1) RALLUMÉ le 2026-09-01, décision opérateur — clé posée dans
# app_secrets.ODDS_API_KEYS. Le défaut du module reste 0 (obsolescence du
# 2026-08-26, tests/test_oddsapi_obsolete.py) : c'est ICI, et seulement ici,
# que le flag est posé. GUERRILLA et REPRICE n'ont pas de Tier 1 par
# construction (run_engine.py le teste avant le flag). GOLDEN ne le porte pas
# NON PLUS, décision opérateur du 2026-09-01 : 24 ticks/jour qui repaient
# chaque ligue en fenêtre favorable auraient vidé une clé (500 crédits/mois)
# en 3-5 jours ; le golden garde sa fenêtre T-120 min sur les sources
# gratuites. Restent standard (8/jour) et deep (2/jour) : ~10 scans payants.
TIER1_ENV = {"ODDS_API": "1"}

MODE_ENV: dict[str, dict[str, str]] = {
    "standard": {**TIER1_ENV},
    "golden": {"GOLDEN_HOUR": "1"},
    # HOURS_AHEAD explicite : run_engine.py le lit avec un défaut de 24, mais
    # deep_scan.yml le posait déjà en clair — on ne change pas ce contrat.
    "deep": {**TIER1_ENV, "DEEP_SCAN": "1", "HOURS_AHEAD": "24"},
    # GUERRILLA ne pose PAS HOURS_AHEAD : son horizon de 48 h vient du code
    # (run_engine.py, branche `elif GUERRILLA`), pas du workflow. L'y écrire
    # ferait diverger deux sources de vérité pour la même valeur.
    "guerrilla": {
        "GUERRILLA": "1",
        "SEARCH_MAX_TOKENS": "2500",       # défaut 2048 — MMA / Pinnacle
        "PINNACLE_BATCH": "25",            # au-delà, le prompt pousse au 413
        "PINNACLE_TAVILY_QUERIES": "6",    # défaut 4
        "TAVILY_RUN_BUDGET": "40",         # défaut 25
        # MAX_ORACLE RETIRÉ le 2026-08-27. Il valait "3", reporté tel quel
        # depuis guerrilla.yml. Le laisser aurait annulé le passage à zéro du
        # défaut (core.oracle.MAX_ORACLE_DEFAULT) pour le SEUL mode qui en
        # abuse le plus : une constante mise à zéro d'un côté et rétablie de
        # l'autre est exactement la divergence de listes que ce dépôt paie le
        # plus cher. Le reste du renforcement guerrilla (budget Tavily, taille
        # de lot, tokens) est conservé : il porte la recherche GROUPÉE, que A4
        # ne touche pas.
        "CACHE_MMA_TTL_H": "4",            # défaut 8
        # eSports a été RETIRÉ du périmètre le 2026-08-22 (RETIRED_SPORTS) :
        # cette variable est morte, elle l'était déjà dans guerrilla.yml. On
        # la reporte à l'identique plutôt que de la retirer en douce dans un
        # commit qui parle de CI.
        "CACHE_ESPORTS_TTL_H": "4",        # défaut 8
        "CACHE_ALT_TTL_H": "1.5",          # défaut 4 — slate ITTF tourne en ~1 h
        "CACHE_EMPTY_TTL_H": "1.5",        # défaut 3 — un vide dû à une clé morte
    },
    # REPRICE=1 est posé par le step lui-même (scan.yml), pas ici : ce mode
    # ne lance pas de scan complet.
    "reprice": {},
}


def resolve(event_name: str, schedule: str, input_mode: str) -> str:
    """Le mode demandé par le déclencheur. Lève ValueError sur un cron inconnu."""
    if event_name == "workflow_dispatch":
        if input_mode not in MODES:
            raise ValueError(f"mode inconnu : {input_mode!r} (attendu : {', '.join(MODES)})")
        return input_mode
    try:
        return CRON_MODES[schedule]
    except KeyError:
        raise ValueError(f"cron non répertorié : {schedule!r} — ajouter sa ligne dans "
                         "scripts/ci_scan_mode.py::CRON_MODES") from None


def promote(mode: str, manual: bool) -> str:
    """Bouton « Scanner » du dashboard : un tick golden (fenêtre 2 h) est promu
    en scan complet ; les autres modes sont déjà des scans complets.

    Le flag est désormais lu par les 36 ticks (contre 24 avant, golden seuls) :
    un clic tombant sur un tick standard/deep/guerrilla est donc consommé sans
    promotion — mais ces modes SONT déjà des scans complets, le clic obtient
    bien un scan. Seule la « saveur » varie. Ne pas ajouter de poller dédié
    pour rendre la promotion systématique : c'est l'erreur du 2026-07-07."""
    return "guerrilla" if (manual and mode == "golden") else mode


def env_for(mode: str, hours_ahead: str = "") -> dict[str, str]:
    env = dict(MODE_ENV[mode])
    if hours_ahead:
        env["HOURS_AHEAD"] = hours_ahead
    return env


def consume_manual_flag() -> bool:
    """Lit meta.scan_request (clé anon, SELECT autorisé) et l'efface avec la
    clé service_role (RLS bloque le DELETE anon — jusqu'au 2026-07-07 le flag
    pouvait rester et re-déclencher à chaque tick). Jamais bloquant : une
    erreur réseau = pas de demande manuelle."""
    import requests  # dépendance du projet, importée tard pour --dry-run

    url = os.environ.get("SUPABASE_URL", "")
    anon = os.environ.get("SUPABASE_KEY", "")
    service = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not (url and anon):
        return False
    try:
        r = requests.get(f"{url}/rest/v1/meta", params={"key": "eq.scan_request", "select": "value"},
                         headers={"apikey": anon, "Authorization": f"Bearer {anon}"}, timeout=10)
        r.raise_for_status()
        if not r.json():
            return False
    except Exception as e:  # noqa: BLE001
        print(f"::notice::Lecture du flag scan_request impossible ({type(e).__name__}) — ignoré")
        return False
    try:
        d = requests.delete(f"{url}/rest/v1/meta", params={"key": "eq.scan_request"},
                            headers={"apikey": service, "Authorization": f"Bearer {service}"}, timeout=10)
        d.raise_for_status()
    except Exception as e:  # noqa: BLE001
        print(f"::warning::DELETE du flag scan_request échoué ({type(e).__name__}) — "
              "il peut re-déclencher au prochain tick")
    return True


def _append(path_var: str, text: str) -> None:
    path = os.environ.get(path_var)
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    event = os.environ.get("GITHUB_EVENT_NAME", "schedule")
    try:
        mode = resolve(event, os.environ.get("SCHEDULE", ""), os.environ.get("INPUT_MODE", ""))
    except ValueError as e:
        print(f"::error::{e}")
        return 1
    manual = False if args.dry_run else consume_manual_flag()
    final = promote(mode, manual)
    env = env_for(final, os.environ.get("INPUT_HOURS", ""))

    line = f"mode={final} (demandé={mode}, manual={manual}, event={event}) env={sorted(env)}"
    print(line)
    if args.dry_run:
        return 0
    _append("GITHUB_ENV", "".join(f"{k}={v}\n" for k, v in env.items()))
    _append("GITHUB_OUTPUT", f"mode={final}\nmanual={str(manual).lower()}\n")
    _append("GITHUB_STEP_SUMMARY", f"### Scan `{final}` — {line}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
