"""
core/bank_view.py — ce que la page /bank affiche, calculé à part (testable).

Entrées : les lignes du ledger du mois (réglées, expirées) et les signaux
actifs du mois (mises engagées), telles que la route les a lues. Sorties :
un résumé, la série QUOTIDIENNE (misé, retours, résultat, cumul, restant en
fin de journée) et la liste des paris. Aucune probabilité, aucun taux nu —
une comptabilité en francs, brute (0 % d'impôt, décision opérateur).

Le JOUR d'un pari réglé est celui de son entrée au ledger (`created_at`),
c'est-à-dire le jour où /performance a validé son résultat : c'est là que
la mise sort de la bankroll (« soustraite au fur et à mesure »).
"""
from __future__ import annotations

from datetime import datetime

from core.bankroll import (BANKROLL_MONTHLY_XOF, day_of, days_in_month, days_left,
                           gross_return, net_result, stake_committed, with_stakes)
from core.perf_view import is_phantom

_REGLE = ("WIN", "LOSS", "PUSH")


def _recommandes(rows: list[dict]) -> list[dict]:
    return [r for r in rows if not is_phantom(r)]


def build(ledger_rows: list[dict], active_rows: list[dict], now: datetime,
          mois: str, bankroll: int = BANKROLL_MONTHLY_XOF) -> dict:
    """Résumé + série quotidienne + paris, pour le mois `mois` ('YYYY-MM').
    `now` sert au budget du jour (mois courant seulement)."""
    courant = now.strftime("%Y-%m") == mois
    ref = datetime.fromisoformat(f"{mois}-01T00:00:00+00:00")
    n_days = days_in_month(ref)

    led = with_stakes(_recommandes([r for r in ledger_rows if day_of(r.get("created_at"))[:7] == mois]),
                      bankroll, n_days)
    act = with_stakes(_recommandes([r for r in active_rows if day_of(r.get("created_at"))[:7] == mois]),
                      bankroll, n_days) if courant else []

    # ── Série quotidienne ──
    par_jour: dict[str, dict] = {}
    for r in led:
        j = day_of(r.get("created_at"))
        d = par_jour.setdefault(j, {"jour": j, "n": 0, "gagnes": 0, "perdus": 0, "rendus": 0,
                                    "mise": 0, "retours": 0.0, "resultat": 0.0})
        d["n"] += 1
        o = r.get("outcome")
        if o == "WIN":
            d["gagnes"] += 1
        elif o == "LOSS":
            d["perdus"] += 1
        else:
            d["rendus"] += 1
        d["mise"] += stake_committed(r)
        d["retours"] += gross_return(r) if o == "WIN" else 0.0
        d["resultat"] += net_result(r)
    jours = []
    cumul = 0.0
    depense = 0
    for j in sorted(par_jour):
        d = par_jour[j]
        cumul += d["resultat"]
        depense += d["mise"]
        d["cumul"] = round(cumul, 0)
        d["restant"] = bankroll - depense
        d["resultat"] = round(d["resultat"], 0)
        d["retours"] = round(d["retours"], 0)
        jours.append(d)

    # ── Résumé ──
    spent = sum(stake_committed(r) for r in led)
    engaged = sum(int(r["stake_xof"]) for r in act)
    retours = sum(gross_return(r) for r in led if r.get("outcome") == "WIN")
    resultat = sum(net_result(r) for r in led)
    remaining = max(0, bankroll - spent - engaged)
    today = day_of(now.isoformat())
    engaged_today = sum(int(r["stake_xof"]) for r in act if day_of(r.get("created_at")) == today)
    dl = days_left(now) if courant else 0
    budget_today = ((bankroll - spent - (engaged - engaged_today)) / dl) if dl else 0.0
    resume = {
        "mois": mois, "courant": courant, "bankroll": bankroll,
        "depense": spent, "engage": engaged, "restant": remaining,
        "retours": round(retours, 0), "resultat": round(resultat, 0),
        "roi_pct": round(resultat / spent * 100, 1) if spent else None,
        "n_regles": sum(1 for r in led if r.get("outcome") in _REGLE),
        "n_gagnes": sum(1 for r in led if r.get("outcome") == "WIN"),
        "n_perdus": sum(1 for r in led if r.get("outcome") == "LOSS"),
        "n_rendus": sum(1 for r in led if r.get("outcome") not in ("WIN", "LOSS")),
        "n_en_cours": len(act),
        "jours_restants": dl,
        "budget_jour": round(max(0.0, budget_today), 0),
        "budget_jour_dispo": round(max(0.0, budget_today - engaged_today), 0),
        "reconstituees": sum(1 for r in led + act if r.get("stake_reconstituee")),
        "solde_fin": bankroll + resultat - engaged if courant else bankroll + resultat,
    }

    # ── Paris (les plus récents d'abord) ──
    paris = []
    for r in act:
        paris.append({"jour": day_of(r.get("created_at")), "match": r.get("match"),
                      "selection": r.get("selection_name") or r.get("selection"),
                      "sport": r.get("sport"), "odds": r.get("xbet_odd") or r.get("odds"),
                      "mise": int(r["stake_xof"]), "outcome": "EN COURS", "resultat": None,
                      "reconstituee": r.get("stake_reconstituee", False)})
    for r in led:
        paris.append({"jour": day_of(r.get("created_at")), "match": r.get("match"),
                      "selection": r.get("selection"), "sport": r.get("sport"),
                      "odds": r.get("odds"), "mise": int(r["stake_xof"]),
                      "outcome": r.get("outcome"), "resultat": round(net_result(r), 0),
                      "reconstituee": r.get("stake_reconstituee", False)})
    paris.sort(key=lambda p: (p["jour"], p["outcome"] == "EN COURS"), reverse=True)

    return {"resume": resume, "jours": jours, "paris": paris}
