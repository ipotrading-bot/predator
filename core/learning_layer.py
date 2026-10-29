"""
core/learning_layer.py — PAIM v9.5 — Adaptive Thresholds (Bayesian Learning)
Lit les 120 dernières lignes de ledger par sport, n'en garde que la ZONE
JOUABLE (playable_rows : 2-24h avant le coup d'envoi, la seule que le système
recommande encore), et ajuste les planchers MIN_EDGE sur le win-rate RÉEL
(colonne `outcome`, WIN/LOSS issus de settle_signal()) — jamais sur clv_final.
clv_final, pour les lignes produites par settle_signal(), est une
re-dérivation de l'edge d'entrée à partir des mêmes prix de scan que
edge_pct : il est ~toujours >= 0 puisque MIN_EDGE a déjà rejeté les edges
négatifs avant émission, donc positif que le pari ait gagné ou perdu.

Depuis le 2026-10-29, un seuil est une FONCTION de la preuve, sans mémoire
(_verdict_seuil → _cible_seuil) : défaut du sport, sauf perte PROUVÉE
(réalisé sous l'annoncé, z ≤ −1,96, ou Wilson haut sous le point mort) ou
gain PROUVÉ — et seulement si les fantômes ne contredisent pas. Il ne bouge
qu'après _LIGNES_NEUVES_MIN réglés neufs. Les règles d'avant avançaient par
pas (+0,4 sans preuve, −0,2 avec) rejoués à chaque ligne neuve : un cliquet
qui envoyait au plafond des sports gagnants (tennis 2,0 → 6,0 à 70 %).

The module also layers a
finer (sport, market-family) threshold on top of the sport-wide one
(load_segment_thresholds/_market_family) — a sport's h2h and totals markets
don't necessarily share the same reliability. _edge_band_diagnostic() logs
(does not gate on) whether higher-edge signals actually win more often, the
core assumption the whole system rests on.
"""
import hashlib
import json
import logging
import os
from datetime import datetime, timezone

from core.constants import CLOSING_SRC_ORACLE, TAX_RATE as _TAX_RATE, net_b, roi_net_of_tax
from core.stats_utils import brier_reference, brier_score, p_breakeven, wilson_ci

log = logging.getLogger("LEARN")

_SUMMARY_KEY = "learning_summary"


# Marqueur d'ANOMALIE dans le résumé (2026-09-18, demande opérateur : « le
# message learning une fois par jour, par contre signaler erreurs et anomalies
# dès que ça survient »). Une ligne qui le porte est une erreur PROBABLE du
# pipeline — pas un mouvement de seuil de routine : elle part tout de suite
# par le digest (run_rapport.py), le reste attend le message du matin
# (run_engine.py, premier créneau `standard` du jour).
# Le marqueur est posé À LA SOURCE, là où la ligne est écrite ; personne ne
# reclasse le texte après coup (règle n°6 : dériver, jamais redire ailleurs).
# Gardé par tests/test_learning_layer.py::TestAnomalies.
ANOMALIE_MARQUEUR = "⚠️"


def lignes_anomalies(summary: list[str]) -> list[str]:
    """Les seules lignes du résumé qui valent une alerte immédiate."""
    return [l for l in summary if ANOMALIE_MARQUEUR in l]


def load_learning_summary_at(sb) -> tuple[list[str], str | None]:
    """(lignes, date de calcul ISO) — la date dit de quel CYCLE d'audit vient
    le résumé, donc si ses anomalies sont neuves. `None` si illisible."""
    try:
        res = (sb.table("meta").select("value,updated_at")
               .eq("key", _SUMMARY_KEY).limit(1).execute())
        if res.data:
            return json.loads(res.data[0]["value"]), res.data[0].get("updated_at")
    except Exception as e:
        log.warning("load_learning_summary: %s", e)
    return [], None


def load_learning_summary(sb) -> list[str]:
    """
    Human-readable lines describing what the last compute_and_save() run
    changed and why (threshold moves, segment moves, edge-band warnings) —
    persisted once per run so Telegram (run_engine.py's morning message) and
    the dashboard (api/index.py) can both show the same explanation without
    recomputing it themselves. Empty list if nothing notable happened last
    run, or the learning layer hasn't run yet.
    """
    return load_learning_summary_at(sb)[0]

def _persister_resume(sb, lignes: list[str], now: str) -> bool:
    """Écrit le résumé, et seulement s'il a CHANGÉ. Rend True s'il a été écrit.

    `updated_at` est la date du dernier CHANGEMENT, pas du dernier calcul :
    l'audit tourne toutes les 3 h et réécrivait le même résumé à chaque
    passage, sa date restait donc éternellement fraîche — et le digest, qui
    n'a que cette fraîcheur pour mémoire (clé anon, il ne peut rien marquer en
    base), a répété la même anomalie baseball toutes les 2 h les 19 et
    20/09/2026. La prochaine alerte attend désormais une vraie nouveauté.
    """
    nouveau = json.dumps(lignes[:20])
    if nouveau == json.dumps(load_learning_summary(sb)):
        log.info("learning_summary inchangé — date de changement conservée")
        return False
    try:
        sb.table("meta").upsert({
            "key":        _SUMMARY_KEY,
            "value":      nouveau,
            "updated_at": now,
        }).execute()
    except Exception as e:
        log.warning("compute_and_save: failed to persist learning_summary: %s", e)
        return False
    return True


# Per-sport baseline — uniquement les sports actifs, seuil relevé à 2.0 % minimum.
# Objectif : moins de signaux, mais plus fiables (gagner ou ne pas jouer).
SPORT_DEFAULTS: dict[str, float] = {
    "soccer":      1.2,   # WC + Copa Lib + MLS + Brasileirão + Amicaux — abaissé pour capturer plus de signaux
    "basketball":  1.5,   # NBA Finals — abaissé ; prob gate 0.65 bloquait tous les signaux
    "hockey":      2.0,   # NHL Cup Finals
    "baseball":    2.0,   # MLB + KBO + NPB — lag timezone documenté
    "rugbyleague": 2.0,   # NRL
    "aussierules": 2.0,   # AFL
    # Sports de combat — flux OddsAPI réel depuis le 2026-08-22 (Phase 1) ;
    # seuil conservateur tant que le ledger n'a pas tranché. eSports/
    # tabletennis/volleyball/handball retirés le même jour (RETIRED_SPORTS,
    # core/constants.py) : plus d'apprentissage pour eux, leurs lignes
    # historiques restent lisibles.
    "mma":         2.0,
    "boxing":      2.0,
    # Phase 2 (2026-08-22) — nouveaux flux, seuil conservateur au départ.
    "americanfootball":      2.0,
    "euroleague_basketball": 2.0,
    # Phase 3 (2026-08-22) — NCAAF + tennis majeur, même prudence.
    "college_football":      2.0,
    "tennis":                2.0,
}
_THRESHOLD_MIN = 1.0   # Floor soccer — permet de capter amicaux et WC avec petit edge
_THRESHOLD_MAX = 6.0   # Hard cap — et seuil posé sur une PERTE PROUVÉE (voir _cible_seuil)

# ── Décision de seuil SANS MÉMOIRE (2026-10-29) ─────────────────────────
# Jusqu'au 2026-10-29, un seuil avançait par pas : +0,4 dès que la réussite
# cumulée passait sous le point mort, rejoué à CHAQUE ligne neuve ; −0,2
# seulement sur preuve à 95 %. Simulé : un sport à +5 % d'edge réel finit
# au plafond 6,0 dans 51 % des cas en 60 paris. Constaté : tennis 2,0 → 6,0
# à 70 % de réussite (z +1,45 contre l'annoncé). Le seuil est désormais une
# FONCTION de la preuve, pas une marche aléatoire — voir _cible_seuil.
_Z_PREUVE          = 1.96   # réalisé contre annoncé : preuve à 95 %, bilatérale
_DETENTE           = 0.5    # abaissement sous le défaut du sport, sur gain prouvé
_LIGNES_NEUVES_MIN = 20     # paris réglés neufs exigés entre deux mouvements d'un seuil
_FANTOMES_MIN      = 10     # sous ce nombre, les fantômes ne contredisent rien
_DERIVE_T_FREIN    = -2.0   # dérive EXCÉDENTAIRE (t) qui bloque un abaissement
_MIN_SAMPLES   = 20    # Minimum closed signals before any adjustment. Ramené de 30 à 20 le
                       # 2026-08-06 en même temps que le filtre de zone jouable ci-dessous :
                       # celui-ci retire ~55% des lignes, et à 30 il ne restait plus AUCUN
                       # sport ajustable hors soccer (basketball 26, baseball 22) — le
                       # mécanisme se serait tu au lieu de se corriger. L'échantillon
                       # filtré est en outre homogène (même régime de jeu), donc 20 lignes
                       # y valent mieux que 30 lignes mélangeant trois régimes.
# Dérive sharp : nombre minimum de clôtures pour qu'elle freine quoi que ce
# soit (voir _derive_excedent). Elle ne DÉCIDE plus d'aucune montée.
_CLV_MIN_SAMPLES = 15

# ── Promotion / rétrogradation d'un sport (Phase 4 du recentrage, 2026-08-22) ──
# Critères CHIFFRÉS, loggés dans meta (`sport_verdict_<sport>`), jamais
# appliqués automatiquement à KELLY_FRACTION : la restauration d'une fraction
# Kelly et le retrait d'un sport sont des décisions opérateur. La barre est
# 30 signaux réglés (et non _MIN_SAMPLES=20, qui sert aux seuils) : on ne
# change pas la TAILLE des mises sur l'échantillon qui sert à régler les
# PLANCHERS — plus de preuve pour plus d'argent.
_PROMOTION_MIN_SAMPLES = 30
_VERDICT_KEY_PREFIX    = "sport_verdict_"


def sport_verdict(stats: dict, clv: dict | None = None) -> dict:
    """Verdict d'un sport d'après ses résultats RÉELS (outcome, jamais
    clv_final) et son CLV réel :
      - insuffisant          : n < 30 ;
      - promotion_eligible   : borne basse de Wilson > rentabilité post-taxe
                               → éligible à la restauration PROGRESSIVE de sa
                               fraction Kelly d'origine (voir KELLY_FRACTION) ;
      - perte_prouvee        : borne HAUTE < rentabilité — retrait proposé ;
      - non_demontre         : n ≥ 30 et l'intervalle chevauche encore la
                               rentabilité — ni gain ni perte prouvés, AUCUN
                               retrait proposé (2026-09-15).
    `avg_clv` est joint à titre d'éclairage (le CLV réel converge plus vite)."""
    n = stats.get("n", 0) or 0
    lo, hi, be = stats.get("wilson_lower"), stats.get("wilson_upper"), stats.get("p_breakeven")
    avg_clv = (clv or {}).get("avg_clv")
    out = {"n": n, "wilson_lower": lo, "wilson_upper": hi, "p_breakeven": be,
           "hit_rate": stats.get("hit_rate"), "roi": stats.get("roi"),
           "pnl_flat": stats.get("pnl_flat"), "roi_flat": stats.get("roi_flat"),
           "avg_clv": avg_clv, "clv_n": (clv or {}).get("n", 0)}
    if n < _PROMOTION_MIN_SAMPLES or lo is None or be is None:
        out.update(status="insuffisant", retrait_propose=False,
                   reason=f"{n}/{_PROMOTION_MIN_SAMPLES} signaux réglés")
    elif lo > be:
        out.update(status="promotion_eligible", retrait_propose=False,
                   reason=f"Wilson bas {lo*100:.1f}% > rentabilité {be*100:.1f}% (n={n})")
    elif hi is not None and hi < be:
        out.update(status="perte_prouvee", retrait_propose=True,
                   reason=f"Wilson haut {hi*100:.1f}% < rentabilité {be*100:.1f}% (n={n})")
    else:
        # « Pas prouvé » vaut dans les deux sens : aucun retrait proposé sur
        # un intervalle qui CHEVAUCHE le point mort (2026-09-15). Le football,
        # seul sport positif à mise plate (+9,86 u), recevait « ⚠️ retrait
        # proposé » à chaque digest de deux heures ; seule une perte PROUVÉE
        # (borne haute sous le point mort) propose désormais un retrait.
        out.update(status="non_demontre", retrait_propose=False,
                   reason=(f"IC [{lo*100:.1f}–{(hi or 0)*100:.1f}%] chevauche la "
                           f"rentabilité {be*100:.1f}% après {n} réglés"))
    return out


