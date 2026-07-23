"""
run_wiz.py — PREDATOR PAIM v10.0 — WIZ batch entry point
Déclenché par .github/workflows/wiz.yml toutes les 2 heures.

Enrichit les signaux actifs d'une analyse contextuelle (recherche web +
raisonnement IA) et les classe par probabilité de réussite. N'écrit QUE dans
`wiz_analysis` — jamais dans `signals`, jamais dans `meta`. Voir
core/wiz_engine.py pour la doctrine complète, sql/migrate_v10_0_wiz.sql pour
le schéma.

Ce que ce fichier gère, et qui n'est pas dans wiz_engine :

  CACHE PAR MATCH. Un même match produit souvent 3 signaux (h2h + totals +
  spreads) dont le contexte terrain est identique — une seule analyse les
  couvre tous. Le goulot d'étranglement est la DURÉE du run (Mistral free
  tier = 2 requêtes/minute, soit ~31 s incompressibles par match), donc
  diviser par 3 le nombre d'appels divise par 3 la durée. Le groupement par
  match_id n'est pas une optimisation, c'est ce qui rend le run tenable sous
  le timeout du workflow.

  BUDGET ET PRIORITÉ. Les matchs sont triés par edge décroissant avant
  troncature : si le budget ne permet pas de tout couvrir, ce sont les
  signaux qui pèsent le plus lourd qui sont documentés.

  TTL ET FENÊTRE DE CONFIRMATION. Ré-analyser un match toutes les 2h ne sert
  à rien — les compositions ne bougent pas. Sauf à T-3h, où elles tombent :
  une seconde passe est autorisée dans cette fenêtre, et une seule.

MODE OBSERVATION. WIZ_ENFORCE n'est lu nulle part ici : ce batch se contente
d'écrire ce qu'il observe. Aucun signal n'est filtré, retiré ou modifié, quel
que soit le verdict — y compris VETO. Le jour où l'enforcement sera activé
(après validation au Brier score sur ~30 signaux réglés), ce sera à un autre
call site de le faire, pas ici.
"""
import logging
import os
import signal as _signal
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from core import wiz_ai
from core.constants import (
    GLOBAL_TIMEOUT,
    WIZ_CONFIRM_WINDOW_H,
    WIZ_LOOKAHEAD_H,
    wiz_run_budget,
    wiz_ttl_h,
)
from core.db import MissingCredentialsError, get_db
from core.wiz_engine import INDISPONIBLE, VERDICTS, analyze_match, now_iso

load_dotenv()

# ── UTC logger — même format que run_engine.py / run_rapport.py ───────
_fmt = logging.Formatter(fmt="%(asctime)s UTC | %(levelname)-7s | %(message)s",
                         datefmt="%Y-%m-%d %H:%M:%S")
_fmt.converter = time.gmtime
_h = logging.StreamHandler()
_h.setFormatter(_fmt)
log = logging.getLogger("WIZ")
log.setLevel(logging.INFO)
log.addHandler(_h)
log.propagate = False
# Les modules appelés loggent sous PREDATOR.wiz_* — on veut leurs messages
# (sources trouvées, modèle mort, argument rejeté) dans la sortie du run.
logging.getLogger("PREDATOR.wiz_ai").addHandler(_h)
logging.getLogger("PREDATOR.wiz_ai").setLevel(logging.INFO)
logging.getLogger("PREDATOR.wiz_engine").addHandler(_h)
logging.getLogger("PREDATOR.wiz_engine").setLevel(logging.INFO)


class _WizTimeout(Exception):
    pass


def _timeout_handler(signum, frame):
    raise _WizTimeout(f"WIZ global timeout ({_GLOBAL_TIMEOUT}s)")


# Le throttle Mistral (31s/appel) rend ce run long PAR CONSTRUCTION : 20
# matchs = ~11 minutes d'attente pure avant même le temps de réponse des
# API. Le GLOBAL_TIMEOUT de 9 minutes de run_engine.py tuerait le run en
# plein milieu. On garde le même motif SIGALRM mais avec une borne adaptée,
# alignée sous le timeout-minutes: 20 du workflow pour que le run se
# termine proprement (et écrive son log de synthèse) plutôt que d'être tué
# net par GitHub Actions.
_GLOBAL_TIMEOUT = int(os.environ.get("WIZ_GLOBAL_TIMEOUT", str(GLOBAL_TIMEOUT * 2)))


