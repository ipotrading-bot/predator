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
import logging
import os
from datetime import datetime, timezone

import requests

from core.audit_engine import count_missed_closing_lines
from core.constants import (CLOSING_SRC_EXCHANGE, CLOSING_SRC_ODDSAPI,
                            CLOSING_SRC_ORACLE, TAX_RATE)
from core.db import get_db
from core.learning_layer import (SPORT_DEFAULTS, _LEDGER_SELECT, _clv_stats,
                                 _sport_stats, load_sport_verdicts, playable_rows)
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


def closing_coverage(signal_rows: list[dict], missed: int) -> list[str]:
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
                   "de closing_line.yml et la passe post-scan de scan.yml")
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
    return lignes


# ── Par ligue (2026-09-05) ─────────────────────────────────────────────
# Le foot porte 98 % des lignes réglées depuis l'époque A6, et « soccer » est
# un agrégat : MLS, Brésil, Argentine, Big 5 et coupes mineures n'ont aucune
# raison de partager le même edge. La colonne `league` existait, personne ne
# la lisait. Même contrat que le reste du rapport : zone jouable, non shadow,
# Wilson bas contre le point mort après taxe — jamais un taux nu (règle n°7).
_LEAGUE_MIN_DECIDED = int(os.environ.get("WEEKLY_LEAGUE_MIN_DECIDED", "10"))
_LEAGUE_SELECT = "league, sport, outcome, odds, time_to_match_minutes, is_shadow, created_at"


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
                  leagues: list[str] | None = None) -> str:
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
        lines.append(
            f"{flag} *{sport}* — n={m['n']} réglés · réussite {_pct(m['hit_rate'])} "
            f"(Wilson- {_pct(m['wilson_lower'])}, requis {_pct(m['p_breakeven'])})\n"
            f"   CLV réel {_pct(m['avg_clv'], True)} sur {m['clv_n']} captures "
            f"({_pct(m['clv_positive_rate'])} positives) · ROI net taxe {_pct(m['roi_net'], True)}\n"
            f"   {brier}")
        if v.get("status") == "promotion_eligible":
            lines.append(f"   → éligible à la restauration progressive de sa fraction Kelly ({v.get('reason')})")
        elif v.get("retrait_propose"):
            lines.append(f"   → ⚠️ RETRAIT PROPOSÉ — {v.get('reason')} (décision opérateur)")
    sus, total = suspect
    lines.append("")
    lines.append(f"🔴 SUSPECT_DATA : {sus}/{total} signaux récents "
                 f"({(100*sus/total):.1f}%)" if total else "🔴 SUSPECT_DATA : aucun signal récent")
    alerts = [s for s, v in verdicts.items() if v.get("retrait_propose")]
    if alerts:
        lines.append("")
        lines.append("⚠️ *Alertes* — retrait proposé : " + ", ".join(sorted(alerts))
                     + " (≥30 réglés, edge non démontré — à trancher par l'opérateur)")
    lines.extend(leagues or [])
    lines.extend(closing or [])
    lines.extend(format_ai_health(ai_health or []))
    return "\n".join(lines)


def _send(text: str) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log.warning("Telegram non configuré — rapport imprimé seulement")
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text, "parse_mode": "Markdown"},
                          timeout=15)
        if r.status_code != 200:
            log.error("Telegram HTTP %d: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.error("Telegram: %s", e)


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
        closing = closing_coverage(signal_rows, count_missed_closing_lines(sb))
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
        leagues = format_leagues(league_breakdown(res.data or []))
    except Exception as e:                  # jamais bloquant pour le rapport
        print(f"par ligue : lecture impossible — {e}")
        leagues = []
    text = format_report(metrics, load_sport_verdicts(sb), suspect, now, ai_health, closing,
                         leagues)
    print(text)
    _send(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