def _lecture_roi(v: dict) -> str:
    """« ROI Kelly x · mise plate y u » — TOUJOURS les deux (2026-09-10) :
    un verdict lu sur le seul ROI pondéré a proposé le retrait d'un sport
    positif à mise plate."""
    roi, pnl = v.get("roi"), v.get("pnl_flat")
    if roi is None and pnl is None:
        return ""
    k = f"ROI Kelly {roi:+.1%}" if roi is not None else "ROI Kelly n/a"
    p = f"mise plate {pnl:+.2f} u" if pnl is not None else "mise plate n/a"
    return f" [{k} · {p}]"


def _lecture_stats(stats: dict) -> str:
    """« réussite 50% [IC95 30–70%] · point mort 51% (n=20) » — jamais un
    taux nu (règle 7). Ces raisons partent sur Telegram : le 2026-09-14,
    « win rate 50% < rentabilité 51% (n=20) » y justifiait une hausse de
    plancher sur un écart d'un point, dans un intervalle de ±20."""
    hr, n = stats.get("hit_rate"), stats.get("n", 0)
    if hr is None:
        return f"n={n}"
    lo, hi, be = stats.get("wilson_lower"), stats.get("wilson_upper"), stats.get("p_breakeven")
    ic = f" [IC95 {lo*100:.0f}–{hi*100:.0f}%]" if lo is not None and hi is not None else ""
    pm = f" · point mort {be*100:.0f}%" if be is not None else ""
    return f"réussite {hr*100:.0f}%{ic}{pm} (n={n})"


def _lecture_bande(wins: int, n: int, be: float | None) -> str:
    """`_lecture_stats` pour une bande (edge, cote) comptée à la main."""
    lo, hi = wilson_ci(wins, n)
    return _lecture_stats({"hit_rate": wins / n, "n": n, "wilson_lower": lo,
                           "wilson_upper": hi, "p_breakeven": be})


def _save_sport_verdicts(sb, stats_by_sport: dict[str, dict],
                         clv_by_sport: dict[str, dict], now: str,
                         perimetre: frozenset | set | None = None,
                         calib_by_sport: dict[str, dict] | None = None) -> list[str]:
    """Persiste un verdict par sport dans meta et rend les lignes de résumé.

    `calib_by_sport` (calibration_par_tranche) est joint au verdict sous
    `calibration` : le rapport hebdo le lit, rien ne l'applique.

    Le verdict de CHAQUE sport est persisté — le rapport hebdo et le dashboard
    les lisent tous, et c'est par là qu'un sport retiré redevient un jour
    éligible. Seules les LIGNES rendues sont filtrées sur `perimetre` (None =
    tout dire) : proposer le retrait d'un sport déjà retiré, ou valider l'edge
    d'un sport qu'on n'achète plus, n'appelle aucune décision.
    """
    lines: list[str] = []
    for sport, stats in stats_by_sport.items():
        v = sport_verdict(stats, clv_by_sport.get(sport))
        v["computed_at"] = now
        if calib_by_sport and sport in calib_by_sport:
            v["calibration"] = calib_by_sport[sport]
        try:
            sb.table("meta").upsert({
                "key":        f"{_VERDICT_KEY_PREFIX}{sport}",
                "value":      json.dumps(v),
                "updated_at": now,
            }).execute()
        except Exception as e:
            log.warning("sport_verdict[%s]: %s", sport, e)
        if perimetre is not None and sport not in perimetre:
            continue
        if v["status"] == "promotion_eligible":
            lines.append(f"{sport}: ✅ edge validé ({v['reason']}) — éligible à la "
                         f"restauration progressive de sa fraction Kelly{_lecture_roi(v)}")
        elif v["retrait_propose"]:
            lines.append(f"{sport}: ⚠️ retrait proposé — {v['reason']} "
                         f"(décision opérateur, rien d'automatique){_lecture_roi(v)}")
        elif v["status"] == "non_demontre":
            lines.append(f"{sport}: edge non démontré — {v['reason']}{_lecture_roi(v)}")
    return lines


def load_sport_verdicts(sb) -> dict[str, dict]:
    """{sport: verdict} depuis meta — pour le rapport hebdo et le dashboard."""
    out: dict[str, dict] = {}
    try:
        res = sb.table("meta").select("key,value").like("key", f"{_VERDICT_KEY_PREFIX}%").execute()
        for row in res.data or []:
            try:
                out[row["key"][len(_VERDICT_KEY_PREFIX):]] = json.loads(row["value"])
            except (ValueError, TypeError):
                continue
    except Exception as e:
        log.warning("load_sport_verdicts: %s", e)
    return out

# ── Zone jouable — ce sur quoi le moteur apprend ─────────────────────
#
# L'apprentissage ne doit porter que sur les paris que le système RECOMMANDE
# encore. Mesuré le 2026-08-06 sur les 204 paris réglés du ledger :
#
#   avance 2-24h  : 91 paris,  60,4% de réussite pour 56,0% requis, ROI  +9,4%
#   hors 2-24h    : 113 paris, 41,6% pour 56,0% requis, ROI -28,5%, p=0,002
#
# Le hors-zone est le SEUL segment significatif du ledger, et il n'est plus
# jouable : >24h ne sort plus du scan (fenêtre ramenée à 24h, commit 1552f1d)
# et <2h part en fantôme PAR SIGNAL (run_engine._shadow_reason, raison
# `t_minus_2h`, borne importée d'ici — le mode de run golden n'existe plus
# depuis le 2026-09-03). Le laisser
# dans l'apprentissage faisait donc monter les seuils à cause de pertes que le
# système ne subit plus — soccer était jugé sur 50,0% de réussite alors que sa
# zone jouable en fait 65,1%.
#
# La borne basse est IMPORTÉE par run_engine._shadow_reason ; la haute suit la
# fenêtre de scan (24h) : si celle-ci bouge, bouger l'autre.
_PLAYABLE_MIN_MINUTES = 120     # T-2h — en deçà, c'est la golden hour, en fantôme
_PLAYABLE_MAX_MINUTES = 1440    # T-24h — au-delà, le scan ne va plus chercher
# Frontière des deux tranches de la zone (2026-10-23) : 2-6h / 6-24h. Lue par
# calibration_par_tranche et IMPORTÉE par run_engine (BASKET_AVANT_EFFECTIFS_MIN) :
# la règle et sa mesure coupent au même endroit.
_TRANCHE_COURTE_MAX_MINUTES = 360

# ── Expérience PRÉ-ENREGISTRÉE sur la borne basse (2026-09-21) ─────────
# La borne basse vaut 120 min, mais sa justification d'origine ne porte PAS sur
# cette grandeur : run_engine._shadow_reason le dit lui-même, « la mesure du
# 2026-08-04 (39 % de réussite sur la tranche T-2h) portait sur la TRANCHE
# HORAIRE » — l'heure de la journée, pas le délai avant le coup d'envoi. La
# règle a donc été posée sur une grandeur et justifiée par une autre, sans
# re-mesure depuis.
#
# MESURÉ le 2026-09-21 (soccer, post-A6, datation par signal), par bande de
# délai : 0-30 min −5,7 % d'EV/pari, 30-60 min −21,6 %, 60-120 min **+19,8 %**,
# 2-4 h +11,4 %, 4-8 h +6,1 %, 8-24 h +18,3 %. Agrégé à la coupure 60 min :
# ≥60 min = 85-45 (+15,16 u), <60 min = 49-46 (−13,29 u), écart 13,8 points,
# z = 2,08. La bande 60-120 min — la MEILLEURE mesurée — est aujourd'hui jetée.
#
# ⚠️ CE N'EST PAS UNE PREUVE, et c'est pour ça que rien n'a bougé. Le scan de
# TOUTES les coupures de 20 à 300 min donne z > 1,96 seulement à 40, 60 et 290
# (290 = bruit, 53 lignes du côté haut). Avec 29 coupures scannées, Bonferroni
# exige z ≈ 2,99 : le maximum est 2,08. La DIRECTION est robuste (z positif aux
# 29 coupures, +1,0 à +2,1) ; le POINT de coupure ne l'est pas. Et la coupure
# actuelle n'est pas mieux justifiée : z = 1,54 à 120 min.
#
# D'où une pré-enregistration, pour que la décision arrive sur preuve et non sur
# la coupure qui ressort le mieux après coup (le chemin bifurquant qui a produit
# l'erreur ci-dessus). On fixe À L'AVANCE la coupure testée, la taille requise
# et la règle de décision ; on ne re-scanne PLUS les coupures. Les données
# arrivent gratuitement : les signaux sous 120 min sont déjà émis, réglés et
# enregistrés en fantôme. Avancement rendu par scripts/weekly_report.frontiere_jouable.
FRONTIERE_TESTEE_MINUTES = 60
FRONTIERE_N_REQUIS = 200        # par groupe, 80 % de puissance pour 13,8 points d'écart
FRONTIERE_DECISION_LE = "2026-10-19"   # 28 j au débit mesuré (9,0 lignes/j, 42 % sous la coupure)

# LA POPULATION AUSSI EST PRÉ-ENREGISTRÉE, et ce n'était pas le cas au premier
# jet — c'est le défaut que le premier run réel a révélé. La coupure, la taille
# et la date étaient fixées, mais pas SUR QUOI on mesure : le même test rendait
# z = 2,08 sur « soccer, datation par signal » et z = 1,31 sur « tous sports,
# datation par règlement ». Laisser ce choix ouvert, c'est garder la liberté de
# prendre la tranche qui arrange le jour de la décision — exactement le chemin
# bifurquant que cette pré-enregistration existe pour fermer.
#
# Population retenue, et pourquoi chaque terme :
#   · soccer SEUL — c'est le seul sport avec une masse statistique (221 lignes
#     post-A6 contre 42 au baseball et n ≤ 3 partout ailleurs) et c'est sur lui
#     que la borne s'applique réellement ;
#   · datation par SIGNAL (_dater_par_signal) — `created_at` du ledger est la
#     date de RÈGLEMENT ; l'incident du 2026-09-11 a montré 131 lignes d'août
#     comptées post-époque pour cette raison ;
#   · zone ≤ _PLAYABLE_MAX_MINUTES — au-delà on ne parle plus de la même borne.
FRONTIERE_SPORT = "soccer"

