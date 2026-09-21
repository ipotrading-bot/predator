"""
scripts/weekly_report.py — rapport hebdomadaire de vérité (Phase 4, 2026-08-22).

La définition opérationnelle de « s'approcher de la perfection » : CLV réel
> 0 et calibration stable — pas un ROI court terme. Par sport :
  - CLV réel moyen (closing line capturée, clv_pct_real) ;
  - Brier score de sharp_prob contre l'issue réelle, avec sa référence ;
  - ROI post-taxe (mise pondérée Kelly, TAX_RATE de core/constants) ;
  - taux de signaux SUSPECT_DATA (sur `signals`, fenêtre ~48h disponible) ;
  - verdict promotion/rétrogradation (meta sport_verdict_*, posé par la
    couche d'apprentissage à chaque audit — jamais appliqué automatiquement).
Lecture seule sur Supabase (clé anon), envoi Telegram si configuré.
Lancé par .github/workflows/reports.yml, job `hebdo` (lundi 07:00 UTC).
"""
import json
import logging
import math
import os
from datetime import datetime, timezone

import requests

from core.audit_engine import count_missed_closing_lines
from core.closing_line import CAUSES_KEY as _CAUSES_KEY, CAUSES_NON_CAPTURE
from core.constants import (CLOSING_SRC_EXCHANGE, CLOSING_SRC_ODDSAPI,
                            CLOSING_SRC_ORACLE, TAX_RATE)
from core.db import get_db
from core.learning_layer import (FRONTIERE_DECISION_LE, FRONTIERE_N_REQUIS,
                                 FRONTIERE_SPORT, FRONTIERE_TESTEE_MINUTES,
                                 SPORT_DEFAULTS, _LEDGER_SELECT, _PLAYABLE_MAX_MINUTES,
                                 _clv_stats, _dater_par_signal, _sport_stats,
                                 load_sport_verdicts, playable_rows,
                                 post_correction_rows)
from core.stats_utils import brier_reference, brier_score, p_breakeven, wilson_ci

logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(message)s")
log = logging.getLogger("WEEKLY")

LIMIT = int(os.environ.get("WEEKLY_LEDGER_LIMIT", "200"))
LEAGUE_LIMIT = int(os.environ.get("WEEKLY_LEAGUE_LIMIT", "1000"))   # toutes ligues confondues
_DECISIVE = ("WIN", "LOSS")


def sport_truth_metrics(rows: list[dict]) -> dict:
    """Métriques de vérité d'un sport depuis ses lignes de ledger (zone
    jouable). Pure — testable sans réseau."""
    rows = playable_rows(rows)
    stats = _sport_stats(rows)
    clv = _clv_stats(rows)
    dec = [r for r in rows if r.get("outcome") in _DECISIVE]
    preds = [(float(r["sharp_prob"]), 1 if r["outcome"] == "WIN" else 0)
             for r in dec if r.get("sharp_prob") is not None]
    # ROI post-taxe : `_sport_stats` le rend déjà net depuis le 2026-08-27.
    # Ce module en portait sa propre copie — la seule des trois qui appliquait
    # la taxe, ce qui faisait dire au rapport hebdo autre chose qu'à la couche
    # d'apprentissage sur les mêmes lignes.
    roi_net = stats["roi"]
    return {
        "n": stats["n"], "hit_rate": stats["hit_rate"],
        "wilson_lower": stats["wilson_lower"], "p_breakeven": stats["p_breakeven"],
        "wilson_upper": stats["wilson_upper"], "roi": stats["roi"],
        "pnl_flat": stats["pnl_flat"],
        "roi_net": roi_net,
        "clv_n": clv["n"], "avg_clv": clv["avg_clv"], "clv_positive_rate": clv["positive_rate"],
        "brier": brier_score(preds) if len(preds) >= 10 else None,
        "brier_ref": brier_reference(preds) if len(preds) >= 10 else None,
        "brier_n": len(preds),
    }


