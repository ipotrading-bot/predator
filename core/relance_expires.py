"""Relance des lignes EXPIRÉES : un score manqué n'est plus jamais définitif.

LE DÉFAUT QUE CE MODULE CORRIGE. Jusqu'ici `expired` était un état TERMINAL :
`audit_engine.fetch_pending` ne sélectionne que `status='active'`, donc une
ligne passée en `expired` — le plus souvent parce qu'on n'a pas PU chercher
(quota Tavily au plafond, Groq en limite par minute, api-sports qui ferme
l'historique au plan gratuit) — ne repassait plus jamais devant un moteur de
recherche. Le commentaire d'`audit_one` le disait déjà noir sur blanc : « a
transient rate limit permanently cost those signals their real WIN/LOSS ».
Mesuré le 2026-08-27 : 199 lignes du ledger et 56 signaux dans cet état, soit
57 % du portefeuille absent de /performance — un biais de survie, puisque
`learning_layer._clv_stats` exclut les expirés.

CE QUE FAIT CE MODULE. À la fin de chaque audit, il reprend un lot de lignes
expirées et REFAIT la recherche, web comprise. Deux populations, parce qu'une
ligne expirée peut avoir perdu son signal :
  - les signaux `status='expired'` encore présents → `settle_signal`, donc le
    vrai chemin (patch de la ligne + insert au ledger + idempotence) ;
  - les lignes du ledger dont le signal a été purgé → il ne reste que
    l'affiche, le marché et la sélection, ce qui suffit à `determine_outcome`.

TROIS GARDES QUI NE SE DISCUTENT PAS.

1. **Ce lot passe APRÈS le settlement normal, jamais avant.** La réserve IA du
   settlement est tenue en négatif depuis la panne du 2026-08-02 : un signal
   frais vaut plus qu'un vieux. Appelé en tête d'audit, ce module mangerait le
   quota des matchs du jour pour courir après des matchs d'il y a deux
   semaines.

2. **Un curseur tournant** (`meta.relance_expires_cursor`). Sans lui, un
   budget de 12 lignes par run repasserait éternellement sur les 12 mêmes
   lignes introuvables — 4 audits par jour à taper sur les mêmes réserves
   australiennes, pendant que les autres ne seraient jamais retentées. Le
   curseur garantit la COUVERTURE, pas la vitesse.

3. **Jamais bloquant, jamais fatal.** Toute panne ici se log et rend la main :
   ce module améliore un état déjà écrit, il ne doit pas pouvoir faire échouer
   un audit qui a par ailleurs bien travaillé.

Ce qu'il ne fait PAS : deviner. `determine_outcome` rend `UNKNOWN` sur un
marché indécidable, `fetch_match_result` rend `None` quand le score n'est pas
trouvé — dans les deux cas la ligne reste `expired` et repassera. Un WIN/LOSS
faux au ledger est DÉFINITIF ; l'attente ne l'est pas.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from core.ai_search import ai_available
from core.settlement import determine_outcome, fetch_match_result, settle_signal

log = logging.getLogger(__name__)

# Lignes retentées par audit. 12 × 4 audits/jour = 48 relances/jour, soit un
# tour complet des ~40 lignes résiduelles chaque jour, sans jamais approcher
# le budget du settlement frais (SETTLE_BUDGET = 25 par run, dépensé AVANT).
RELANCE_BUDGET = int(os.environ.get("RELANCE_EXPIRES_BUDGET", "12"))

CURSEUR_KEY = "relance_expires_cursor"


def _curseur_lire(sb) -> int:
    try:
        r = sb.table("meta").select("value").eq("key", CURSEUR_KEY).execute()
        return int((r.data or [{}])[0].get("value", 0))
    except Exception:                                            # noqa: BLE001
        return 0


def _curseur_ecrire(sb, valeur: int) -> None:
    try:
        sb.table("meta").upsert(
            {"key": CURSEUR_KEY, "value": str(valeur),
             "updated_at": datetime.now(timezone.utc).isoformat()},
            on_conflict="key").execute()
    except Exception as e:                                       # noqa: BLE001
        log.debug("curseur %s: %s", CURSEUR_KEY, e)


def _issue(match: str, sport: str, market_key: str, selection: str,
           home_score: int, away_score: int) -> str | None:
    """L'issue selon le moteur. None si le marché n'est pas décidable."""
    if not match or " vs " not in match:
        return None
    home, away = (p.strip() for p in match.split(" vs ", 1))
    out = determine_outcome(sport or "soccer", market_key or "h2h",
                            selection or "", home, away, home_score, away_score)
    return None if out == "UNKNOWN" else out


def relancer(sb, budget: int | None = None) -> dict:
    """Retente un lot de lignes expirées. Rend un compte-rendu chiffré."""
    faits = {"signaux": 0, "ledger": 0, "sans_score": 0, "indecidable": 0}
    if not ai_available():
        # Pas d'erreur : sans fournisseur, la recherche web est impossible et
        # laisser la ligne `expired` est le comportement correct — elle
        # repassera au prochain audit.
        log.info("RELANCE — aucun fournisseur IA disponible, lot sauté")
        return faits

    budget = RELANCE_BUDGET if budget is None else budget
    if budget <= 0:
        return faits

    restant = [budget]
    now_iso = datetime.now(timezone.utc).isoformat()

    # ── 1. Signaux expirés : ils ont gardé leur match_time, donc la
    #       recherche web est mieux ancrée. On les sert en premier.
    try:
        sigs = (sb.table("signals").select("*")
                .eq("status", "expired")
                .order("match_time", desc=True)
                .limit(budget).execute()).data or []
    except Exception as e:                                       # noqa: BLE001
        log.warning("RELANCE — lecture des signaux expirés : %s", e)
        sigs = []

    for sig in sigs:
        if restant[0] <= 0:
            break
        restant[0] -= 1
        try:
            if settle_signal(sb, sig, now_iso):
                faits["signaux"] += 1
                log.info("RELANCE ✓ signal %s | %s", sig.get("id"), sig.get("match"))
            else:
                faits["sans_score"] += 1
        except Exception as e:                                   # noqa: BLE001
            log.warning("RELANCE — signal %s : %s", sig.get("id"), e)

    # ── 2. Lignes de ledger orphelines, prises à partir du curseur.
    if restant[0] > 0:
        depart = _curseur_lire(sb)
        try:
            lignes = (sb.table("ai_learning_ledger")
                      .select("id,match,sport,market_type,selection")
                      .eq("outcome", "expired")
                      .order("created_at", desc=False)
                      .range(depart, depart + restant[0] - 1).execute()).data or []
        except Exception as e:                                   # noqa: BLE001
            log.warning("RELANCE — lecture du ledger : %s", e)
            lignes = []

        # Le curseur a dépassé la fin : on repart du début au prochain tour.
        if not lignes and depart > 0:
            _curseur_ecrire(sb, 0)
        else:
            _curseur_ecrire(sb, depart + len(lignes))

        for row in lignes:
            if restant[0] <= 0:
                break
            restant[0] -= 1
            try:
                # Pas de date : le ledger ne porte pas le coup d'envoi une fois
                # le signal purgé. api-sports refusera (il lui faut une date),
                # la recherche web prend le relais — c'est le but.
                res = fetch_match_result(row.get("match", ""), row.get("sport") or "soccer", "")
                if not res or not res.get("completed"):
                    faits["sans_score"] += 1
                    continue
                out = _issue(row.get("match", ""), row.get("sport"),
                             row.get("market_type"), row.get("selection"),
                             int(res["home_score"]), int(res["away_score"]))
                if out is None:
                    faits["indecidable"] += 1
                    continue
                (sb.table("ai_learning_ledger").update({"outcome": out})
                   .eq("id", row["id"]).execute())
                faits["ledger"] += 1
                log.info("RELANCE ✓ ledger %s | %s | %d-%d -> %s",
                         str(row["id"])[:8], row.get("match"),
                         res["home_score"], res["away_score"], out)
            except Exception as e:                               # noqa: BLE001
                log.warning("RELANCE — ledger %s : %s", row.get("id"), e)

    log.info("RELANCE — %d signal(aux) et %d ligne(s) de ledger réglés | "
             "%d sans score | %d marché indécidable",
             faits["signaux"], faits["ledger"], faits["sans_score"], faits["indecidable"])
    return faits