# ── BANDE DE COTE 1,50-1,75 : hypothèse PRÉ-ENREGISTRÉE (2026-09-27) ──
# Question de l'opérateur : « on stagne à 60 %, comment monter à 70 % ? ».
# Mesure (post-A6, datation par signal, zone jouable, hors shadow, n=171) :
# réalisé 60,2 % pour une sharp_prob annoncée de 59,6 % — le moteur est
# CALIBRÉ, 60 % est ce que le marché prédit aux cotes jouées. Le taux de
# réussite se règle par la cote (75 % sous 1,50, 48 % au-dessus de 2,0) :
# viser 70 % reviendrait à jouer plus bas, sans rien gagner.
#
# Découpé par marché, par edge et par sport, chaque segment « gagnant »
# s'INVERSE sur les fantômes (totals_over +27,5 % → −8,5 % d'EV, edge 0-2
# +13,6 % → −32 %). Une seule bande tient des deux côtés : cote 1,50-1,75,
# zone 37-13 (annoncé 62,6 %, EV +22 %), fantômes 21-9 (EV +15,5 %). Mais
# elle a été lue parmi CINQ bandes après coup — le maximum sélectionné
# n'est pas une preuve (INCIDENTS.md, 2026-09-21). D'où cette
# pré-enregistration, qui ne change RIEN à l'émission (règles 10 et 11).
#
# Ce qui est figé, et pourquoi :
#   · bornes 1,50 ≤ cote < 1,75 — celles qui ont été vues, sans retouche ;
#   · population : tous sports, zone jouable (_PLAYABLE_MIN/MAX_MINUTES),
#     non-shadow (ce que l'opérateur joue), datation par SIGNAL, émis à
#     partir de BANDE_COTE_DEPUIS SEULEMENT — les lignes qui ont fait naître
#     l'hypothèse ne peuvent pas la confirmer ;
#   · statistique : excès de calibration DANS la bande, réalisé contre
#     sharp_prob annoncée, z = (W − Σp) / √Σp(1−p). Jamais un taux brut
#     comparé à une autre bande : le taux dépend de la cote par construction ;
#   · n requis 200 : 80 % de puissance pour +10 points (72,5 contre 62,5)
#     donne ≈ 183, arrondi comme FRONTIERE_N_REQUIS ;
#   · échéance : 50 lignes en 31 jours dans la bande, soit 1,6/jour → ≈ 125
#     jours pour 200.
# Avancement rendu par scripts/weekly_report.bande_cote.
BANDE_COTE_MIN = 1.50
BANDE_COTE_MAX = 1.75           # exclue
BANDE_COTE_DEPUIS = "2026-09-28"
BANDE_COTE_N_REQUIS = 200
BANDE_COTE_DECISION_LE = "2027-01-31"

# DÉCISION OPÉRATEUR du 2026-09-27, le même jour, en connaissance de cause :
# « ne pas attendre 200 paris, proposer ces signaux, je déciderai de les
# jouer ». Dans la zone jouable, la bande était DÉJÀ recommandée ; ce qui
# s'ajoute, ce sont ses signaux sous T-2h, jusque-là fantômes (21-9 depuis
# A6). run_engine.BANDE_COTE_PROPOSEE les rend recommandés. Rien n'est
# remplacé : la mesure pré-enregistrée ci-dessus reste sur la ZONE et ne
# les compte pas (bornes _PLAYABLE_*), les seuils non plus (playable_rows).


def dans_bande_cote(cote) -> bool:
    """La cote tombe-t-elle dans la bande BANDE_COTE_MIN ≤ cote < BANDE_COTE_MAX ?
    Une cote absente ou illisible n'y est pas. Seule définition de la bande :
    le moteur, Telegram, le dashboard et le rapport hebdo l'importent (règle 6)."""
    try:
        c = float(cote)
    except (TypeError, ValueError):
        return False
    return BANDE_COTE_MIN <= c < BANDE_COTE_MAX

# ── Market-family segmentation (win/loss error analysis) ─────────────
# ai_learning_ledger.market_type stores the raw market_key emitted by
# run_engine.py's _emit() — an exact "h2h", but "totals_over"/"totals_under"
# / "spreads_home"/"spreads_away" for the other two markets (side is baked
# into the key). run_engine.py only ever applies ONE min_edge per
# _process_h2h/_process_totals/_process_spreads call, so segmentation must
# operate at the market-FAMILY level, not the raw market_key — see
# _market_family() below.
_MARKET_FAMILIES = ("h2h", "totals", "spreads")
_SEGMENT_MIN_SAMPLES = 20   # lower bar than _MIN_SAMPLES: a narrower slice of the same sport


def _market_family(market_type: str | None) -> str:
    if not market_type:
        return ""
    if market_type.startswith("totals"):
        return "totals"
    if market_type.startswith("spreads"):
        return "spreads"
    return market_type   # "h2h"


def load_thresholds(sb) -> dict[str, float]:
    """
    Load sport-specific MIN_EDGE from Supabase `meta` table.
    Falls back to SPORT_DEFAULTS if the keys don't exist yet.
    """
    result = SPORT_DEFAULTS.copy()
    try:
        res = sb.table("meta").select("key,value").like("key", "threshold_%").execute()
        for row in (res.data or []):
            sport = row["key"].replace("threshold_", "")
            if sport in result:
                result[sport] = float(row["value"])
    except Exception as e:
        log.warning("load_thresholds: %s — using defaults", e)
    return result


def load_segment_thresholds(sb) -> dict[str, float]:
    """
    Load (sport, market-family) MIN_EDGE overrides from `meta`, keyed as
    "sport:family" (e.g. "soccer:totals"). A missing key means no
    segment-specific override exists yet — callers must fall back to
    load_thresholds()'s coarser sport-level value.

    Stored under a `threshold_seg_` prefix, deliberately distinct from
    load_thresholds()'s `threshold_%` — a bare `threshold_soccer_totals` key
    would otherwise also match that function's LIKE query and parse to
    sport="soccer_totals" (not in SPORT_DEFAULTS, silently ignored there),
    which works by accident rather than by design. Keeping the two
    namespaces separate makes that unambiguous instead of relying on it.
    """
    result: dict[str, float] = {}
    try:
        res = sb.table("meta").select("key,value").like("key", "threshold_seg_%").execute()
        for row in (res.data or []):
            rest = row["key"].replace("threshold_seg_", "", 1)
            for fam in _MARKET_FAMILIES:
                suffix = f"_{fam}"
                if rest.endswith(suffix):
                    sport = rest[:-len(suffix)]
                    try:
                        result[f"{sport}:{fam}"] = float(row["value"])
                    except (TypeError, ValueError):
                        pass
                    break
    except Exception as e:
        log.warning("load_segment_thresholds: %s — no segment overrides", e)
    return result


def load_edge_ceilings(sb) -> dict[str, float]:
    """Plafonds d'edge appris par sport (`edge_ceiling_<sport>` dans `meta`).

    Au-dessus de ce plafond, l'edge mesuré n'est pas une inefficience de marché
    mais un prix mal apparié ou périmé : la bande haute perd davantage que les
    bandes basses (voir _top_band_verdict). Un sport absent n'a pas de plafond —
    l'appelant garde alors les bornes globales de core/constants.py.
    """
    result: dict[str, float] = {}
    try:
        res = sb.table("meta").select("key,value").like("key", "edge_ceiling_%").execute()
        for row in (res.data or []):
            sport = row["key"].replace("edge_ceiling_", "", 1)
            try:
                result[sport] = float(row["value"])
            except (TypeError, ValueError):
                pass
    except Exception as e:
        log.warning("load_edge_ceilings: %s — aucun plafond appris", e)
    return result


def load_odds_ceilings(sb) -> dict[str, float]:
    """Plafonds de COTE appris par sport (`odds_ceiling_<sport>` dans `meta`).

    Une sélection cotée au-dessus n'est pas un favori : c'est un quasi
    pile-ou-face que le ledger a prouvé perdant pour ce sport. Sport absent =
    aucun plafond, comportement inchangé.
    """
    result: dict[str, float] = {}
    try:
        res = sb.table("meta").select("key,value").like("key", "odds_ceiling_%").execute()
        for row in (res.data or []):
            sport = row["key"].replace("odds_ceiling_", "", 1)
            try:
                result[sport] = float(row["value"])
            except (TypeError, ValueError):
                pass
    except Exception as e:
        log.warning("load_odds_ceilings: %s — aucun plafond de cote", e)
    return result


_DECISIVE_OUTCOMES = ("WIN", "LOSS")   # PUSH/UNKNOWN/closed/expired carry no real result


def _sport_stats(rows: list[dict]) -> dict:
    """
    Real performance stats from a batch of ai_learning_ledger rows.
    Keyed exclusively off `outcome` — never `clv_final` (see module
    docstring). PUSH/UNKNOWN/'closed'/'expired' rows are excluded from both
    hit_rate and ROI: they carry no decisive WIN/LOSS result.

    Le ROI est NET DE TAXE et vient de `core.constants.roi_net_of_tax` —
    formule unique du dépôt. Il était calculé ICI, en BRUT, jusqu'au
    2026-08-27 : la couche qui décide de monter ou de baisser un seuil jugeait
    donc la rentabilité sur des gains que l'opérateur n'encaisse pas. Sur un
    portefeuille à cote moyenne 1,85, la retenue de 20 % coûte environ 17
    points de ROI — largement de quoi faire passer un sport perdant pour
    rentable, et inversement.

    Une ligne sans `kelly_pct` (avant migration) est écartée du ROI mais
    compte toujours pour le `hit_rate`.
    """
    decisive = [r for r in rows if r.get("outcome") in _DECISIVE_OUTCOMES]
    n = len(decisive)
    if n == 0:
        return {"n": 0, "hit_rate": None, "roi": None, "wilson_lower": None,
                "wilson_upper": None, "p_breakeven": None,
                "pnl_flat": None, "roi_flat": None}

    wins = sum(1 for r in decisive if r["outcome"] == "WIN")
    hit_rate = wins / n
    # La borne HAUTE sert autant que la basse depuis le 2026-08-06 : elle est ce
    # qui prouve une perte (borne haute sous la rentabilité), là où la basse
    # prouve un gain. Voir _verdict_seuil.
    wilson_lower, wilson_upper = wilson_ci(wins, n)

    odds_vals = [r["odds"] for r in decisive if r.get("odds")]
    avg_odds = sum(odds_vals) / len(odds_vals) if odds_vals else None
    # Explicit constants.TAX_RATE, never p_breakeven's own bare default —
    # this breakeven gates _verdict_seuil's gain/loss decision (below),
    # and a caller relying on the default silently re-introduces the exact
    # bug this fixed: TAX_RATE=0.0 (operator-zeroed) vs the default's 0.20
    # froze every lowering behind a ~25%-harder breakeven than the account's
    # real, configured economics. See core/stats_utils.py's p_breakeven()
    # docstring for why the default itself is left alone rather than
    # imported from constants there.
    breakeven = p_breakeven(avg_odds, _TAX_RATE) if avg_odds else None

    roi = roi_net_of_tax(decisive, _TAX_RATE)

    # Mise PLATE à côté du ROI pondéré Kelly (2026-09-10) : le verdict
    # « retrait proposé » du football affichait −19,8 % de ROI Kelly quand
    # les mêmes 33 lignes faisaient +3,36 u à mise plate. Les deux sont vrais
    # (les grosses mises Kelly sont tombées sur des perdants), mais un seul
    # était montré, et c'est sur ce texte que l'opérateur décide d'un retrait.
    misables = [r for r in decisive if r.get("odds")]
    pnl_flat = (sum(net_b(r["odds"], _TAX_RATE) if r["outcome"] == "WIN" else -1.0
                    for r in misables) if misables else None)
    roi_flat = pnl_flat / len(misables) if misables else None

    return {
        "n": n,
        "hit_rate": hit_rate,
        "roi": roi,
        "wilson_lower": wilson_lower,
        "wilson_upper": wilson_upper,
        "p_breakeven": breakeven,
        "pnl_flat": pnl_flat,
        "roi_flat": roi_flat,
    }