def suspect_rate(signal_rows: list[dict]) -> tuple[int, int]:
    """(nb SUSPECT_DATA, total) sur des lignes de `signals`."""
    total = len(signal_rows)
    sus = sum(1 for r in signal_rows if r.get("risk_flag") == "SUSPECT_DATA")
    return sus, total


# Relevé en base le 2026-08-26 à 19:32 UTC, juste avant que la capture depuis
# l'exchange n'entre en service : 77 signaux actifs avaient dépassé leur coup
# d'envoi sans le moindre prix de clôture, sur 92 actifs — 6 seulement en
# portaient un. C'est le chiffre contre lequel se lisent les suivants. Sans
# lui, « 60 manqués » se lit comme une panne alors que ce serait un progrès.
CLOSING_MISSED_BASELINE = 77
CLOSING_BASELINE_DATE = "2026-08-26"


def causes_de_non_capture(sb, jours: int = 7, aujourdhui=None) -> dict:
    """Somme, sur `jours` glissants, des causes de non-capture de closing line
    (clés `meta` journalières posées par core.closing_line.persister_causes).

    POURQUOI. La couverture du CLV plafonnait à 77,8 % en zone jouable et
    51,4 % en fantôme (mesuré le 2026-09-21) : ~36 % des lignes réglées ne
    portent aucun `clv_pct_real`. Or le CLV converge ~3× plus vite que le
    résultat et c'est un critère de PREMIER rang des seuils. Le module
    énumérait quatre causes possibles dans ses logs sans en attribuer aucune :
    impossible de savoir laquelle corriger. Cette section rend la cause
    DOMINANTE visible, pour que le correctif suive une mesure et non une
    intuition.

    Le nom de clé et la liste des causes sont IMPORTÉS de core.closing_line,
    jamais recopiés (règle n°6). Ne lève jamais : une section absente vaut
    mieux qu'un rapport qui ne part pas."""
    from datetime import datetime, timedelta, timezone

    base = aujourdhui or datetime.now(timezone.utc)
    total: dict = {}
    for d in range(jours):
        cle = _CAUSES_KEY.format(jour=(base - timedelta(days=d)).strftime("%Y%m%d"))
        try:
            row = sb.table("meta").select("value").eq("key", cle).maybe_single().execute()
            if not (row and row.data and row.data.get("value")):
                continue
            for cause, n in json.loads(row.data["value"]).items():
                if cause in CAUSES_NON_CAPTURE:
                    total[cause] = total.get(cause, 0) + int(n)
        except Exception as e:
            log.debug("causes de non-capture [%s] : %s", cle, e)
    return total


