"""
scripts/ci_scan_mode.py — cron qui a tiré → mode de scan → variables de run_engine.py.

Appelé par .github/workflows/scan.yml (step « Résoudre le mode ») à travers
scripts/ci_env.py --pool scan, donc avec les secrets Supabase du pool dans l'env.

    python scripts/ci_scan_mode.py            # écrit $GITHUB_ENV / $GITHUB_OUTPUT / $GITHUB_STEP_SUMMARY
    python scripts/ci_scan_mode.py --dry-run  # n'écrit rien, n'appelle pas Supabase, imprime la décision
    python scripts/ci_scan_mode.py --note-slot  # marque le créneau servi (APRÈS un scan standard réussi)

Deux modes (2026-09-03) : `standard` (scan complet, 8×/jour) et `reprice`
(tick horaire gratuit). Le bouton « Scanner » promeut reprice → standard.

Un CRÉNEAU standard ne vaut qu'UN scan payant (2026-09-05) : quand le cron
GitHub, livré avec jusqu'à 2 h de retard, arrive derrière le rattrapage du
chien de garde, le second se dégrade en `reprice` au lieu de repayer. Voir le
bloc « UN CRÉNEAU = UN SCAN PAYANT » plus bas.

Entrées (env, posées par le workflow — aucune n'est un secret) :
    GITHUB_EVENT_NAME   schedule | workflow_dispatch
    SCHEDULE            ${{ github.event.schedule }}  (vide hors schedule)
    INPUT_MODE          ${{ inputs.mode }}            (vide hors dispatch)
    INPUT_HOURS         ${{ inputs.hours_ahead }}     (vide = défaut du mode)
    INPUT_FORCE         ${{ inputs.force }}           (« true » = payer même si
                        le créneau a déjà été servi ; le chien de garde ne le
                        pose jamais, seul un humain coche la case)
    SCAN_SLOT           créneau résolu, repassé au step de marquage

La table CRON_MODES est la SEULE correspondance cron → mode du dépôt :
tests/test_ci_env.py vérifie qu'elle est exactement l'ensemble des `cron` de
scan.yml. Ajouter un cron sans sa ligne ici fait échouer le test ET le run.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

# ── Cron → mode. DEUX modes depuis le 2026-09-03 (décision opérateur :
# « trop de workflows et de process ») — golden, deep et guerrilla sont
# supprimés, voir INCIDENTS.md « Cinq modes de scan, deux qui servent ».
# Les minutes sont décalées vis-à-vis de closing_line.yml (H+14/34/54). ──
CRON_MODES = {
    "3 6,9,11,13,16,19,21,23 * * *": "standard",  # scan complet 24 h, T-2h30..6h des coups d'envoi (core/scan_windows.py)
    "25 * * * *":                    "reprice",    # horaire, GRATUIT : slate soft en cache vs Matchbook
}
MODES = ("standard", "reprice")

# Variables lues par run_engine.py (évaluées à l'import du module).
# OddsAPI (Tier 1) RALLUMÉ le 2026-09-01, décision opérateur — clé posée dans
# app_secrets.ODDS_API_KEYS. Le défaut du module reste 0 (obsolescence du
# 2026-08-26, tests/test_oddsapi_obsolete.py) : c'est ICI, et seulement ici,
# que le flag est posé, pour `standard` seulement. REPRICE n'a pas de Tier 1
# par construction (run_engine.py le teste avant le flag, et son pool de
# secrets ne contient aucune clé payante). La dépense est bornée par le
# rythme mensuel (core/scan_windows), sur 8 ticks/jour.
TIER1_ENV = {"ODDS_API": "1"}

MODE_ENV: dict[str, dict[str, str]] = {
    "standard": {**TIER1_ENV},
    # REPRICE=1 vit ICI (table unique mode → variables, règle n°6), plus dans
    # le step YAML : jusqu'au 2026-09-03 le tick horaire portait DEUX
    # run_engine (scan golden puis REPRICE) et le step devait poser le flag
    # lui-même. Un seul run_engine par tick désormais.
    "reprice": {"REPRICE": "1"},
}


# ── UN CRÉNEAU `standard` = UN SCAN PAYANT (2026-09-05) ───────────────
# GitHub livre ses crons avec un retard mesuré jusqu'à ~2 h, et le chien de
# garde Cloudflare rattrape le créneau après 25 min de grâce : les deux
# arrivent alors pour le MÊME créneau, et le second REPAIE. Mesuré le
# 2026-09-05 : créneau 09:03 servi à 09:30 (16 crédits) puis à 10:08
# (27 crédits) ; le plafond du jour est tombé à 13:30 et les QUATRE créneaux
# suivants — 13:43, 16:30, 17:58, 19:30 — sont repartis avec 0 ligue payée,
# ceux qui portent le Big 5 du soir et la NFL/NBA.
# Le second run se DÉGRADE en `reprice` : gratuit, il re-tarife quand même le
# slate en cache et capte la closing line. Rien n'est supprimé en silence — la
# dégradation est loggée et résumée dans le run.
# Le créneau n'est marqué QU'APRÈS un scan réussi (`--note-slot`) : un scan qui
# échoue laisse son créneau à rattraper. Le `concurrency` de scan.yml sérialise
# les runs, donc le marquage précède toujours la résolution du suivant.
SLOT_META_KEY = "scan_standard_slot"


def standard_slots() -> tuple[int, list[int]]:
    """(minute, heures UTC) du cron `standard`, DÉRIVÉES de CRON_MODES —
    jamais réécrites ici (règle n°6)."""
    crons = [c for c, m in CRON_MODES.items() if m == "standard"]
    if len(crons) != 1:
        raise ValueError(f"{len(crons)} crons `standard` — cette fonction suppose "
                         "l'unicité, l'étendre en connaissance de cause")
    minute, heures = crons[0].split()[:2]
    return int(minute), sorted(int(h) for h in heures.split(","))


def due_slot(now: datetime) -> str:
    """Le dernier créneau `standard` DÛ à `now` (UTC), « AAAA-MM-JJTHH:MM ».

    Un run en retard sert le créneau qu'il RATTRAPE, pas l'heure à laquelle il
    tourne : c'est ce qui rend le dé-doublonnage insensible au retard de
    GitHub. Avant le premier créneau du jour, c'est le dernier d'hier (le trou
    de nuit 23:03 → 06:03)."""
    minute, heures = standard_slots()
    for jours in (0, 1):
        jour = now - timedelta(days=jours)
        for h in reversed(heures):
            t = jour.replace(hour=h, minute=minute, second=0, microsecond=0)
            if t <= now:
                return t.strftime("%Y-%m-%dT%H:%M")
    raise ValueError("aucun créneau dû : le cron `standard` n'a plus d'heures")


def _meta_get(key: str) -> str | None:
    """meta.<key> par la clé anon (SELECT autorisé). None si absent ou
    injoignable : jamais bloquant."""
    import requests  # dépendance du projet, importée tard pour --dry-run

    url, anon = os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_KEY", "")
    if not (url and anon):
        return None
    try:
        r = requests.get(f"{url}/rest/v1/meta",
                         params={"key": f"eq.{key}", "select": "value"},
                         headers={"apikey": anon, "Authorization": f"Bearer {anon}"},
                         timeout=10)
        r.raise_for_status()
        rows = r.json()
    except Exception as e:  # noqa: BLE001
        print(f"::notice::Lecture de meta.{key} impossible ({type(e).__name__}) — ignoré")
        return None
    return (rows[0].get("value") or None) if rows else None


def _meta_set(key: str, value: str) -> bool:
    """Upsert meta.<key> avec la clé service_role (RLS bloque l'écriture
    anon). False sur échec — le créneau pourra être servi deux fois, ce qui
    est le comportement d'avant ce garde-fou."""
    import requests

    url = os.environ.get("SUPABASE_URL", "")
    service = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not (url and service):
        print(f"::warning::meta.{key} non marqué : pas de clé service_role")
        return False
    try:
        r = requests.post(f"{url}/rest/v1/meta", params={"on_conflict": "key"},
                          json={"key": key, "value": value,
                                "updated_at": datetime.now(timezone.utc).isoformat()},
                          headers={"apikey": service,
                                   "Authorization": f"Bearer {service}",
                                   "Prefer": "resolution=merge-duplicates"},
                          timeout=10)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        print(f"::warning::Écriture de meta.{key} échouée ({type(e).__name__}) — "
              "le créneau pourra être servi deux fois")
        return False
    return True