def _clv_stats(rows: list[dict]) -> dict:
    """
    Real closing-line signal — core/audit_engine.py's capture_closing_lines()
    (hourly, pre-kickoff, see run_closing_line.py) writes the genuine
    market-confirmed CLV into `clv_pct_real`, independently of whether the
    bet later won or lost. It does NOT wait for a graded outcome, so its
    sample is often larger than the decisive WIN/LOSS one.

    Deliberately NOT `was_clv_positive`: that ledger column is `clv > 0`
    computed at insert time (core/db.py:log_to_ledger) from whichever `clv`
    the CALLER passed in — for core/settlement.py's settle_signal() that is
    the entry-edge re-derivation, the exact tautology this module's
    docstring already warns clv_final suffers from (~always >= 0, since
    MIN_EDGE rejected negative-edge signals before they were ever sent), and
    for core/audit_engine.py's Pass-2 fallback it's a proxy fetched hours-to-
    days after kickoff — not the true closing line either. Only rows the
    hourly job actually reached before kickoff carry a real `clv_pct_real`;
    everything else is None and is correctly excluded here, not defaulted.
    """
    real = [r["clv_pct_real"] for r in rows if r.get("clv_pct_real") is not None]
    n = len(real)
    if n == 0:
        return {"n": 0, "avg_clv": None, "positive_rate": None}
    positive = sum(1 for c in real if c > 0)
    return {"n": n, "avg_clv": sum(real) / n, "positive_rate": positive / n}


def _derive_stats(rows: list[dict]) -> dict:
    """Dérive du prix SHARP entre l'émission et la clôture, en % du prix —
    la même forme et la même unité que `_clv_stats` (`n`, `avg_clv`,
    `positive_rate`), lue par _derive_excedent (frein) et par calibration_stats.

        dérive = (1 / sharp_prob) / closing_pinnacle_price − 1

    Positive quand le marché sharp est venu vers notre sélection (sa cote a
    baissé entre l'entrée et la clôture) : c'est la seule partie du CLV que
    l'edge d'entrée ne contient pas déjà.

    Pourquoi pas `clv_pct_real` (2026-09-27) : il vaut cote prise / clôture
    sharp − 1, donc ≈ l'edge d'ENTRÉE tant que le sharp ne bouge pas. Mesuré
    le 2026-09-24 sur 301 lignes : CLV moyen +2,63 % pour +2,73 % d'edge
    d'entrée ; dérive −0,10 %, 106 positives contre 105 négatives. Tout
    signal émis ayant un edge positif par construction, « CLV > +1 % » était
    toujours vrai : la descente accélérée jugeait l'edge par lui-même. Le
    CLV reste AFFICHÉ (verdicts, rapport hebdo) ; il ne DÉCIDE plus rien.
    Une ligne sans clôture capturée ou sans sharp_prob est ignorée, jamais
    comptée à zéro (règle 14).

    Une clôture `oracle` est ignorée aussi (2026-09-29) : ce prix était
    DEMANDÉ à l'ancien LLM (core/oracle.py, supprimé le 2026-09-02), pas
    observé. La migration v10_15 n'avait neutralisé que le CLV de ces lignes
    et laissé le prix : 49 lignes du ledger nourrissaient encore cette
    dérive, celle qui DÉCIDE des seuils. Aucune n'entrait dans les mesures
    appliquées ce jour-là (signaux antérieurs à l'époque) — le filtre tient
    le jour où l'époque bouge."""
    derives = _derive_valeurs(rows)
    n = len(derives)
    if n == 0:
        return {"n": 0, "avg_clv": None, "positive_rate": None}
    return {"n": n, "avg_clv": sum(derives) / n,
            "positive_rate": sum(1 for d in derives if d > 0) / n}


def _derive_valeurs(rows: list[dict]) -> list[float]:
    """Une dérive par ligne qui en porte une (voir _derive_stats), en %."""
    derives = []
    for r in rows:
        if r.get("closing_source") == CLOSING_SRC_ORACLE:
            continue
        p, close = r.get("sharp_prob"), r.get("closing_pinnacle_price")
        try:
            p, close = float(p), float(close)
        except (TypeError, ValueError):
            continue
        if p <= 0 or close <= 1.0:
            continue
        derives.append(((1 / p) / close - 1) * 100)
    return derives


def _derive_excedent(rows: list[dict], reference: float | None) -> dict:
    """Dérive du segment MOINS la dérive moyenne de tout le ledger, avec son t.

    Pourquoi un excédent (2026-10-29) : la dérive brute est BIAISÉE. Mesurée
    sur 617 clôtures post-époque, elle vaut −0,64 % en moyenne (t −2,70)
    alors que les résultats collent au prix d'ENTRÉE — 329 gagnés pour 329,4
    annoncés à l'entrée, 327,3 à la clôture. La clôture est le milieu
    ARITHMÉTIQUE back/lay d'un exchange, non dévigé, comparé à un prix
    d'entrée venu d'une autre source : une part de la « dérive » est la
    fourchette, pas le marché. Elle pèse plus là où la fourchette est large :
    tennis −1,45 %, et la règle « < −1 % → ↑ » a poussé le tennis de 2,0 à
    6,0 pendant qu'il gagnait 70 %. Seul l'écart à la référence commune peut
    porter une information propre au segment.

    Renvoie {"n", "excedent", "t"} ; t vaut None sous _CLV_MIN_SAMPLES
    clôtures, sans référence, ou sur un échantillon sans dispersion."""
    vals = _derive_valeurs(rows)
    n = len(vals)
    if reference is None or n < _CLV_MIN_SAMPLES:
        return {"n": n, "excedent": None, "t": None}
    ecarts = [v - reference for v in vals]
    moy = sum(ecarts) / n
    var = sum((e - moy) ** 2 for e in ecarts) / (n - 1)
    t = moy / (var / n) ** 0.5 if var > 0 else None
    return {"n": n, "excedent": moy, "t": t}


_EDGE_BUCKETS = [(0.0, 2.0), (2.0, 4.0), (4.0, 8.0), (8.0, 100.0)]


_CEILING_MIN = 4.0   # % — jamais de plafond en dessous : sous 4% la bande
                     # haute n'est plus « suspecte », c'est le cœur du signal.
_CEILING_MIN_N = 10  # résultats minimum sous le plafond (voir _top_band_verdict) :
                     # un plafond coupe l'émission, cinq paris ne le justifient pas.

# Découpage plus fin que _EDGE_BUCKETS pour cette décision précise. Les quatre
# tranches larges de _EDGE_BUCKETS noient l'information : mesuré le 2026-08-02,
# soccer perdait au-dessus de 6% mais son bucket (4, 8) mélangeait la zone
# gagnante 4-6% avec la zone perdante 6-8%, et le verdict ne voyait rien.
_CALIB_BUCKETS = [(0.0, 1.5), (1.5, 2.5), (2.5, 4.0), (4.0, 6.0), (6.0, 100.0)]


def _top_band_verdict(rows: list[dict]) -> tuple[bool, float | None, int, float | None]:
    """La bande d'edge la plus haute perd-elle plus que les bandes basses ?

    Renvoie (sous_performe, plafond, n, plancher_de_la_meilleure_bande).

    C'est la mesure qui manquait à l'ancienne règle de seuil (avant le 2026-10-29), qui n'avait qu'un
    levier — le PLANCHER d'edge — et le monte quand un sport perd. Or si ce
    sont justement les gros edges qui perdent, monter le plancher pousse
    toute l'émission dans la bande perdante et aggrave le résultat, qui fait
    remonter le plancher au tour suivant : une boucle qui finit au plafond.
    Mesuré le 2026-08-02 sur le vrai ledger : soccer à 6%+ affichait 36,7% de
    réussite sur 49 résultats pour 47,8% requis, pendant que 1,5-4% gagnait —
    et le seuil du sport avait justement été poussé à 6,00%.

    Un gros edge qui perd plus qu'un petit n'est pas une inefficience de
    marché : c'est presque toujours un prix mal apparié ou périmé qui gonfle
    l'edge apparent (voir _edge_band_diagnostic).
    """
    decisive = [r for r in rows
                if r.get("outcome") in _DECISIVE_OUTCOMES and r.get("initial_edge") is not None]
    if len(decisive) < _SEGMENT_MIN_SAMPLES:
        return False, None, 0, None

    # Les bandes se comparent sur la MARGE au-dessus du seuil de rentabilité
    # (réussite − 1/cote), jamais sur le taux brut : chaque bande a sa propre
    # cote moyenne, donc son propre seuil. Mesuré le 2026-08-02 sur soccer, la
    # bande 0-1,5% tournait à 65,0% pour 61,6% requis (marge +3,4) et la bande
    # 2,5-4% à 80,0% pour 55,9% requis (marge +24,1) — un tri sur le brut
    # sous-estime la seconde et surestime la première.
    bucket_wr = []
    for lo, hi in _CALIB_BUCKETS:
        in_b = [r for r in decisive if lo <= r["initial_edge"] < hi]
        if len(in_b) < 5:
            continue
        wins = sum(1 for r in in_b if r["outcome"] == "WIN")
        wr = wins / len(in_b)
        odds_vals = [r["odds"] for r in in_b if r.get("odds")]
        avg_odds = sum(odds_vals) / len(odds_vals) if odds_vals else None
        be = p_breakeven(avg_odds, _TAX_RATE) if avg_odds else None
        margin = wr - be if be is not None else wr
        bucket_wr.append((lo, hi, len(in_b), margin))
    if len(bucket_wr) < 2:
        return False, None, 0, None

    best_lo, best_wr = max(((lo, m) for lo, _, _, m in bucket_wr),
                           key=lambda t: t[1])

    # On remonte depuis le haut tant que les bandes sous-performent la
    # meilleure : le plafond se pose au BAS de cette série, pas seulement sous
    # la dernière bande. Sinon une bande perdante intermédiaire reste ouverte —
    # soccer perdait dès 6% alors que seule la bande 8%+ aurait été coupée.
    #
    # Une bande n'entre dans la série que si elle PERD — marge sous son point
    # mort —, pas seulement si elle gagne moins que la meilleure ; et la série
    # doit peser _CEILING_MIN_N résultats. Le 2026-09-14 un plafond soccer à
    # 6,0 % a été posé et APPLIQUÉ sur n=5, pendant que la bande 4-8 % gagnait
    # à 71 % pour ~58 % requis : « perdante » parce que moins bonne que 75 %.
    ceiling_lo = None
    covered_n = 0
    for lo, _hi, n, wr in reversed(bucket_wr):
        if wr < best_wr and wr < 0 and lo >= _CEILING_MIN:
            ceiling_lo = lo
            covered_n += n
        else:
            break

    if ceiling_lo is None or covered_n < _CEILING_MIN_N:
        return False, None, 0, best_lo
    return True, ceiling_lo, covered_n, best_lo