def closing_coverage(signal_rows: list[dict], missed: int,
                     causes: dict | None = None) -> list[str]:
    """Section « couverture closing line » du rapport hebdo. Pure.

    POURQUOI ELLE EXISTE. Le CLV réel est le juge de rentabilité de tout ce
    pipeline, et `core/learning_layer.py` en fait un critère de premier rang —
    mais un signal sans prix de clôture n'y participe simplement pas. Le
    compteur existait déjà (`count_missed_closing_lines`), il ne vivait que
    dans les logs d'un job : personne ne le voyait monter. Une métrique que
    seul un humain attentif peut remarquer n'est pas surveillée.

    CE QU'ELLE DOIT RENDRE VISIBLE. La cadence de closing_line.yml est passée
    de 144 ticks/jour à 108 passes le 2026-08-26 (`4-59/10` → `14,34,54`, plus
    une passe après chaque scan). C'est un arbitrage mesurable, pas un gain
    acquis : les passes post-scan sont agglutinées sur les minutes de scan et
    non réparties. Si les manqués montent au-dessus de la référence, le retour
    arrière tient en une ligne de cron.
    """
    captures: dict[str, int] = {}
    for r in signal_rows:
        src = r.get("closing_source")
        if r.get("closing_pinnacle_price") and src:
            captures[str(src)] = captures.get(str(src), 0) + 1

    delta = missed - CLOSING_MISSED_BASELINE
    if delta <= -5:
        verdict = f"🟢 {abs(delta)} de moins qu'au {CLOSING_BASELINE_DATE}"
    elif delta >= 5:
        verdict = (f"🔴 {delta} de PLUS qu'au {CLOSING_BASELINE_DATE} — vérifier la cadence "
                   "de closing\\_line.yml et la passe post-scan de scan.yml")
    else:
        verdict = f"⚪ stable vs {CLOSING_BASELINE_DATE}"

    lignes = ["", "📉 *Couverture closing line*",
              f"   {missed} signal(s) actif(s) ont dépassé le coup d'envoi sans prix de "
              f"clôture (référence {CLOSING_MISSED_BASELINE}) — {verdict}"]
    if captures:
        libelles = {CLOSING_SRC_EXCHANGE: "exchange (exact, gratuit)",
                    CLOSING_SRC_ODDSAPI: "oddsapi (voie morte depuis le 2026-08-26)",
                    CLOSING_SRC_ORACLE: "oracle (estimation web, favori h2h)"}
        detail = " · ".join(f"{libelles.get(k, k)} : {v}"
                            for k, v in sorted(captures.items(), key=lambda kv: -kv[1]))
        lignes.append(f"   Captures en base — {detail}")
    else:
        lignes.append("   Aucune capture en base : le CLV réel ne peut alimenter aucun seuil.")
    if not captures.get(CLOSING_SRC_EXCHANGE):
        lignes.append("   ⚠️ ZÉRO capture `exchange` — `capture_from_exchange` ne produit rien. "
                      "Elle est appelée après `_enrich_from_exchange` dans run_engine.py ; "
                      "un slate sans marché Matchbook apparié donne ce résultat.")
    lignes.extend(_ventilation_causes(causes or {}))
    return lignes


# Libellés en français des causes techniques : le rapport est lu par
# l'opérateur, pas par le code. Dérivé de CAUSES_NON_CAPTURE — un gardien
# vérifie qu'aucune cause n'est sans libellé (règle n°6).
_LIBELLE_CAUSE = {
    "sans_cote_exchange":    "l'exchange ne cotait pas le match",
    "ligne_illisible":       "ligne du pari illisible",
    "ligne_absente_echelle": "ligne absente de l'échelle exchange",
    "ligne_bougee":          "ligne bougée (autre pari)",
    "selection_non_resolue": "sélection h2h non résolue",
    "sans_prix_de_nul":      "h2h sans cote de nul (DNB impossible)",
}


def _ventilation_causes(causes: dict) -> list[str]:
    """Pourquoi les captures ont manqué, la cause dominante en premier. Pure.

    Sans chiffre, on ne dit rien plutôt que d'afficher un tableau vide : une
    semaine sans non-capture est une bonne nouvelle, pas une section à remplir.
    """
    chiffres = {c: n for c, n in causes.items() if n}
    if not chiffres:
        return []
    total = sum(chiffres.values())
    classe = sorted(chiffres.items(), key=lambda kv: -kv[1])
    tete, n_tete = classe[0]
    lignes = [f"   Causes de non-capture (7 j, {total} refus) — dominante : "
              f"{_LIBELLE_CAUSE.get(tete, tete)} ({n_tete}, {n_tete / total:.0%})"]
    detail = " · ".join(f"{_LIBELLE_CAUSE.get(c, c)} {n}" for c, n in classe[1:])
    if detail:
        lignes.append(f"   Puis — {detail}")
    return lignes


# ── Par ligue (2026-09-05) ─────────────────────────────────────────────
# Le foot porte 98 % des lignes réglées depuis l'époque A6, et « soccer » est
# un agrégat : MLS, Brésil, Argentine, Big 5 et coupes mineures n'ont aucune
# raison de partager le même edge. La colonne `league` existait, personne ne
# la lisait. Même contrat que le reste du rapport : zone jouable, non shadow,
# Wilson bas contre le point mort après taxe — jamais un taux nu (règle n°7).
_LEAGUE_MIN_DECIDED = int(os.environ.get("WEEKLY_LEAGUE_MIN_DECIDED", "10"))
_LEAGUE_SELECT = ("league, sport, outcome, odds, time_to_match_minutes, is_shadow, "
                  "created_at, signal_id")   # signal_id : datation par SIGNAL de la