def slot_deja_servi(slot: str) -> bool:
    """Ce créneau a-t-il déjà eu SON scan payant ? Sans Supabase : non — mieux
    vaut un scan de trop qu'un créneau sans couverture."""
    return _meta_get(SLOT_META_KEY) == slot


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
    """Bouton « Scanner » du dashboard : un tick reprice (horaire, sans scan)
    est promu en scan complet `standard` ; un tick standard l'est déjà.

    Le flag est lu par les 32 ticks : un clic tombant sur un tick standard est
    consommé sans promotion — le clic obtient bien un scan complet. Ne pas
    ajouter de poller dédié pour rendre la promotion plus rapide : c'est
    l'erreur du 2026-07-07 (288 déclenchements/jour). Le rattrapage du chien
    de garde Cloudflare dispatche `reprice` : il passe par ici aussi, donc un
    clic en attente est honoré même sur un rattrapage."""
    return "standard" if (manual and mode == "reprice") else mode


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
    ap.add_argument("--note-slot", action="store_true",
                    help="marque le créneau standard comme servi (après un scan réussi)")
    args = ap.parse_args(argv)

    now = datetime.now(timezone.utc)

    if args.note_slot:
        # Le créneau vient du step « mode » (l'heure DUE) : un scan qui déborde
        # sur le créneau suivant ne doit pas le consommer.
        slot = os.environ.get("SCAN_SLOT") or due_slot(now)
        ok = _meta_set(SLOT_META_KEY, slot)
        print(f"créneau {slot} {'marqué servi' if ok else 'NON marqué'}")
        return 0

    event = os.environ.get("GITHUB_EVENT_NAME", "schedule")
    try:
        mode = resolve(event, os.environ.get("SCHEDULE", ""), os.environ.get("INPUT_MODE", ""))
    except ValueError as e:
        print(f"::error::{e}")
        return 1
    manual = False if args.dry_run else consume_manual_flag()
    final = promote(mode, manual)
    slot = due_slot(now)

    # Dé-doublonnage de créneau. Épargné : le bouton « Scanner » (l'opérateur
    # veut un scan MAINTENANT) et un dispatch coché `force`.
    force = os.environ.get("INPUT_FORCE", "").strip().lower() == "true"
    doublon = (final == "standard" and not manual and not force
               and not args.dry_run and slot_deja_servi(slot))
    if doublon:
        final = "reprice"
        print(f"::notice::Créneau {slot} déjà servi par un scan payant — ce run "
              "se dégrade en `reprice` (gratuit) au lieu de repayer les mêmes ligues")

    env = env_for(final, os.environ.get("INPUT_HOURS", ""))

    line = (f"mode={final} (demandé={mode}, manual={manual}, event={event}, "
            f"créneau={slot}{', DOUBLON dégradé' if doublon else ''}) env={sorted(env)}")
    print(line)
    if args.dry_run:
        return 0
    _append("GITHUB_ENV", "".join(f"{k}={v}\n" for k, v in env.items()))
    _append("GITHUB_OUTPUT",
            f"mode={final}\nmanual={str(manual).lower()}\nslot={slot}\n")
    _append("GITHUB_STEP_SUMMARY", f"### Scan `{final}` — {line}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
