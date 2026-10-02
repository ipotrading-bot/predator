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
expirées et REFAIT la recherche de score (chaîne déterministe : api-sports,
MLB statsapi, TheSportsDB — la recherche web a été supprimée le 2026-09-02
avec Groq/Tavily). Deux populations, parce qu'une
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

4. **TheSportsDB est réservé, ici comme dans l'audit (2026-09-06).** Le lot
   par run se DÉRIVE de la cadence de l'audit (`AUDIT_INTERVAL_H`), un
   fantôme (`is_shadow`) ne reçoit jamais le repli TheSportsDB, et la relance
   ne touche plus au budget TheSportsDB dès qu'il est à moitié dépensé — la
   seconde moitié appartient au règlement des signaux frais. Mesuré le
   2026-09-06 : le lot de 12 était dimensionné pour 4 audits par jour ; à 8
   audits, 96 relances par jour sur ~35 lignes irrécupérables (dont 25
   fantômes), chacune coûtant jusqu'à six requêtes par ÉQUIPE à TheSportsDB —
   150/150 à 14:41, 0 ligne réglée sur 8 runs, et plus aucun repli pour les
   recommandés de la vague du soir. La classe exacte de l'incident du 04/09,
   un étage plus bas.

5. **`closed` est repris comme `expired` (2026-10-02).** `audit_one` écrit
   `closed` au lieu d'`expired` quand une clôture avait été capturée : même
   fait — le score n'a pas été trouvé —, autre mot. Depuis que l'exchange
   capture la clôture à chaque tick, les scores manqués finissent `closed`,
   et ce module, qui ne lisait qu'`expired`, ne les reprenait JAMAIS : 41
   lignes du ledger et 24 signaux sans résultat, pour toujours (mesuré le
   2026-10-02 ; 0 signal `expired` ce jour-là). Le lot par run ne grossit
   pas : les signaux en prennent au plus la MOITIÉ, sous leur propre curseur
   tournant — sans quoi les mêmes signaux les plus récents repasseraient à
   chaque audit et les lignes du ledger ne seraient plus jamais servies.

