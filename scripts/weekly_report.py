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
Lancé par .github/workflows/rank_sports.yml (hebdo, lundi 07:00 UTC).
"""
import logging
import os
from datetime import datetime, timezone

import requests

from core.constants import TAX_RATE
from core.db import get_db
from core.learning_layer import (SPORT_DEFAULTS, _LEDGER_SELECT, _clv_stats,
                                 _sport_stats, load_sport_verdicts, playable_rows)
from core.stats_utils import brier_reference, brier_score

logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(message)s")
log = logging.getLogger("WEEKLY")

LIMIT = int(os.environ.get("WEEKLY_LEDGER_LIMIT", "200"))
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
    # ROI post-taxe : la taxe ne frappe que le gain net d'un pari gagnant.
    staked = [r for r in dec if r.get("kelly_pct") and r.get("odds")]
    roi_net = None
    if staked:
        num = sum(r["kelly_pct"] * (r["odds"] - 1) * (1 - TAX_RATE) if r["outcome"] == "WIN"
                  else -r["kelly_pct"] for r in staked)
        den = sum(r["kelly_pct"] for r in staked)
        roi_net = num / den if den else None
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


def _pct(x, signed=False):
    if x is None:
        return "—"
    return f"{x*100:+.1f}%" if signed else f"{x*100:.1f}%"


def format_report(metrics_by_sport: dict[str, dict], verdicts: dict[str, dict],
                  suspect: tuple[int, int], now: datetime) -> str:
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
        res = sb.table("signals").select("risk_flag").limit(1000).execute()
        suspect = suspect_rate(res.data or [])
    except Exception as e:
        print(f"signals: lecture impossible — {e}")
        suspect = (0, 0)
    text = format_report(metrics, load_sport_verdicts(sb), suspect, now)
    print(text)
    _send(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