_ODDS_BUCKETS = [(1.0, 1.50), (1.50, 1.80), (1.80, 2.20), (2.20, 3.00), (3.00, 99.0)]
_ODDS_CEILING_MIN = 1.50   # jamais de plafond sous 1,50 : couper les favoris
                           # courts reviendrait à ne plus rien émettre du tout.


def _odds_band_verdict(rows: list[dict]) -> tuple[float | None, int, str | None]:
    """Existe-t-il une cote au-dessus de laquelle le sport perd, PROUVÉ ?

    Renvoie (plafond_de_cote, n_couvert, diagnostic).

    Le test est ABSOLU et strict — borne HAUTE de Wilson sous le seuil de
    rentabilité de la bande — là où _top_band_verdict se contente d'une
    comparaison relative entre bandes. Cette asymétrie est voulue :

      Pour l'edge, l'affirmation « un edge trop gros est un prix mal apparié »
      préexiste dans le code (SUSPECT_EDGE, MAX_EDGE, le docstring de
      _edge_band_diagnostic). Les données ne font que confirmer une hypothèse
      déjà posée, et le test relatif haut-contre-bas la valide (p=0,005 sur
      soccer au 2026-08-02). Une comparaison relative suffit.

      Pour la cote, « on gagne sur les favoris courts » est une affirmation
      NOUVELLE, sans mécanisme préalable, et c'est exactement le genre de motif
      qui ressort d'une fouille de données puis disparaît hors échantillon. Au
      2026-08-02 aucune bande n'était concluante — même celle à n=109, dont la
      borne haute (52,5%) dépassait encore son seuil de 50,0%. Poser une règle
      dessus aurait coupé 58% du volume sur du bruit.

    Le plafond ne s'active donc que le jour où une bande est prouvée perdante.
    D'ici là la fonction renvoie None et rien ne change.
    """
    decisive = [r for r in rows
                if r.get("outcome") in _DECISIVE_OUTCOMES and r.get("odds")]
    if len(decisive) < _SEGMENT_MIN_SAMPLES:
        return None, 0, None

    proven_losing = []
    for lo, hi in _ODDS_BUCKETS:
        if lo < _ODDS_CEILING_MIN:
            continue
        in_b = [r for r in decisive if lo <= float(r["odds"]) < hi]
        if len(in_b) < _SEGMENT_MIN_SAMPLES:
            continue
        wins = sum(1 for r in in_b if r["outcome"] == "WIN")
        _, w_hi = wilson_ci(wins, len(in_b))
        avg_odds = sum(float(r["odds"]) for r in in_b) / len(in_b)
        be = p_breakeven(avg_odds, _TAX_RATE)
        if be is not None and w_hi < be:
            proven_losing.append((lo, len(in_b), wins, be))

    if not proven_losing:
        return None, 0, None

    # La bande prouvée perdante la PLUS BASSE fixe le plafond : tout ce qui est
    # au-dessus est au moins aussi douteux.
    lo, n, wins, be = min(proven_losing, key=lambda t: t[0])
    diag = (f"plafond de cote {lo:.2f} — la bande {lo:.2f}+ perd de façon prouvée "
            f"({_lecture_bande(wins, n, be)})")
    return lo, sum(t[1] for t in proven_losing), diag


def _edge_band_diagnostic(sport: str, rows: list[dict]) -> str | None:
    """
    Bucket decisive rows by initial_edge and log each bucket's real win
    rate — the founding assumption of this whole system (a bigger edge is
    more likely to be real, not noise) is otherwise never checked against
    actual outcomes. Diagnostic only, never a gate: per-bucket samples are
    too small to safely auto-act on (same reasoning as the Wilson-CI gate
    of the threshold verdict, _verdict_seuil). This exists to surface a systematic pattern —
    e.g. the top bucket, nearest SUSPECT_EDGE (core/constants.py), losing
    MORE than lower buckets, a classic sign of a data/team-matching error
    inflating the apparent edge rather than a real market inefficiency.

    Returns the warning line (also logged) if the top bucket underperforms,
    else None — the caller folds a non-None return into the operator-facing
    learning summary (see load_learning_summary).
    """
    decisive = [r for r in rows
                if r.get("outcome") in _DECISIVE_OUTCOMES and r.get("initial_edge") is not None]
    if len(decisive) < _SEGMENT_MIN_SAMPLES:
        return None

    bandes = []
    for lo, hi in _EDGE_BUCKETS:
        in_b = [r for r in decisive if lo <= r["initial_edge"] < hi]
        if len(in_b) < 5:
            continue
        wins = sum(1 for r in in_b if r["outcome"] == "WIN")
        odds_vals = [r["odds"] for r in in_b if r.get("odds")]
        be = p_breakeven(sum(odds_vals) / len(odds_vals), _TAX_RATE) if odds_vals else None
        marge = wins / len(in_b) - (be if be is not None else 0.0)
        bandes.append((lo, hi, len(in_b), wins, be, marge))
    if len(bandes) < 2:
        return None

    log.info("[%s] Bandes d'edge : %s", sport, " | ".join(
        f"{lo:.0f}-{hi:.0f}%: {_lecture_bande(w, n, be)}" for lo, hi, n, w, be, _m in bandes))

    # Alerte seulement si la bande haute PERD (sous son point mort) ET fait
    # pire que la meilleure des autres — comparées en MARGE, chaque bande a sa
    # cote. Le 2026-09-14 le digest disait « Edge 4-8%+ perd plus souvent
    # (71%, n=7) » d'une bande qui gagnait au-dessus de son point mort, parce
    # qu'elle gagnait moins que la meilleure (75 %).
    top, autres = bandes[-1], bandes[:-1]
    meilleure = max(autres, key=lambda b: b[5])
    if top[5] < 0 and top[5] < meilleure[5]:
        msg = (f"{ANOMALIE_MARQUEUR} [{sport}] Edge {top[0]:.0f}-{top[1]:.0f}%+ sous son point mort : "
               f"{_lecture_bande(top[3], top[2], top[4])} — meilleure tranche "
               f"{meilleure[0]:.0f}-{meilleure[1]:.0f}% : "
               f"{_lecture_bande(meilleure[3], meilleure[2], meilleure[4])} — "
               f"possible erreur de données/matching gonflant l'edge")
        log.warning(msg)
        return msg
    return None


def _depuis_ouverture(r: dict) -> dict:
    """La ligne vue à son PREMIER prix (sql/migrate_v10_21) : `sharp_prob` du
    ledger est celui du dernier rafraîchissement, qui réécrit le prix d'un
    fantôme basket promu à T-4h. Sans ouverture stockée (ligne d'avant la
    migration), la ligne garde son sharp_prob."""
    p = r.get("ouverture_sharp_prob")
    return {**r, "sharp_prob": p} if p is not None else r


def calibration_stats(rows: list[dict]) -> dict:
    """Le moteur annonçait-il juste ? Gagnés réels contre Σ sharp_prob, et
    dérive du sharp entre l'ouverture et la clôture (2026-10-23).

        z = (gagnés − attendus) / √Σ p(1−p)

    z < 0 : le sharp_prob d'entrée surestimait nos chances. La dérive
    réutilise _derive_stats (clôture oracle et clôture absente ignorées,
    règle 14), en % du prix, négative quand le marché est parti CONTRE la
    sélection. Plus rapide qu'un Wilson contre le point mort : le basket 6-24h
    sortait à z −2,68 sur 19 paris, quand son Wilson touchait encore le point
    mort. Lu par _verdict_seuil depuis le 2026-10-29 (critère de premier
    rang des seuils) ; la découpe par tranche (calibration_par_tranche) reste
    CONSULTATIVE."""
    lignes = [_depuis_ouverture(r) for r in rows
              if r.get("outcome") in _DECISIVE_OUTCOMES]
    preds = []
    for r in lignes:
        try:
            p = float(r.get("sharp_prob"))
        except (TypeError, ValueError):
            continue
        if 0 < p < 1:
            preds.append((p, 1 if r["outcome"] == "WIN" else 0))
    derive = _derive_stats(lignes)
    n = len(preds)
    gagnes = sum(o for _, o in preds)
    attendus = sum(p for p, _ in preds)
    var = sum(p * (1 - p) for p, _ in preds)
    return {"n": n, "gagnes": gagnes, "attendus": round(attendus, 1),
            "z": round((gagnes - attendus) / var ** 0.5, 2) if var > 0 else None,
            "derive_moy": None if derive["avg_clv"] is None else round(derive["avg_clv"], 2),
            "derive_n": derive["n"]}


def calibration_par_tranche(rows: list[dict]) -> dict[str, dict]:
    """calibration_stats par tranche de la zone jouable : « 2-6h », « 6-24h ».
    Coupe à _TRANCHE_COURTE_MAX_MINUTES, la borne de run_engine
    BASKET_AVANT_EFFECTIFS_MIN. Une ligne sans délai connu n'entre nulle part."""
    tranches: dict[str, list[dict]] = {"2-6h": [], "6-24h": []}
    for r in rows:
        try:
            t = float(r.get("time_to_match_minutes"))
        except (TypeError, ValueError):
            continue
        if _PLAYABLE_MIN_MINUTES <= t < _TRANCHE_COURTE_MAX_MINUTES:
            tranches["2-6h"].append(r)
        elif _TRANCHE_COURTE_MAX_MINUTES <= t <= _PLAYABLE_MAX_MINUTES:
            tranches["6-24h"].append(r)
    return {k: calibration_stats(v) for k, v in tranches.items()}


def _lecture_calibration(zone: dict, fantomes: dict) -> str:
    """« · annoncé 59% z +1.45 · fantômes z −0.40 (n=12) » : le réalisé lu
    CONTRE la probabilité annoncée (règle 7 : jamais un taux nu)."""
    txt = ""
    if zone.get("z") is not None and zone.get("n"):
        txt += f" · annoncé {zone['attendus'] / zone['n'] * 100:.0f}% z {zone['z']:+.2f}"
    if fantomes.get("z") is not None and fantomes.get("n"):
        txt += f" · fantômes z {fantomes['z']:+.2f} (n={fantomes['n']})"
    return txt