Ce qu'il ne fait PAS : deviner. `determine_outcome` rend `UNKNOWN` sur un
marché indécidable, `fetch_match_result` rend `None` quand le score n'est pas
trouvé — dans les deux cas la ligne reste `expired` et repassera. Un WIN/LOSS
faux au ledger est DÉFINITIF ; l'attente ne l'est pas.
"""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone

from core import score_sources
from core.constants import AUDIT_INTERVAL_H
from core.settlement import determine_outcome, fetch_match_result, settle_signal
from core.paim_engine import nom_avec_etage

log = logging.getLogger(__name__)

# Relances par JOUR : un tour complet des ~40 lignes résiduelles chaque jour,
# sans jamais approcher le budget du settlement frais (SETTLE_BUDGET = 25 par
# run, dépensé AVANT). C'est l'intention ; le lot par run s'en DÉDUIT.
RELANCES_PAR_JOUR = 48

# Lot par audit = relances/jour ÷ audits/jour. Écrit en dur à 12 jusqu'au
# 2026-09-06, c'est-à-dire « 4 audits par jour » figé dans un nombre : quand
# l'audit est passé à 3 h (2026-09-05), le lot a doublé sans que personne ne
# l'ait décidé. Dérivé de la cadence, il la suit. L'override d'environnement
# reste un outil d'opérateur, pas un réglage courant.
RELANCE_BUDGET = int(os.environ.get("RELANCE_EXPIRES_BUDGET")
                     or math.ceil(RELANCES_PAR_JOUR * AUDIT_INTERVAL_H / 24))

# Part du budget TheSportsDB que la relance LAISSE au règlement des signaux
# frais : au-delà, elle continue sur les voies gratuites seulement. Une ligne
# expirée est, par définition, une ligne qu'aucune source n'a rendue à
# l'audit ; la retenter vaut moins qu'un recommandé du jour qui attend.
RELANCE_TSDB_RESERVE = 0.5

CURSEUR_KEY = "relance_expires_cursor"
CURSEUR_SIGNAUX_KEY = "relance_signaux_cursor"

# Les deux mots d'un score MANQUÉ : `expired` (aucune clôture capturée) et
# `closed` (clos sur sa seule clôture). Voir la garde n°5 du module.
ETATS_SANS_SCORE = ("expired", "closed")


def _tsdb_ok(fantome: bool) -> bool:
    """La relance peut-elle toucher TheSportsDB pour cette ligne ?

    Jamais pour un fantôme (même règle que `audit_engine._tsdb_encore_utile`),
    et pour un recommandé seulement tant que la réserve du règlement frais est
    intacte. Lu à chaque ligne : le compteur est partagé et bouge pendant le
    run."""
    if fantome:
        return False
    reserve = score_sources.TSDB_DAILY_BUDGET * RELANCE_TSDB_RESERVE
    return score_sources.tsdb_budget_restant() > reserve


def _curseur_lire(sb, cle: str = CURSEUR_KEY) -> int:
    try:
        r = sb.table("meta").select("value").eq("key", cle).execute()
        return int((r.data or [{}])[0].get("value", 0))
    except Exception:                                            # noqa: BLE001
        return 0


def _curseur_ecrire(sb, valeur: int, cle: str = CURSEUR_KEY) -> None:
    try:
        sb.table("meta").upsert(
            {"key": cle, "value": str(valeur),
             "updated_at": datetime.now(timezone.utc).isoformat()},
            on_conflict="key").execute()
    except Exception as e:                                       # noqa: BLE001
        log.debug("curseur %s: %s", cle, e)


def _issue(match: str, sport: str, market_key: str, selection: str,
           home_score: int, away_score: int, decompte: tuple | None = None) -> str | None:
    """L'issue selon le moteur. None si le marché n'est pas décidable.

    `decompte` : le compte réel des deux camps quand le score n'en est pas un
    (les JEUX du tennis, `core.settlement._paire_comptable`). Jusqu'au
    2026-10-01 la relance ne le transmettait pas, alors que `settle_signal`
    le fait : un total de tennis expiré trouvait son score (0-1 en sets) et
    sortait « marché indécidable » à chaque audit, pour toujours. Vécu sur le
    signal 10547 (Over 21.5, décompte 6-12), run 36867889783."""
    if not match or " vs " not in match:
        return None
    home, away = (p.strip() for p in match.split(" vs ", 1))
    out = determine_outcome(sport or "soccer", market_key or "h2h",
                            selection or "", home, away, home_score, away_score,
                            decompte)
    return None if out == "UNKNOWN" else out


def relancer(sb, budget: int | None = None) -> dict:
    """Retente un lot de lignes expirées. Rend un compte-rendu chiffré.

    `signaux` + `ledger` = les règlements ABOUTIS. Depuis le 2026-09-17, le
    contrat de fin de l'audit les compte comme des règlements du run : un
    règlement, d'où qu'il vienne, prouve que la chaîne de scores répond. Ses
    ÉCHECS, eux, ne comptent pas — ce lot contient par construction des lignes
    que personne ne peut régler (Kakkonen, Vtora Liga…), donc les compter
    peindrait chaque audit en rouge pour toujours. L'asymétrie est le point.
    """
    faits = {"signaux": 0, "ledger": 0, "sans_score": 0, "indecidable": 0,
             "sans_tsdb": 0}
    budget = RELANCE_BUDGET if budget is None else budget
    if budget <= 0:
        return faits

    restant = [budget]
    now_iso = datetime.now(timezone.utc).isoformat()

    # ── 1. Signaux sans score (`expired`, `closed`) : ils ont gardé leur
    #       match_time, donc la recherche est ancrée sur le bon jour. Servis en
    #       premier, sur la MOITIÉ du lot au plus et sous leur propre curseur.
    part_signaux = math.ceil(budget / 2)
    depart_s = _curseur_lire(sb, CURSEUR_SIGNAUX_KEY)
    try:
        sigs = (sb.table("signals").select("*")
                .in_("status", list(ETATS_SANS_SCORE))
                .order("match_time", desc=True)
                .range(depart_s, depart_s + part_signaux - 1).execute()).data or []
    except Exception as e:                                       # noqa: BLE001
        log.warning("RELANCE — lecture des signaux sans score : %s", e)
        sigs = []
    sigs = sigs[:part_signaux]
    if sigs:
        _curseur_ecrire(sb, depart_s + len(sigs), CURSEUR_SIGNAUX_KEY)
    elif depart_s > 0:
        _curseur_ecrire(sb, 0, CURSEUR_SIGNAUX_KEY)

    for sig in sigs:
        if restant[0] <= 0:
            break
        restant[0] -= 1
        tsdb = _tsdb_ok(bool(sig.get("is_shadow")))
        faits["sans_tsdb"] += not tsdb
        try:
            if settle_signal(sb, sig, now_iso, tsdb_ok=tsdb):
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
                      .select("id,match,sport,league,market_type,selection,is_shadow")
                      .in_("outcome", list(ETATS_SANS_SCORE))
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
            tsdb = _tsdb_ok(bool(row.get("is_shadow")))
            faits["sans_tsdb"] += not tsdb
            try:
                # Pas de date : le ledger ne porte pas le coup d'envoi une fois
                # le signal purgé. ESPN et LiveScore cherchent sur leurs
                # derniers jours sans date ; la voie PAR ÉQUIPE de TheSportsDB
                # (core/score_sources) prend le relais quand elle est ouverte —
                # l'appariement des deux noms sur un événement UNIQUE des 15
                # derniers résultats reste exigé, deux confrontations de la
                # même paire font refuser.
                # Étage porté par la ligue (jeunes), comme settle_signal.
                res = fetch_match_result(nom_avec_etage(row.get("match", ""),
                                                        row.get("league") or ""),
                                         row.get("sport") or "soccer",
                                         "", tsdb_ok=tsdb)
                if not res or not res.get("completed"):
                    faits["sans_score"] += 1
                    continue
                out = _issue(row.get("match", ""), row.get("sport"),
                             row.get("market_type"), row.get("selection"),
                             int(res["home_score"]), int(res["away_score"]),
                             res.get("decompte"))
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
             "%d sans score | %d marché indécidable | %d sans TheSportsDB",
             faits["signaux"], faits["ledger"], faits["sans_score"],
             faits["indecidable"], faits["sans_tsdb"])
    return faits