# section frontière (voir FRONTIERE_SPORT). Une colonne de plus sur une lecture qui
# existait déjà — zéro requête ajoutée.


# ── Frontière jouable : expérience PRÉ-ENREGISTRÉE (2026-09-21) ─────────

def frontiere_jouable(rows: list[dict], coupure: int = FRONTIERE_TESTEE_MINUTES,
                      n_requis: int = FRONTIERE_N_REQUIS) -> dict:
    """Avancement de l'expérience pré-enregistrée sur la borne basse. Pure.

    La coupure, la taille requise et la date de décision sont FIXÉES À
    L'AVANCE dans core.learning_layer (voir le bloc FRONTIERE_*) : cette
    fonction ne cherche pas la meilleure coupure, elle mesure celle qui a été
    annoncée. C'est toute la différence avec le scan qui a produit z = 2,08 en
    regardant 29 coupures — un maximum sélectionné n'est pas une preuve.

    LA POPULATION AUSSI est pré-enregistrée (`FRONTIERE_SPORT`, zone
    ≤ `_PLAYABLE_MAX_MINUTES`, datation par SIGNAL) — et ce n'était pas le cas
    au premier jet. Le premier run réel l'a révélé : le même test rendait
    z = 2,08 sur « soccer, datation par signal » et z = 1,31 sur « tous sports,
    datation par règlement ». Une pré-enregistration qui laisse la population
    ouverte n'en est pas une : elle garde la liberté de choisir la tranche qui
    arrange, le jour de la décision.

    L'appelant doit avoir posé `signal_created_at` (`_dater_par_signal`) : sans
    lui, `post_correction_rows` retombe sur `created_at` du ledger, qui est la
    date de RÈGLEMENT — l'incident du 2026-09-11 (131 lignes d'août comptées
    post-époque) dit ce que ça coûte. Un gardien vérifie que `main()` le fait.

    ⚠️ EXCEPTION ASSUMÉE à `.claude/rules/learning.md` (« toute analyse du
    ledger se conditionne sur la zone jouable 2-24 h AVANT de conclure ») :
    cette fonction NE conditionne PAS sur la borne basse, parce que c'est
    précisément elle qu'elle met à l'épreuve. Le groupe « sous la coupure » est
    fait de fantômes ; les filtrer viderait l'expérience de son objet. La borne
    HAUTE, elle, est bien appliquée — au-delà on ne parle plus de la même borne.

    Rend `decidable=False` tant que les DEUX groupes n'ont pas `n_requis`
    lignes : sous cette barre on n'annonce rien, quel que soit l'écart."""
    population = [r for r in rows
                  if (r.get("sport") or "") == FRONTIERE_SPORT
                  and r.get("time_to_match_minutes") is not None
                  and float(r["time_to_match_minutes"]) <= _PLAYABLE_MAX_MINUTES]
    decisifs = [r for r in post_correction_rows(population)
                if r.get("outcome") in _DECISIVE and r.get("odds")]
    sous = [r for r in decisifs if float(r["time_to_match_minutes"]) < coupure]
    sur = [r for r in decisifs if float(r["time_to_match_minutes"]) >= coupure]

    def _bloc(groupe: list[dict]) -> dict:
        n = len(groupe)
        if not n:
            return {"n": 0, "wins": 0, "hit_rate": None, "wilson_lower": None,
                    "avg_odds": None, "p_breakeven": None, "ev_par_pari": None}
        w = sum(1 for r in groupe if r["outcome"] == "WIN")
        cote = sum(float(r["odds"]) for r in groupe) / n
        lo, _hi = wilson_ci(w, n)
        return {"n": n, "wins": w, "hit_rate": w / n, "wilson_lower": lo,
                "avg_odds": cote, "p_breakeven": p_breakeven(cote, TAX_RATE),
                "ev_par_pari": (w / n) * cote - 1}

    a, b = _bloc(sous), _bloc(sur)
    z = None
    if a["n"] and b["n"]:
        pool = (a["wins"] + b["wins"]) / (a["n"] + b["n"])
        se = math.sqrt(pool * (1 - pool) * (1 / a["n"] + 1 / b["n"]))
        if se:
            z = (b["hit_rate"] - a["hit_rate"]) / se
    return {"coupure": coupure, "n_requis": n_requis, "sous": a, "sur": b, "z": z,
            "decidable": a["n"] >= n_requis and b["n"] >= n_requis,
            "decision_le": FRONTIERE_DECISION_LE}