def _verdict_seuil(zone: list[dict], fantomes: list[dict],
                   derive_ref: float | None) -> tuple[str, str]:
    """« perte », « gain » ou « neutre » pour un sport ou un segment, et la
    raison lisible. SANS MÉMOIRE : la même preuve donne toujours le même
    verdict, quel que soit le seuil en place (2026-10-29).

    Le critère est le RÉALISÉ CONTRE L'ANNONCÉ (`calibration_stats`, z) : un
    moteur calibré gagne ce qu'il annonce, et c'est ce qu'il fait — 436
    recommandés post-époque, 58,7 % réalisés pour 58,4 % annoncés (z +0,14).
    Un segment ne mérite un autre seuil que s'il s'en écarte de façon PROUVÉE.
    La borne de Wilson contre le point mort reste une seconde preuve, la
    seule possible sur une ligne sans `sharp_prob`.

    Les FANTÔMES (même sport, hors zone jouable) servent de contre-épreuve :
    mesuré le 2026-10-29, presque tous les « motifs » de segment
    s'inversaient entre la zone et les fantômes (football sous 4 % d'edge :
    +21,8 u en zone, −22,7 u en fantômes). Un motif que les fantômes
    contredisent n'est pas retenu ; s'ils vont dans le même sens, ils
    s'ajoutent à la preuve.

    La dérive sharp ne décide plus d'aucune montée : biaisée de −0,64 %
    (voir _derive_excedent), elle a poussé le tennis de 2,0 à 6,0 pendant
    qu'il battait son annonce. Seul son EXCÉDENT significatif freine un
    abaissement."""
    stats = _sport_stats(zone)
    cz, cf = calibration_stats(zone), calibration_stats(fantomes)
    z_pool = calibration_stats(list(zone) + list(fantomes))["z"]
    be, lo, hi = stats["p_breakeven"], stats["wilson_lower"], stats["wilson_upper"]
    lecture = _lecture_stats(stats) + _lecture_calibration(cz, cf)

    # Sens du segment : z s'il existe, sinon réussite contre point mort.
    if cz["z"] is not None:
        sens = cz["z"]
    elif be is not None and stats["hit_rate"] is not None:
        sens = stats["hit_rate"] - be
    else:
        return "neutre", f"{lecture} — ni probabilité annoncée ni cote : hold"
    contre = (cf["z"] if cf["n"] >= _FANTOMES_MIN and cf["z"] is not None else None)

    if sens < 0:
        if contre is not None and contre > 0:
            return "neutre", (f"{lecture} : les fantômes contredisent — motif "
                              f"non retenu, seuil par défaut")
        if ((z_pool is not None and z_pool <= -_Z_PREUVE)
                or (hi is not None and be is not None and hi < be)):
            return "perte", f"{lecture} : perte prouvée → seuil relevé"
    elif sens > 0:
        if contre is not None and contre < 0:
            return "neutre", (f"{lecture} : les fantômes contredisent — motif "
                              f"non retenu, seuil par défaut")
        if ((z_pool is not None and z_pool >= _Z_PREUVE)
                or (lo is not None and be is not None and lo > be)):
            dx = _derive_excedent(zone, derive_ref)
            if dx["t"] is not None and dx["t"] <= _DERIVE_T_FREIN:
                return "neutre", (f"{lecture} : gain prouvé mais dérive excédentaire "
                                  f"{dx['excedent']:+.2f}% (t {dx['t']:+.1f}, n={dx['n']}) "
                                  f"— le marché ne confirme pas, seuil par défaut")
            return "gain", f"{lecture} : gain prouvé → seuil abaissé"
    return "neutre", f"{lecture} : ni perte ni gain prouvés — seuil par défaut"


def _cible_seuil(verdict: str, defaut: float) -> float | None:
    """La valeur que le verdict IMPOSE ; None = rien de posé, le défaut vaut.

    Une perte prouvée pose le plafond dur : le relèvement par pas ne servait
    à rien, l'edge ne prédisant pas le résultat au-delà de la probabilité
    annoncée (2026-10-29 : z ≈ 0 dans toutes les bandes d'edge). Un gain
    prouvé abaisse d'un cran sous le défaut — EV_EDGE_FLOOR reste le filet au
    chargement (run_engine._segment_min_edge)."""
    if verdict == "perte":
        return _THRESHOLD_MAX
    if verdict == "gain":
        return max(_THRESHOLD_MIN, round(defaut - _DETENTE, 2))
    return None


def _mouvement_permis(bases: dict, cle: str, n: int) -> bool:
    """Un seuil ne bouge qu'après _LIGNES_NEUVES_MIN paris réglés neufs.

    Le cliquet du 2026-09-15 (même échantillon rejoué à chaque audit) est un
    cas particulier : zéro ligne neuve. Celui du 2026-10-29 venait de l'autre
    bout — une ligne neuve suffisait à rejouer +0,4. Une clé sans base connue
    (jamais bougée par cette règle, ou posée par l'ancienne) se décide tout
    de suite : c'est ce qui réévalue les seuils hérités du cliquet. Un
    échantillon qui RÉTRÉCIT (archivage) n'est plus le même : décision
    permise."""
    base = bases.get(cle)
    if base is None:
        return True
    try:
        n0 = int(str(base).split(":", 1)[0])
    except ValueError:
        return True
    return n < n0 or n - n0 >= _LIGNES_NEUVES_MIN


_LEDGER_SELECT = ("signal_id, outcome, kelly_pct, odds, market_type, initial_edge, sharp_prob, "
                  "clv_pct_real, closing_pinnacle_price, closing_source, time_to_match_minutes, "
                  "created_at")
# Prix d'ouverture (sql/migrate_v10_21) : lu en plus quand la colonne existe ;
# sans elle, la lecture retombe sur _LEDGER_SELECT au lieu de casser l'audit.
_LEDGER_SELECT_OUVERTURE = _LEDGER_SELECT + ", ouverture_sharp_prob"
_LECTURE_MAX = 5000   # garde-fou de volume de compute_and_save, pas une fenêtre


def _dater_par_signal(sb, rows: list[dict]) -> list[dict]:
    """Pose `signal_created_at` (date d'ÉMISSION du signal) sur chaque ligne
    qui porte un `signal_id`, en lisant `signals` puis `signals_archive`.

    Pourquoi : `ai_learning_ledger.created_at` est la date du RÈGLEMENT, pas
    celle du signal. Mesuré le 2026-09-11 : 131 lignes réglées les 27-28/08
    (vague de backfill) portaient des signaux du 7 au 26 août — l'ancien
    moteur — et passaient pour post-époque ; en zone jouable elles font 26-6
    et posaient un faux plateau à 81 % en tête de toute courbe, d'où « le
    taux de pertes ne fait que baisser ». Un signal introuvable (30 lignes,
    purgées avant la règle d'archivage) reste NON daté : écarté des mesures
    appliquées, jamais compté comme preuve (post_correction_rows). Une
    lecture qui échoue fait de même, en le disant.
    """
    ids = sorted({r["signal_id"] for r in rows if r.get("signal_id") is not None})
    if not ids:
        return rows
    dates: dict = {}
    for table in ("signals", "signals_archive"):
        manquants = [i for i in ids if i not in dates]
        if not manquants:
            break
        try:
            res = sb.table(table).select("id,created_at").in_("id", manquants).execute()
            for s in (res.data or []):
                if s.get("created_at"):
                    dates[s["id"]] = s["created_at"]
        except Exception as e:
            log.warning("datation par signal (%s) : %s — les lignes non datées sont "
                        "ÉCARTÉES des mesures appliquées", table, e)
    for r in rows:
        if r.get("signal_id") is not None:
            r["signal_created_at"] = dates.get(r["signal_id"])
    return rows


# ── ÉPOQUE DE CALIBRATION (2026-08-27) ────────────────────────────────
# Tout ce que cette couche LOGGE peut se mesurer sur n'importe quelle ligne.
# Tout ce qu'elle fait APPLIQUER par le moteur — les plafonds d'edge et de
# cote, lus par run_engine._EDGE_CEILINGS/_ODDS_CEILINGS — ne le peut pas.
#
# Le 2026-08-27, A6 a corrigé le PRIX (prix exécutable) puis le PARI comparé
# (_meme_ligne : égalité exacte de ligne, signe compris). La distribution des
# edges d'AVANT décrit un moteur qui n'existe plus : « soccer au-dessus de
# 6 % perd » a été mesuré le 2026-08-02, dans une unité que la refonte EV du
# 2026-08-22 a changée. A6 a tranché « _EDGE_CEILINGS : rien à poser ».
#
# Cette couche l'a pourtant reposé le soir même (`edge_ceiling_soccer=6.0`,
# écrit à 19:28), parce que rien dans le code ne l'en empêchait : elle
# apprenait sur les 120 dernières lignes, toutes antérieures à la correction.
# Un plafond appris sur l'ancien moteur et appliqué au nouveau est exactement
# ce que la règle 10 de CLAUDE.md interdit — « aucun seuil numérique
# d'émission n'est modifié sans mesure sur des lignes réglées POSTÉRIEURES à
# la correction en cours ».
#
# D'où cette borne. Elle ne masque aucune ligne : le ledger reste entier, les
# diagnostics et les verdicts continuent de tout lire. Elle dit seulement de
# quoi on a le droit de faire une CONSIGNE. Elle se lève d'elle-même dès que
# le moteur corrigé a produit assez de résultats.
CALIBRATION_EPOCH = os.environ.get("CALIBRATION_EPOCH", "2026-08-27")


def post_correction_rows(rows: list[dict]) -> list[dict]:
    """Lignes réglées POSTÉRIEURES à la correction A6 (voir CALIBRATION_EPOCH).

    La date qui compte est celle du SIGNAL (`signal_created_at`, posée par
    _dater_par_signal), pas celle de la ligne de ledger, qui est la date du
    règlement : réglé après la correction ne veut pas dire émis par le moteur
    corrigé (2026-09-11 : 131 lignes du backfill des 27-28/08 datées
    post-époque pour des signaux d'août). Une ligne que personne n'a datée
    par signal (pas de clé) se lit encore par `created_at` : c'est le cas des
    lignes synthétiques et des lecteurs qui n'ont pas accès à `signals`.

    Une ligne sans date est ÉCARTÉE — contrairement à playable_rows,
    qui conserve l'inconnu. Le contrat est inverse ici : playable_rows filtre
    ce qu'on OBSERVE (jeter l'inconnu viderait l'historique), celle-ci filtre
    ce qu'on IMPOSE au moteur (garder l'inconnu ferait passer une ligne de
    l'ancien moteur pour une preuve).
    """
    def _date(r: dict) -> str:
        if "signal_created_at" in r:
            return r.get("signal_created_at") or ""
        return r.get("created_at") or ""
    return [r for r in rows if _date(r)[:10] >= CALIBRATION_EPOCH]


def playable_rows(rows: list[dict]) -> list[dict]:
    """Ne garder que les paris que le système recommande encore.

    Voir _PLAYABLE_MIN_MINUTES/_PLAYABLE_MAX_MINUTES pour la mesure qui
    justifie ces bornes. Une ligne sans `time_to_match_minutes` est CONSERVÉE :
    on ne peut pas prouver qu'elle est hors zone, et la jeter viderait
    l'apprentissage de tout l'historique antérieur à la colonne. Même principe
    que le dashboard avec un signal sans match_time.
    """
    kept = []
    for r in rows:
        t = r.get("time_to_match_minutes")
        if t is None:
            kept.append(r)
            continue
        try:
            t = float(t)
        except (TypeError, ValueError):
            kept.append(r)
            continue
        if _PLAYABLE_MIN_MINUTES <= t <= _PLAYABLE_MAX_MINUTES:
            kept.append(r)
    return kept


def _drop_stale_ceiling(sb, key: str, sport: str, ancienne, summary_lines: list[str],
                        nature: str = "plafond") -> None:
    """Retire une sortie APPLIQUÉE (plafond ou plancher appris) que les lignes
    postérieures à l'époque ne prouvent plus.

    Ne lève jamais : une clé qu'on n'arrive pas à effacer reste en place et
    sera re-tentée au prochain audit — on ne fait pas tomber l'apprentissage
    pour ça. Silencieux quand la clé n'existe pas (le cas nominal).
    """
    if ancienne is None:          # rien de posé : le cas nominal, aucun appel
        return
    try:
        sb.table("meta").delete().eq("key", key).execute()
        # Libellé neutre (2026-09-15) : la même fonction retire un plafond
        # mesuré sur l'ancien moteur ET un plafond que les réglés récents ne
        # prouvent pas (trop peu de paris, bande qui gagne). « mesure
        # pré-2026-08-27 » partait sur Telegram pour le plafond soccer posé
        # sur 5 paris du moteur COURANT : raison fausse.
        log.warning("[%s] %s %s=%s RETIRÉ — les réglés postérieurs au %s ne le "
                    "prouvent pas (règle 10)", sport, nature, key, ancienne,
                    CALIBRATION_EPOCH)
        summary_lines.append(f"{sport}: {nature} {key} retiré (non prouvé par les réglés récents)")
    except Exception as e:
        log.warning("[%s] retrait de %s impossible (%s) — il reste appliqué",
                    sport, key, e)