def _arm_global_timeout() -> None:
    """Filet SIGALRM best-effort — motif de run_engine.py `_arm_global_timeout`.
    Dégrade silencieusement (AttributeError sous Windows, ValueError hors
    thread principal) plutôt que de faire tomber le run."""
    try:
        _signal.signal(_signal.SIGALRM, _timeout_handler)
        _signal.alarm(_GLOBAL_TIMEOUT)
    except (AttributeError, ValueError) as e:
        log.warning("Timeout global non installé (%s) — run sans borne dure", e)


def _disarm_global_timeout() -> None:
    try:
        _signal.alarm(0)
    except (AttributeError, ValueError):
        pass


def _parse_dt(s):
    """ISO → datetime UTC-aware, ou None. Tolère 'Z' et l'espace séparateur."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00").replace(" ", "T", 1))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _match_key(sig: dict) -> str:
    """Clé de regroupement d'un match.

    `signals.match_id` vaut '' sur tout signal qui ne vient pas d'OddsAPI
    (harvester, oracle — et sql/migrate_v9_2 a backfillé les NULL en ''),
    donc grouper dessus naïvement fusionnerait TOUS ces signaux en un seul
    faux « match » et leur collerait à tous l'analyse du premier. On retombe
    sur le nom du match, qui est NOT NULL et suffisamment discriminant.
    """
    mid = (sig.get("match_id") or "").strip()
    if mid:
        return mid
    return f"name:{(sig.get('match') or '').strip().lower()}"


def _load_candidates(sb, now):
    """Signaux actifs dont le coup d'envoi est dans les WIZ_LOOKAHEAD_H heures,
    regroupés par match. Retourne {clé: ctx} prêt pour analyze_match()."""
    horizon = (now + timedelta(hours=WIZ_LOOKAHEAD_H)).isoformat()
    try:
        res = (sb.table("signals").select("*")
               .eq("status", "active")
               .gte("match_time", now.isoformat())
               .lte("match_time", horizon)
               .order("edge_pct", desc=True)
               .limit(500).execute())
        rows = res.data or []
    except Exception as e:
        log.error("Lecture des signaux impossible: %s", e)
        return {}

    groups: dict = defaultdict(list)
    for s in rows:
        groups[_match_key(s)].append(s)

    ctxs = {}
    for key, sigs in groups.items():
        best = max(sigs, key=lambda s: float(s.get("edge_pct") or 0.0))
        # consensus_score : celui du meilleur signal du match. Il mesure
        # l'accord entre sources sharp sur CE prix, il n'a pas de sens
        # agrégé sur plusieurs marchés.
        ctxs[key] = {
            "key":             key,
            "match_id":        (best.get("match_id") or "").strip(),
            "match":           best.get("match") or "",
            "sport":           best.get("sport") or "",
            "league":          best.get("league") or "",
            "kickoff":         best.get("match_time") or "",
            "kickoff_dt":      _parse_dt(best.get("match_time")),
            "markets":         sorted({f"{s.get('market') or s.get('market_key') or '?'}"
                                       f" — {s.get('selection_name') or s.get('match')}"
                                       for s in sigs}),
            "market_keys":     sorted({(s.get("market_key") or "") for s in sigs}),
            "signal_ids":      sorted({s["id"] for s in sigs if s.get("id") is not None}),
            "edge_pct":        float(best.get("edge_pct") or 0.0),
            "consensus_score": best.get("consensus_score"),
        }
    return ctxs


def _load_last_analyses(sb, keys) -> dict:
    """Dernière analyse par clé de match — pour appliquer le TTL.

    On relit large (une fenêtre de temps) plutôt que match par match : une
    requête au lieu de N, et la table est petite.
    """
    if not keys:
        return {}
    since = (datetime.now(timezone.utc) - timedelta(hours=max(48.0, wiz_ttl_h() * 3))).isoformat()
    try:
        res = (sb.table("wiz_analysis")
               .select("match_id,match,analyzed_at")
               .gte("analyzed_at", since)
               .order("analyzed_at", desc=True)
               .limit(1000).execute())
        rows = res.data or []
    except Exception as e:
        # Cas le plus probable : sql/migrate_v10_0_wiz.sql pas encore
        # appliquée. On ne peut pas dédupliquer, mais on peut encore
        # analyser — l'insert échouera plus bas avec un message explicite.
        log.warning("Lecture de wiz_analysis impossible (%s) — TTL non appliqué "
                    "ce run ; sql/migrate_v10_0_wiz.sql est-elle appliquée ?", e)
        return {}

    last: dict = {}
    for r in rows:
        key = (r.get("match_id") or "").strip() or f"name:{(r.get('match') or '').strip().lower()}"
        dt = _parse_dt(r.get("analyzed_at"))
        if dt and (key not in last or dt > last[key]):
            last[key] = dt
    return last


def _needs_analysis(ctx: dict, last_dt, now) -> tuple[bool, str]:
    """Faut-il (ré)analyser ce match ? Retourne (oui/non, motif pour le log)."""
    if last_dt is None:
        return True, "jamais analysé"

    age_h = (now - last_dt).total_seconds() / 3600.0
    if age_h >= wiz_ttl_h():
        return True, f"analyse vieille de {age_h:.1f}h"

    # Fenêtre de confirmation T-3h : les compositions officielles tombent
    # ici. Une analyse faite à T-20h ne les a pas vues, quelle que soit sa
    # fraîcheur au sens du TTL. Une seule seconde passe : on ne repasse que
    # si la dernière analyse est ANTÉRIEURE à l'entrée dans la fenêtre.
    ko = ctx.get("kickoff_dt")
    if ko:
        window_start = ko - timedelta(hours=WIZ_CONFIRM_WINDOW_H)
        if window_start <= now < ko and last_dt < window_start:
            return True, "fenêtre de confirmation T-3h (compositions)"

    return False, f"en cache ({age_h:.1f}h < TTL {wiz_ttl_h():.0f}h)"


def _write(sb, row: dict, analyzed_at: str) -> bool:
    payload = dict(row)
    payload["analyzed_at"] = analyzed_at
    try:
        sb.table("wiz_analysis").insert(payload).execute()
        return True
    except Exception as e:
        log.error("INSERT wiz_analysis ÉCHOUÉ [%s] — sql/migrate_v10_0_wiz.sql "
                  "est-elle appliquée dans le SQL Editor Supabase ? : %s",
                  row.get("match"), e)
        return False


def run() -> None:
    _arm_global_timeout()
    started = time.time()
    now = datetime.now(timezone.utc)
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("WIZ v10.0 — analyse contextuelle | budget %d match(s)/run | TTL %.0fh | fenêtre %dh",
             wiz_run_budget(), wiz_ttl_h(), WIZ_LOOKAHEAD_H)

    # ── Dégradation silencieuse sans clé ──────────────────────────────
    # Testé AVANT la DB : sans fournisseur, il n'y a rien à écrire, et
    # get_db(write=True) lèverait sur l'absence de service key alors que le
    # run n'avait de toute façon rien à faire. Wiz est optionnel — son
    # absence ne doit jamais faire échouer un workflow.
    if not wiz_ai.wiz_available():
        log.warning("MISTRAL_API_KEY absente — Wiz désactivé, rien à faire")
        return
    try:
        sb = get_db(write=True)
    except MissingCredentialsError as e:
        log.error("Supabase: %s", e)
        return
    if not sb:
        log.error("SUPABASE_URL absente — abandon")
        return

    # ── Sélection ─────────────────────────────────────────────────────
    ctxs = _load_candidates(sb, now)
    if not ctxs:
        log.info("Aucun signal actif avec coup d'envoi dans les %dh — rien à analyser",
                 WIZ_LOOKAHEAD_H)
        return
    log.info("%d match(s) candidat(s) issus des signaux actifs", len(ctxs))

    last = _load_last_analyses(sb, list(ctxs))
    todo = []
    cached = 0
    for ctx in ctxs.values():
        needed, why = _needs_analysis(ctx, last.get(ctx["key"]), now)
        if needed:
            ctx["_why"] = why
            todo.append(ctx)
        else:
            cached += 1
            log.debug("SKIP | %s — %s", ctx["match"], why)

    # Priorité à l'edge : si le budget ne couvre pas tout, ce sont les
    # signaux les plus lourds qui sont documentés.
    todo.sort(key=lambda c: c["edge_pct"], reverse=True)
    max_matches = wiz_run_budget()
    if len(todo) > max_matches:
        # Borné par la DURÉE, pas par un quota de requêtes : 2 RPM = ~31 s
        # par match, et le workflow a timeout-minutes: 20.
        log.info("Budget: %d match(s) éligibles, %d analysables (~%.0f min de run)",
                 len(todo), max_matches, max_matches * 31 / 60)
        todo = todo[:max_matches]

    log.info("%d en cache · %d à analyser", cached, len(todo))

    # ── Analyse ───────────────────────────────────────────────────────
    counts = {v: 0 for v in VERDICTS}
    counts[INDISPONIBLE] = 0
    written = failed = 0

    try:
        for i, ctx in enumerate(todo, 1):
            # Le budget borne la durée du run : au-delà, GitHub Actions
            # tuerait le job avant le log de synthèse.
            if wiz_ai.search_exhausted():
                if wiz_ai.search_quota_dead():
                    # Cause côté Mistral, pas côté nous : le remède n'est pas
                    # de baisser WIZ_RUN_BUDGET mais d'attendre que le quota
                    # du connecteur se recharge (ou de changer de plan).
                    log.warning("Quota du connecteur web_search épuisé côté Mistral "
                                "après %d match(s) — arrêt propre. Ce n'est PAS "
                                "WIZ_RUN_BUDGET : attendre la recharge du quota.", i - 1)
                else:
                    log.warning("Budget local du run épuisé après %d match(s) "
                                "(WIZ_RUN_BUDGET=%d) — arrêt propre",
                                i - 1, wiz_run_budget())
                break
            if wiz_ai.wiz_dead():
                log.warning("Tous les modèles Mistral sont morts — arrêt propre après %d match(s)",
                            i - 1)
                break

            log.info("[%d/%d] %s | %s | edge %+.2f%% | %s",
                     i, len(todo), ctx["match"], ctx["sport"], ctx["edge_pct"], ctx["_why"])
            row = analyze_match(ctx)
            counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
            if _write(sb, row, now_iso()):
                written += 1
            else:
                failed += 1
    except _WizTimeout as e:
        log.error("%s — arrêt, %d analyse(s) déjà écrite(s)", e, written)
    finally:
        _disarm_global_timeout()

    # ── Synthèse ──────────────────────────────────────────────────────
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("WIZ terminé en %.0fs | %d écrite(s), %d échec(s) | %d recherche(s) consommée(s)",
             time.time() - started, written, failed, wiz_ai.queries_used())
    log.info("Verdicts | CONFIRME %d · NEUTRE %d · ALERTE %d · VETO %d · INDISPONIBLE %d",
             counts.get("CONFIRME", 0), counts.get("NEUTRE", 0), counts.get("ALERTE", 0),
             counts.get("VETO", 0), counts.get(INDISPONIBLE, 0))
    if wiz_ai.search_quota_dead():
        log.warning("Le quota du connecteur web_search de Mistral était épuisé — "
                    "les analyses de ce run sont INDISPONIBLE pour cette raison, "
                    "pas parce que l'information n'existe pas.")
    elif counts.get(INDISPONIBLE, 0) and counts[INDISPONIBLE] >= written:
        # Rien d'exploitable produit : quota mort, requêtes mal ciblées, ou
        # noms de modèles Mistral invalides (voir WIZ_MISTRAL_MODELS).
        # `python -m core.wiz_ai` diagnostique les trois en une minute.
        log.warning("Toutes les analyses sont INDISPONIBLE — vérifier les quotas "
                    "et les noms de modèles avec `python -m core.wiz_ai`")
    log.info("Mode observation (WIZ_ENFORCE sans effet ici) — aucun signal filtré")


if __name__ == "__main__":
    run()