def format_frontiere(etat: dict) -> list[str]:
    """Section « frontière jouable » du rapport hebdo. Pure.

    Ne conclut JAMAIS sous la taille requise : sans cette garde, la section
    rejouerait chaque semaine la tentation de croire un z de 2,0 obtenu sur un
    échantillon deux fois trop petit."""
    a, b, c = etat["sous"], etat["sur"], etat["coupure"]
    if not a["n"] or not b["n"]:
        return []
    lignes = ["", f"🧭 *Frontière jouable — test pré-enregistré à T-{c} min*",
              f"   sous {c} min : {a['wins']}-{a['n'] - a['wins']} "
              f"({a['hit_rate']:.1%}, point mort {a['p_breakeven']:.1%}, "
              f"EV/pari {a['ev_par_pari']:+.1%})",
              f"   dès {c} min : {b['wins']}-{b['n'] - b['wins']} "
              f"({b['hit_rate']:.1%}, point mort {b['p_breakeven']:.1%}, "
              f"EV/pari {b['ev_par_pari']:+.1%})"]
    z = etat["z"]
    manque = max(0, etat["n_requis"] - a["n"]) + max(0, etat["n_requis"] - b["n"])
    if not etat["decidable"]:
        lignes.append(f"   ⚪ n insuffisant ({a['n']} et {b['n']} sur "
                      f"{etat['n_requis']} requis, {manque} lignes à venir) — "
                      f"AUCUNE conclusion. Échéance {etat['decision_le']}"
                      + (f", z actuel {z:+.2f}" if z is not None else ""))
    elif z is not None and z > 1.96:
        lignes.append(f"   🔴 écart significatif (z {z:+.2f}) — dès {c} min on gagne, "
                      f"sous {c} min on perd : _PLAYABLE_MIN_MINUTES doit DESCENDRE "
                      f"à {c} (on récupère la bande {c}-120 min). Décision mûre, "
                      f"relire INCIDENTS.md avant de basculer")
    elif z is not None and z < -1.96:
        lignes.append(f"   🔴 écart significatif INVERSE (z {z:+.2f}) — sous {c} min "
                      f"on fait MIEUX. La borne actuelle est trop permissive : "
                      f"_PLAYABLE_MIN_MINUTES doit MONTER, pas descendre")
    else:
        lignes.append(f"   🟢 n atteint, écart NON significatif (z {z:+.2f}) — la "
                      f"frontière actuelle tient, expérience close")
    return lignes