# v2 depuis le 2026-10-29 : les bases de l'ancienne règle (`learning_bases`,
# une empreinte par pas de +0,4) ne disent rien de la nouvelle. Une clé
# absente d'ici se décide tout de suite (_mouvement_permis), ce qui réévalue
# en un audit chaque seuil hérité du cliquet.
_BASES_KEY = "learning_bases_v2"


def _empreinte(rows: list[dict]) -> str:
    """Empreinte de l'ÉCHANTILLON sur lequel un seuil a bougé : (signal,
    issue, CLV réel) de chaque ligne. Même empreinte = rien de neuf appris."""
    items = sorted(f"{r.get('signal_id')}:{r.get('outcome')}:{r.get('clv_pct_real')}"
                   for r in rows)
    return f"{len(items)}:" + hashlib.sha1("|".join(items).encode()).hexdigest()[:16]


def _charger_bases(sb) -> dict[str, str]:
    """{clé de seuil: empreinte de l'échantillon du dernier mouvement}."""
    try:
        res = sb.table("meta").select("key,value").like("key", _BASES_KEY).execute()
        for r in res.data or []:
            if r.get("key") == _BASES_KEY:
                v = json.loads(r["value"])
                return v if isinstance(v, dict) else {}
    except Exception as e:
        log.warning("lecture de %s : %s — aucun mouvement antérieur connu", _BASES_KEY, e)
    return {}


def _decisives(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("outcome") in _DECISIVE_OUTCOMES]


def _poser_ou_retirer(sb, cle: str, sport: str, posee: float | None,
                      cible: float | None, n: int, empreinte: str, bases: dict,
                      raison: str, libelle: str, defaut: float,
                      lignes: list[str], now: str) -> float | None:
    """Applique la cible d'un seuil (None = aucune clé, le défaut vaut).
    Renvoie la valeur en vigueur APRÈS l'appel — l'ancienne si le mouvement
    est refusé (_mouvement_permis) ou si l'écriture échoue."""
    if cible == posee:
        return posee
    if not _mouvement_permis(bases, cle, n):
        log.info("[%s] %s tenu à %s : %s, mais moins de %d réglés neufs depuis le "
                 "dernier mouvement (%s)", sport, cle, posee, raison,
                 _LIGNES_NEUVES_MIN, bases.get(cle))
        return posee
    avant = posee if posee is not None else defaut
    try:
        if cible is None:
            sb.table("meta").delete().eq("key", cle).execute()
            lignes.append(f"{libelle}: seuil {avant:.2f}% → défaut {defaut:.2f}% ({raison})")
        else:
            sb.table("meta").upsert({"key": cle, "value": str(cible),
                                     "updated_at": now}).execute()
            lignes.append(f"{libelle}: seuil {avant:.2f}% → {cible:.2f}% ({raison})")
    except Exception as e:
        log.warning("[%s] %s non écrit (%s) — l'ancien reste en vigueur", sport, cle, e)
        return posee
    log.info("[%s] %s : %s → %s | %s", sport, cle, posee, cible, raison)
    bases[cle] = empreinte
    return cible


def compute_and_save(sb) -> dict[str, float]:
    """
    Re-compute thresholds from real WIN/LOSS history against the announced
    probability (see _verdict_seuil) and persist them to `meta`. Returns
    the updated PER-SPORT threshold dict unchanged in shape/keys from
    before (callers/tests keying off plain sport names keep working as-is).

    Also computes and persists a finer (sport, market-family) segment
    threshold layer (see load_segment_thresholds()) and logs an edge-band
    win-rate diagnostic — both additive, neither changes this function's
    return value.
    """
    current = load_thresholds(sb)
    updated = current.copy()
    # Plafonds réellement posés aujourd'hui : sert à ne retirer que ce qui
    # existe, sans un SELECT par sport et par clé à chaque audit.
    edge_ceilings_posed = load_edge_ceilings(sb)
    odds_ceilings_posed = load_odds_ceilings(sb)
    segment_current = load_segment_thresholds(sb)
    # Seuils réellement POSÉS en base (load_thresholds fond les défauts dans le
    # résultat et ne dit pas lesquels existent) : sert à ne retirer que ce qui
    # est posé, comme pour les plafonds.
    seuils_poses: set[str] = set()
    try:
        res_poses = sb.table("meta").select("key").like("key", "threshold_%").execute()
        seuils_poses = {r["key"] for r in (res_poses.data or [])}
    except Exception as e:
        log.warning("lecture des seuils posés : %s — aucun retrait ce tour", e)
    # LES CLIQUETS. 2026-09-15 : chaque audit rejouait la décision sur les
    # MÊMES lignes (`threshold_seg_baseball_totals` +0,4 par passage, jusqu'à
    # 4,6). 2026-10-29 : une seule ligne neuve suffisait à rejouer +0,4. Un
    # seuil ne bouge plus qu'après _LIGNES_NEUVES_MIN réglés neufs
    # (_mouvement_permis), et sa valeur ne dépend plus de la précédente.
    bases = _charger_bases(sb)
    bases_avant = dict(bases)
    now     = datetime.now(timezone.utc).isoformat()
    summary_lines: list[str] = []
    # Import tardif : core.odds_api importe ce module (zone jouable), les
    # deux ne peuvent pas se lire à l'import.
    from core.odds_api import sports_au_perimetre

    perimetre = sports_au_perimetre()
    ranking_stats: dict[str, dict] = {}   # sport -> stats, pour _save_sport_ranking
    clv_by_sport: dict[str, dict] = {}    # sport -> _clv_stats, pour les verdicts
    calib_by_sport: dict[str, dict] = {}  # sport -> calibration_par_tranche, consultatif
    all_preds: list[tuple[float, int]] = []   # calibration, tous sports confondus

    # Lecture de TOUS les sports avant toute décision (2026-10-29) : la dérive
    # sharp ne se lit plus que contre sa moyenne commune (_derive_excedent),
    # qui exige le ledger entier.
    lus: dict[str, list[dict]] = {}
    for sport in SPORT_DEFAULTS:
        try:
            # TOUTE l'époque, plus une fenêtre des N dernières lignes
            # (2026-09-27). La fenêtre de 120 comptait les FANTÔMES : le
            # football était jugé sur 65 paris réglés en zone, alors que 130
            # existaient depuis A6 — la moitié des preuves jetée, des
            # intervalles 1,4 fois trop larges. `created_at` du ledger est la
            # date de RÈGLEMENT, sur-ensemble de la date de signal :
            # post_correction_rows refiltre ensuite par signal. La borne
            # _LECTURE_MAX n'est qu'un garde-fou de volume.
            def _lire(colonnes: str):
                return (sb.table("ai_learning_ledger")
                        .select(colonnes)
                        .eq("sport", sport)
                        .gte("created_at", CALIBRATION_EPOCH)
                        .order("created_at", desc=True)
                        .limit(_LECTURE_MAX)
                        .execute())
            try:
                res = _lire(_LEDGER_SELECT_OUVERTURE)
            except Exception as e:
                log.warning("[%s] ledger sans prix d'ouverture (migration v10_21 ?) : %s",
                            sport, str(e)[:80])
                res = _lire(_LEDGER_SELECT)
            lus[sport] = _dater_par_signal(sb, res.data or [])
        except Exception as e:
            log.error("learning_layer [%s] lecture : %s", sport, e)
    toutes = [r for lignes in lus.values() for r in post_correction_rows(lignes)]
    derives_communes = _derive_valeurs(toutes)
    derive_ref = (sum(derives_communes) / len(derives_communes)
                  if len(derives_communes) >= _CLV_MIN_SAMPLES else None)
    if derive_ref is not None:
        log.info("dérive sharp de référence %+.2f%% sur %d clôtures (biais commun, "
                 "retranché de chaque segment)", derive_ref, len(derives_communes))

    for sport in SPORT_DEFAULTS:
        # Tampon par sport : ce qui est DIT à l'opérateur passe par le filtre
        # du périmètre en fin d'itération, ce qui est MESURÉ ne l'est jamais.
        lignes_sport: list[str] = []
        if sport not in lus:
            continue
        try:
            raw_rows = lus[sport]
            rows = playable_rows(raw_rows)
            if len(raw_rows) != len(rows):
                log.info("[%s] apprentissage sur %d/%d lignes (zone jouable %d-%dmin)",
                         sport, len(rows), len(raw_rows),
                         _PLAYABLE_MIN_MINUTES, _PLAYABLE_MAX_MINUTES)

            # Verdicts et classement : lignes POSTÉRIEURES à la correction
            # seulement (2026-09-10). Ils ne sont jamais appliqués, mais ils
            # sont MONTRÉS (learning_summary → Telegram, rapport hebdo) : un
            # basket « n=26 » venu d'août, sans une ligne recommandée depuis
            # le 27/08, classait un moteur qui n'existe plus (règle 10).
            appliquables = post_correction_rows(rows)
            if len(appliquables) != len(rows):
                log.info("[%s] plafonds, verdict, classement : %d/%d lignes "
                         "postérieures au %s (le reste décrit un moteur qui "
                         "n'existe plus)", sport, len(appliquables), len(rows),
                         CALIBRATION_EPOCH)
            stats = _sport_stats(appliquables)
            ranking_stats[sport] = stats
            clv_by_sport[sport] = _clv_stats(appliquables)
            calib_by_sport[sport] = calibration_par_tranche(appliquables)
            all_preds.extend(
                (r["sharp_prob"], 1 if r["outcome"] == "WIN" else 0)
                for r in rows
                if r.get("outcome") in _DECISIVE_OUTCOMES
                and r.get("sharp_prob") is not None)

            # Plafond d'edge : au-delà, l'edge mesuré n'est pas une inefficience
            # mais un prix mal apparié. Persisté avant la décision de seuil, qui
            # en dépend.
            # ── Ce qui est APPLIQUÉ se mesure APRÈS la correction ──────
            # Trois sorties de cette couche sont RESPECTÉES par le moteur :
            # les deux plafonds (run_engine._EDGE_CEILINGS/_ODDS_CEILINGS)
            # et les seuils threshold_* (run_engine._segment_min_edge → le
            # min_edge du scan). La version précédente de ce commentaire
            # prétendait que les seuils étaient « loggés, non appliqués » —
            # c'était FAUX, et threshold_soccer=5.6 (appris le 2026-08-24
            # sur les edges de l'ANCIEN moteur) a gaté l'émission du nouveau
            # jusqu'au 2026-08-28. Tout ce qui est appliqué ne se calcule
            # que sur des lignes postérieures à CALIBRATION_EPOCH — et depuis
            # le 2026-09-10 les verdicts et le classement aussi (ci-dessus) ;
            # seuls les diagnostics de bandes et la calibration lisent encore
            # le ledger entier.
            top_fake, ceiling, band_n, _meilleure = _top_band_verdict(appliquables)
            if top_fake and ceiling is not None:
                log.warning("[%s] Plafond d'edge %.1f%% — la bande haute perd plus "
                            "que les basses sur %d résultats", sport, ceiling, band_n)
                sb.table("meta").upsert({
                    "key":        f"edge_ceiling_{sport}",
                    "value":      str(ceiling),
                    "updated_at": now,
                }).execute()
                lignes_sport.append(
                    f"{sport}: plafond edge {ceiling:.1f}% (bande haute perdante, n={band_n})")
            else:
                # Gater les écritures FUTURES ne suffit pas : un plafond posé
                # avant l'époque resterait appliqué indéfiniment, cette couche
                # ne faisant que des upserts. Il est retiré, pas laissé.
                _drop_stale_ceiling(sb, f"edge_ceiling_{sport}", sport,
                                    edge_ceilings_posed.get(sport), lignes_sport)

            odds_cap, odds_n, odds_diag = _odds_band_verdict(appliquables)
            if odds_cap is not None:
                log.warning("[%s] %s", sport, odds_diag)
                sb.table("meta").upsert({
                    "key":        f"odds_ceiling_{sport}",
                    "value":      str(odds_cap),
                    "updated_at": now,
                }).execute()
                lignes_sport.append(f"{sport}: {odds_diag}")
            else:
                _drop_stale_ceiling(sb, f"odds_ceiling_{sport}", sport,
                                    odds_ceilings_posed.get(sport), lignes_sport)

            # Le seuil est appliqué par le scan : il ne se décide que sur les
            # lignes post-époque, et un seuil posé que ces lignes ne portent
            # plus est RETIRÉ (retour à SPORT_DEFAULTS) — même mécanique que
            # les plafonds, même incident d'origine (f30b317).
            cle_seuil = f"threshold_{sport}"
            # Contre-épreuve : les FANTÔMES du même sport (post-époque, hors
            # zone jouable) — voir _verdict_seuil.
            en_zone = {id(r) for r in rows}
            fantomes = post_correction_rows([r for r in raw_rows if id(r) not in en_zone])
            if stats["n"] < _MIN_SAMPLES:
                if cle_seuil in seuils_poses:
                    _drop_stale_ceiling(sb, cle_seuil, sport,
                                        current[sport], lignes_sport,
                                        nature="plancher appris")
                    updated[sport] = SPORT_DEFAULTS[sport]
                log.info("[%s] %d réglés post-%s < %d — seuil par défaut (%.1f%%)",
                         sport, stats["n"], CALIBRATION_EPOCH, _MIN_SAMPLES,
                         SPORT_DEFAULTS[sport])
            else:
                verdict, raison = _verdict_seuil(appliquables, fantomes, derive_ref)
                log.info("[%s] verdict de seuil : %s — %s", sport, verdict, raison)
                en_vigueur = _poser_ou_retirer(
                    sb, cle_seuil, sport,
                    current[sport] if cle_seuil in seuils_poses else None,
                    _cible_seuil(verdict, SPORT_DEFAULTS[sport]),
                    stats["n"], _empreinte(_decisives(appliquables)), bases, raison,
                    sport, SPORT_DEFAULTS[sport], lignes_sport, now)
                updated[sport] = en_vigueur if en_vigueur is not None else SPORT_DEFAULTS[sport]

            edge_warning = _edge_band_diagnostic(sport, rows)
            if edge_warning:
                lignes_sport.append(edge_warning)

            # Segment layer: same rows, sliced by market family (h2h/totals/
            # spreads) — a narrower question ("is THIS market within this
            # sport reliable?") than the sport-wide aggregate above.
            # Appliqué lui aussi (_segment_min_edge) : lignes post-époque
            # seulement, et un segment posé que ces lignes ne portent plus
            # est retiré comme le seuil de sport.
            by_family: dict[str, list[dict]] = {}
            for r in appliquables:
                fam = _market_family(r.get("market_type"))
                if fam in _MARKET_FAMILIES:
                    by_family.setdefault(fam, []).append(r)

            fams_posees = {k.split(":", 1)[1] for k in segment_current
                           if k.startswith(f"{sport}:")}
            for fam in sorted(set(by_family) | fams_posees):
                fam_rows = by_family.get(fam, [])
                fam_stats = _sport_stats(fam_rows)
                dict_key = f"{sport}:{fam}"
                meta_key = f"threshold_seg_{sport}_{fam}"
                if fam_stats["n"] < _SEGMENT_MIN_SAMPLES:
                    if dict_key in segment_current:
                        _drop_stale_ceiling(sb, meta_key, sport,
                                            segment_current[dict_key], lignes_sport,
                                            nature="plancher appris")
                    continue
                fam_fantomes = [r for r in fantomes
                                if _market_family(r.get("market_type")) == fam]
                seg_verdict, seg_raison = _verdict_seuil(fam_rows, fam_fantomes, derive_ref)
                log.info("[%s/%s] verdict de seuil : %s — %s", sport, fam, seg_verdict, seg_raison)
                # Un segment NEUTRE ne pose rien : il hérite du seuil du sport.
                _poser_ou_retirer(
                    sb, meta_key, sport, segment_current.get(dict_key),
                    _cible_seuil(seg_verdict, SPORT_DEFAULTS[sport]),
                    fam_stats["n"], _empreinte(_decisives(fam_rows)), bases, seg_raison,
                    f"{sport}/{fam}", updated[sport], lignes_sport, now)

            if sport in perimetre:
                summary_lines.extend(lignes_sport)
            elif lignes_sport:
                # Sport retiré du scan payant (core/odds_api.LIGUES_RETIREES) :
                # ses lignes restent réglées et apprises — utiles le jour d'une
                # réouverture — mais plus rien n'en est annoncé. Le 2026-09-20,
                # le digest criait une anomalie de bande d'edge sur le baseball
                # toutes les 2 h, trois jours après sa sortie du scan.
                log.info("[%s] hors périmètre — %d ligne(s) de résumé tues : %s",
                         sport, len(lignes_sport), " | ".join(lignes_sport))

        except Exception as e:
            log.error("learning_layer [%s]: %s", sport, e)

    # Verdicts promotion/rétrogradation (Phase 4) — loggés, jamais appliqués.
    try:
        summary_lines.extend(_save_sport_verdicts(sb, ranking_stats, clv_by_sport, now,
                                                  perimetre=perimetre,
                                                  calib_by_sport=calib_by_sport))
    except Exception as e:
        log.warning("sport verdicts: %s", e)

    if bases != bases_avant:
        try:
            sb.table("meta").upsert({
                "key":        _BASES_KEY,
                "value":      json.dumps(bases, sort_keys=True),
                "updated_at": now,
            }).execute()
        except Exception as e:
            log.warning("compute_and_save: %s non persisté (%s) — le prochain "
                        "audit peut rejouer le même mouvement", _BASES_KEY, e)

    _persister_resume(sb, summary_lines, now)

    _save_sport_ranking(sb, ranking_stats, now)
    _save_calibration_snapshot(sb, all_preds, now)
    return updated


