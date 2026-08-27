#!/usr/bin/env python3
"""Rattrape les lignes `expired` dont le score a été retrouvé À LA MAIN.

POURQUOI CE SCRIPT EXISTE. Le settlement normal cherche le score chez
api-sports puis, en dernier recours, par recherche web. Les deux ont lâché
ensemble fin août 2026 (Tavily au plafond de plan, Groq en limite), et
api-sports ferme l'historique au plan gratuit — « Free plans do not have
access to this date ». Résultat mesuré le 2026-08-27 : **255 lignes**
`expired` (199 au ledger dont le signal était déjà purgé, 56 signaux encore
présents), soit 43 % seulement de résolution sur /performance. Une ligne
expirée n'est pas neutre : `learning_layer._clv_stats` l'exclut, donc le
portefeuille est jugé sur sa partie la mieux suivie — un biais de survie que
la page elle-même signale.

CE QU'IL NE FAIT PAS. Il ne devine RIEN. Il consomme un fichier de scores
établi à la main (`reports/backfill_scores_2026-08.json`), et pour chaque
ligne il calcule l'issue avec `core.settlement.determine_outcome` — la
fonction du moteur, jamais une copie. Une affiche absente du fichier reste
`expired`. C'est voulu : les affiches ambiguës (séries MLB de trois matchs
consécutifs sans date de coup d'envoi, scores contradictoires entre deux
sources) ont été volontairement laissées de côté, même règle que
`result_from_api_sports` — deux prétendants, on refuse.

COMMENT IL ÉCRIT.
  - Signaux encore présents : il appelle `core.settlement.settle_signal`,
    donc le VRAI chemin (patch de la ligne + insert au ledger + idempotence).
    Seule la SOURCE du score est substituée, par le fichier — sa logique
    d'issue, elle, n'est pas dupliquée. Copier le corps de `settle_signal`
    ici aurait créé la deuxième liste qui diverge (CLAUDE.md §6).
  - Lignes de ledger orphelines (leur signal a été purgé) : simple UPDATE de
    `outcome`. C'est exactement la colonne que `log_to_ledger` écrit et que
    `learning_layer` relit ; `actual_result`/`profit_units` restent nulles
    comme le pipeline les laisse.

IDEMPOTENT : une ligne déjà réglée est sautée. Rejouable sans dommage.

    python scripts/backfill_expired_results.py                  # à blanc
    python scripts/backfill_expired_results.py --write          # applique
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from core import settlement                       # noqa: E402
from core.db import get_db                        # noqa: E402
from core.settlement import determine_outcome     # noqa: E402

SCORES = ROOT / "reports" / "backfill_scores_2026-08.json"


def _issue(match: str, sport: str, market_key: str, selection: str, sc: list):
    """L'issue selon le moteur. None si le marché n'est pas décidable."""
    if " vs " not in match:
        return None
    home, away = (p.strip() for p in match.split(" vs ", 1))
    out = determine_outcome(sport or "soccer", market_key or "h2h",
                            selection or "", home, away, int(sc[0]), int(sc[1]))
    return None if out == "UNKNOWN" else out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="applique réellement (sinon : simulation)")
    ap.add_argument("--scores", default=str(SCORES))
    args = ap.parse_args(argv)

    scores = json.load(open(args.scores, encoding="utf-8"))
    sb = get_db(write=True)
    if sb is None:
        print("ERREUR : pas de client Supabase en écriture", file=sys.stderr)
        return 1

    now_iso = datetime.now(timezone.utc).isoformat()
    faits = {"signal": 0, "ledger": 0, "saute": 0, "indecidable": 0, "sans_score": 0}

    # ── 1. Signaux encore présents ────────────────────────────────────
    sigs = (sb.table("signals").select("*").eq("status", "expired").execute()).data or []
    for sig in sigs:
        sc = scores.get(sig["match"])
        if not sc:
            faits["sans_score"] += 1
            continue
        out = _issue(sig["match"], sig.get("sport"), sig.get("market_key"),
                     sig.get("selection_name"), sc)
        if out is None:
            faits["indecidable"] += 1
            continue
        print(f"  signal {sig['id']:>6} | {sig['match'][:48]:48s} | {sc[0]}-{sc[1]} -> {out}")
        if args.write:
            # On substitue la SOURCE du score, pas la logique de règlement :
            # settle_signal reste seul maître du patch, du ledger et de
            # l'idempotence. Restauré juste après, le processus étant partagé.
            vrai = settlement.fetch_match_result
            settlement.fetch_match_result = (
                lambda m, s, d="", _sc=sc: {"home_score": int(_sc[0]),
                                            "away_score": int(_sc[1]),
                                            "completed": True,
                                            "source": "backfill_manuel"})
            try:
                if settle_ok := settlement.settle_signal(sb, sig, now_iso):
                    faits["signal"] += 1
                if not settle_ok:
                    print(f"    ⚠ écriture refusée pour {sig['id']}")
            finally:
                settlement.fetch_match_result = vrai
        else:
            faits["signal"] += 1

    # ── 2. Lignes de ledger orphelines ────────────────────────────────
    led = (sb.table("ai_learning_ledger").select(
        "id,match,sport,market_type,selection,outcome")
        .eq("outcome", "expired").execute()).data or []
    for row in led:
        sc = scores.get(row["match"])
        if not sc:
            faits["sans_score"] += 1
            continue
        out = _issue(row["match"], row.get("sport"), row.get("market_type"),
                     row.get("selection"), sc)
        if out is None:
            faits["indecidable"] += 1
            continue
        print(f"  ledger {str(row['id'])[:8]} | {row['match'][:48]:48s} | {sc[0]}-{sc[1]} -> {out}")
        if args.write:
            try:
                (sb.table("ai_learning_ledger").update({"outcome": out})
                   .eq("id", row["id"]).execute())
                faits["ledger"] += 1
            except Exception as e:  # noqa: BLE001
                print(f"    ⚠ {row['id']} : {e}")
        else:
            faits["ledger"] += 1

    mode = "ÉCRIT" if args.write else "SIMULATION"
    print(f"\n[{mode}] signaux réglés {faits['signal']} | lignes ledger {faits['ledger']} | "
          f"sans score {faits['sans_score']} | marché indécidable {faits['indecidable']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