def league_breakdown(rows: list[dict], min_decided: int = _LEAGUE_MIN_DECIDED) -> list[dict]:
    """Par ligue : décidés, gagnés, réussite, Wilson bas, point mort (cote
    moyenne, TAX_RATE), P&L à mise plate d'une unité. Zone jouable et non
    shadow seulement ; ligues sous `min_decided` agrégées sous « autres ».
    Pure — testable sans réseau. Tri : décidés décroissants."""
    rows = [r for r in playable_rows(rows) if not r.get("is_shadow")
            and r.get("outcome") in _DECISIVE and r.get("odds")]
    par_ligue: dict[str, list[dict]] = {}
    for r in rows:
        par_ligue.setdefault(str(r.get("league") or "?"), []).append(r)
    out, autres = [], []
    for league, lr in par_ligue.items():
        (out if len(lr) >= min_decided else autres).append((league, lr))

    def _stats(league: str, lr: list[dict]) -> dict:
        n = len(lr)
        wins = sum(1 for r in lr if r["outcome"] == "WIN")
        avg_odds = sum(float(r["odds"]) for r in lr) / n
        pnl = sum(float(r["odds"]) - 1 if r["outcome"] == "WIN" else -1.0 for r in lr)
        return {"league": league, "n": n, "wins": wins, "hit_rate": wins / n,
                "wilson_lower": wilson_ci(wins, n)[0], "avg_odds": avg_odds,
                "p_breakeven": p_breakeven(avg_odds, TAX_RATE), "pnl_flat": pnl}

    result = sorted((_stats(lg, lr) for lg, lr in out), key=lambda d: -d["n"])
    if autres:
        reste = [r for _, lr in autres for r in lr]
        d = _stats(f"autres ({len(autres)} ligues < {min_decided})", reste)
        result.append(d)
    return result


def format_leagues(breakdown: list[dict]) -> list[str]:
    """Section « par ligue » du rapport hebdo. Pure."""
    if not breakdown:
        return []
    lignes = ["", "🏟 *Par ligue* (zone jouable, hors fantômes, mise plate 1 u)"]
    for d in breakdown:
        verdict = "✅" if d["wilson_lower"] >= d["p_breakeven"] else "•"
        lignes.append(
            f"{verdict} {d['league']} — {d['wins']}-{d['n'] - d['wins']} · réussite "
            f"{_pct(d['hit_rate'])} (Wilson- {_pct(d['wilson_lower'])}, requis "
            f"{_pct(d['p_breakeven'])} à {d['avg_odds']:.2f}) · P&L {d['pnl_flat']:+.1f} u")
    return lignes


def _pct(x, signed=False):
    if x is None:
        return "—"
    return f"{x*100:+.1f}%" if signed else f"{x*100:.1f}%"


def _pts(x):
    """Une grandeur DÉJÀ en points de pourcentage (clv_pct_real). Passée par
    `_pct`, le CLV sortait multiplié par cent : « CLV réel +434.0% » le
    2026-09-14 pour +4,34 %."""
    return "—" if x is None else f"{x:+.2f}%"


def format_ai_health(rows: list[dict]) -> list[str]:
    """Section « santé IA » du rapport hebdo (mission 4). Pure.

    Ce qu'on veut voir d'un coup d'œil : qui a servi, combien, qui est au
    repos, et surtout COMBIEN DE BASCULES DE MODÈLE. Une bascule n'est pas
    une erreur — c'est le routeur qui fait son travail — mais une bascule
    récurrente sur le même fournisseur annonce un palier gratuit qui se
    referme, et c'est ça qu'on veut voir venir plutôt que découvrir un matin
    que le repli ne repliait plus rien.
    """
    if not rows:
        return ["", "🤖 *Santé IA* — aucun fournisseur configuré"]
    lines = ["", "🤖 *Santé IA* — tokens/jour, échecs, bascules"]
    for r in sorted(rows, key=lambda x: -int(x.get("calls_today") or 0)):
        etat = "🔴 repos" if r.get("breaker_open") else (
            "⚠️" if int(r.get("consecutive_errors") or 0) else "✅")
        flag = f" ⚖️{r['terms_flag']}" if r.get("terms_flag") not in ("", "-", None) else ""
        budget = f"/{r['budget']}" if r.get("budget") else ""
        lines.append(
            f"{etat} *{r['provider']}*{flag} — {r.get('calls_today', 0)}{budget} appels · "
            f"{r.get('tokens_today', 0)} tokens · {r.get('consecutive_errors', 0)} échec(s) "
            f"consécutif(s) · {r.get('failovers', 0)} bascule(s)")
    lourds = [r["provider"] for r in rows if int(r.get("failovers") or 0) >= 3]
    if lourds:
        lines.append("   → ⚠️ bascules répétées : " + ", ".join(sorted(lourds))
                     + " — palier gratuit probablement en train de se refermer")
    return lines