def _save_calibration_snapshot(sb, preds: list[tuple[float, int]], now: str) -> None:
    """Une ligne d'historique de calibration par run, dans `brier_scores`.

    La table existait depuis sa création sans qu'aucun code Python n'y écrive :
    le Brier était recalculé à la volée pour /performance puis jeté, donc
    aucune série temporelle ne permettait de voir une dérive arriver. C'est
    précisément ce qui a laissé le drapeau de surconfiance rester levé pendant
    des semaines sans que personne ne puisse le constater.

    On persiste le score AVEC sa référence : un Brier seul n'est pas
    interprétable, son plancher vaut p(1-p) et dépend de la difficulté des
    paris. L'écart de calibration (annoncé − réalisé) est la mesure lisible.
    Périmètre : la zone jouable uniquement, cohérent avec le reste du module.
    """
    if len(preds) < _SEGMENT_MIN_SAMPLES:
        log.info("Calibration : %d paris < %d — pas d'instantané", len(preds),
                 _SEGMENT_MIN_SAMPLES)
        return
    score = brier_score(preds)
    ref   = brier_reference(preds)
    gap   = (sum(p for p, _ in preds) - sum(o for _, o in preds)) / len(preds)
    try:
        sb.table("brier_scores").insert({
            "brier_score":     score,
            "brier_reference": ref,
            "calibration_gap": round(gap, 4),
            "sample_size":     len(preds),
            "scope":           "playable",
            "computed_at":     now,
        }).execute()
        log.info("Calibration : Brier %.4f pour une référence de %.4f, écart "
                 "%+.1f pts sur %d paris", score, ref, 100 * gap, len(preds))
    except Exception as e:
        # Colonnes absentes = migration v10_2 pas encore appliquée : on
        # réessaie avec le schéma d'origine plutôt que de perdre la mesure.
        log.debug("brier_scores (schéma complet): %s", str(e)[:80])
        try:
            sb.table("brier_scores").insert({
                "brier_score": score,
                "sample_size": len(preds),
                "computed_at": now,
            }).execute()
        except Exception as e2:
            log.warning("_save_calibration_snapshot: %s", str(e2)[:80])


_RANKING_KEY = "sport_ranking"


def _save_sport_ranking(sb, stats_by_sport: dict[str, dict], now: str) -> None:
    """Persiste l'ordre des sports par réussite réelle, pour arbitrer les
    ressources rares côté scan (budget oracle, ordre du rapport).

    Trié sur `wilson_lower` et non sur `hit_rate` : le taux brut est du bruit
    aux volumes observés — un sport à 5 gagnés sur 5 affiche 100% et ne prouve
    rien face à 40 sur 83. La borne basse de Wilson répond à « quel est le pire
    taux compatible avec ce que j'ai vu ? », donc elle pénalise d'elle-même les
    petits échantillons, ce qui est exactement le critère pour trancher entre
    deux sports quand il n'y a de budget que pour un.

    Seuls les sports atteignant _MIN_SAMPLES entrent : classer un sport sur 5
    résultats produirait un ordre qui change à chaque audit. Les autres restent
    absents, et run_engine les traite en position neutre — un sport sans
    historique est INCONNU, pas mauvais, et le reléguer l'empêcherait
    justement d'acquérir l'historique qui le départagerait.
    """
    ranked = sorted(
        ((s, st) for s, st in stats_by_sport.items()
         if st.get("n", 0) >= _MIN_SAMPLES and st.get("wilson_lower") is not None),
        key=lambda kv: kv[1]["wilson_lower"], reverse=True,
    )
    order = [s for s, _ in ranked]
    try:
        sb.table("meta").upsert({
            "key":        _RANKING_KEY,
            "value":      json.dumps(order),
            "updated_at": now,
        }).execute()
        if order:
            log.info("Classement sports (Wilson bas, n>=%d) : %s",
                     _MIN_SAMPLES, " > ".join(order))
        else:
            log.info("Classement sports : aucun sport n'atteint %d résultats "
                     "décisifs — ordre laissé vide, run_engine garde son défaut.",
                     _MIN_SAMPLES)
    except Exception as e:
        log.warning("_save_sport_ranking: %s", e)


def load_sport_ranking(sb) -> list[str]:
    """Ordre des sports par réussite, écrit par _save_sport_ranking.

    Renvoie [] si absent/illisible : l'appelant doit alors garder son ordre
    par défaut plutôt que de deviner.
    """
    try:
        res = (sb.table("meta").select("value")
               .eq("key", _RANKING_KEY).maybe_single().execute())
        if res and res.data:
            order = json.loads(res.data["value"])
            if isinstance(order, list):
                return [str(s) for s in order]
    except Exception as e:
        log.debug("load_sport_ranking: %s", e)
    return []