def format_report(metrics_by_sport: dict[str, dict], verdicts: dict[str, dict],
                  suspect: tuple[int, int], now: datetime,
                  ai_health: list[dict] | None = None,
                  closing: list[str] | None = None,
                  leagues: list[str] | None = None,
                  frontiere: list[str] | None = None) -> str:
    """Texte Telegram/console du rapport hebdo. Pure."""
    lines = [f"📚 *PREDATOR — rapport hebdo de vérité* · {now:%d/%m %H:%M} UTC",
             "CLV réel > 0 et calibration stable = l'objectif ; le ROI court terme n'est qu'un témoin.",
             ""]
    ordered = sorted(metrics_by_sport.items(),
                     key=lambda kv: (kv[1]["avg_clv"] is None, -(kv[1]["avg_clv"] or 0)))
    for sport, m in ordered:
        if m["n"] == 0 and m["clv_n"] == 0:
            continue
        v = verdicts.get(sport, {})
        flag = {"promotion_eligible": "✅", "perte_prouvee": "🔴",
                "non_demontre": "⚠️"}.get(v.get("status"), "•")
        brier = (f"Brier {m['brier']:.3f} (réf {m['brier_ref']:.3f}, n={m['brier_n']})"
                 if m["brier"] is not None else "Brier — (n<10)")
        # Chiffres du VERDICT quand il existe : l'audit les calcule sur les
        # lignes postérieures à l'époque, datées par signal, avec la clé
        # service — ce job n'a que la clé anon, qui ne lit pas
        # signals_archive. Le 2026-09-14 le rapport montrait n=57 (200
        # dernières lignes, sans époque) au-dessus d'un verdict à 39 réglés.
        s = v if v.get("n") else m
        pnl = s.get("pnl_flat")
        lines.append(
            f"{flag} *{sport}* — n={s['n']} réglés · réussite {_pct(s.get('hit_rate'))} "
            f"[IC95 {_pct(s.get('wilson_lower'))}–{_pct(s.get('wilson_upper'))}] · "
            f"point mort {_pct(s.get('p_breakeven'))}\n"
            f"   CLV réel {_pts(s.get('avg_clv'))} sur {s.get('clv_n') or 0} captures · "
            f"ROI Kelly {_pct(s.get('roi'), True)} · mise plate "
            f"{f'{pnl:+.2f} u' if pnl is not None else '—'}\n"
            f"   {brier}")
        if v.get("status") == "promotion_eligible":
            lines.append(f"   → éligible à la restauration progressive de sa fraction Kelly ({v.get('reason')})")
        elif v.get("retrait_propose"):
            lines.append(f"   → ⚠️ RETRAIT PROPOSÉ — {v.get('reason')} (décision opérateur)")
    sus, total = suspect
    lines.append("")
    # `\_` : en Markdown Telegram un `_` nu ouvre un italique. Celui de
    # SUSPECT_DATA, jamais fermé, a fait refuser le rapport entier le
    # 2026-09-14 (HTTP 400, « can't parse entities », octet 1056).
    lines.append(f"🔴 SUSPECT\\_DATA : {sus}/{total} signaux récents "
                 f"({(100*sus/total):.1f}%)" if total else "🔴 SUSPECT\\_DATA : aucun signal récent")
    alerts = [s for s, v in verdicts.items() if v.get("retrait_propose")]
    if alerts:
        lines.append("")
        lines.append("⚠️ *Alertes* — retrait proposé : " + ", ".join(sorted(alerts))
                     + " (perte prouvée, borne haute de Wilson sous le point mort"
                     " — à trancher par l'opérateur)")
    lines.extend(leagues or [])
    lines.extend(frontiere or [])
    lines.extend(closing or [])
    lines.extend(format_ai_health(ai_health or []))
    return "\n".join(lines)


def _send(text: str) -> bool:
    """True si Telegram a pris le rapport (ou n'est pas configuré).

    Le 2026-09-14 le rapport a été refusé (HTTP 400, mise en forme) et le job
    est resté VERT : l'opérateur n'a rien reçu, personne ne l'a su. Un refus
    de MISE EN FORME est retenté en texte brut — le contenu compte plus que
    le gras ; tout autre échec rend False, et main() sort en échec."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log.warning("Telegram non configuré — rapport imprimé seulement")
        return True
    for mode in ("Markdown", None):
        payload = {"chat_id": chat, "text": text}
        if mode:
            payload["parse_mode"] = mode
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json=payload, timeout=15)
        except Exception as e:
            log.error("Telegram: %s", e)
            return False
        if r.status_code == 200:
            if mode is None:
                log.warning("Telegram : rapport envoyé en texte brut (mise en forme refusée)")
            return True
        log.error("Telegram HTTP %d: %s", r.status_code, r.text[:200])
        if not (r.status_code == 400 and "parse entities" in r.text):
            return False
    return False


def main() -> int:
    sb = get_db(write=False)
    now = datetime.now(timezone.utc)
    metrics: dict[str, dict] = {}
    for sport in SPORT_DEFAULTS:
        try:
            res = (sb.table("ai_learning_ledger").select(_LEDGER_SELECT)
                   .eq("sport", sport).order("created_at", desc=True)
                   .limit(LIMIT).execute())
            metrics[sport] = sport_truth_metrics(res.data or [])
        except Exception as e:
            print(f"{sport}: lecture impossible — {e}")
    try:
        res = (sb.table("signals")
               .select("risk_flag,closing_source,closing_pinnacle_price")
               .limit(1000).execute())
        signal_rows = res.data or []
        suspect = suspect_rate(signal_rows)
    except Exception as e:
        print(f"signals: lecture impossible — {e}")
        signal_rows, suspect = [], (0, 0)
    try:
        closing = closing_coverage(signal_rows, count_missed_closing_lines(sb),
                                   causes_de_non_capture(sb))
    except Exception as e:                  # jamais bloquant pour le rapport
        print(f"couverture closing line : lecture impossible — {e}")
        closing = []
    try:
        from core.ai_router import health_summary
        ai_health = health_summary()
    except Exception as e:                  # jamais bloquant pour le rapport
        print(f"santé IA : lecture impossible — {e}")
        ai_health = []
    try:
        res = (sb.table("ai_learning_ledger").select(_LEAGUE_SELECT)
               .order("created_at", desc=True).limit(LEAGUE_LIMIT).execute())
        ledger_rows = res.data or []
        leagues = format_leagues(league_breakdown(ledger_rows))
    except Exception as e:                  # jamais bloquant pour le rapport
        print(f"par ligue : lecture impossible — {e}")
        ledger_rows, leagues = [], []
    try:
        # MÊME lecture que la section par ligue — zéro requête de plus, et les
        # deux sections parlent des mêmes lignes. Celle-ci a besoin des
        # FANTÔMES (le groupe « sous la coupure » en est fait), donc elle lit
        # `ledger_rows` brut et non le filtre non-shadow de league_breakdown.
        # Datation par SIGNAL avant toute mesure appliquée (règle 10) : le
        # `created_at` du ledger est la date de RÈGLEMENT. Fait ICI et non dans
        # la fonction pure, qui ne doit pas toucher la base.
        frontiere = format_frontiere(frontiere_jouable(_dater_par_signal(sb, ledger_rows)))
    except Exception as e:                  # jamais bloquant pour le rapport
        print(f"frontière jouable : calcul impossible — {e}")
        frontiere = []
    text = format_report(metrics, load_sport_verdicts(sb), suspect, now, ai_health, closing,
                         leagues, frontiere)
    print(text)
    return 0 if _send(text) else 1


if __name__ == "__main__":
    raise SystemExit(main())
