"""
core/odds_api.py — PAIM v8.3 — The Odds API (Hunter Multi-Sport Mode)
Markets: h2h | spreads | totals selon le sport

QUOTA — ne rien recopier ici, ce chiffre a déjà divergé trois fois. La seule
source de vérité est l'en-tête `x-requests-remaining` des réponses, loggé à
chaque appel servi. Constat du 2026-08-01 : le plan réel était à **500
requêtes/mois** (et non 20 000 comme l'annonçait ce docstring), pour une
consommation mesurée de ~16 crédits/heure — un mois de quota brûlé en 30
heures, deux mois de suite.

DÉCISION OPÉRATEUR (2026-08-01) : on laisse couler. Pas de rationnement, pas
de garde local — la clé se vide, on en change. Un gouverneur mensuel a été
écrit puis retiré : rendre des scans stériles pour étaler un budget que
l'opérateur préfère dépenser à fond n'avait pas de sens de son point de vue.

Reste le PRÉ-VOL GRATUIT (`_events_in_window`), qui n'est PAS du
rationnement : il ne refuse jamais un scan utile, il évite seulement de
payer une ligue qui n'a aucun match dans la fenêtre — la réponse aurait été
vide de toute façon. Il fait donc durer la même couverture plus longtemps,
sans jamais la réduire.
"""
import logging
import os
import re
import requests
from datetime import datetime, timedelta, timezone

from core.constants import EXECUTION_BOOKS
from core.execution_books import fusionner_lignes, ordre
from core.math_engine import nul_cote
from core.secret_store import get_secret
# Borne T-2h du fantôme (core/learning_layer, règle n°6 : une seule copie).
from core.learning_layer import _PLAYABLE_MIN_MINUTES as PLAYABLE_MIN_MINUTES

# Retard de livraison des crons GitHub, mesuré jusqu'à ~40 min : un match à
# T-2h05 au moment du pré-vol sera à T-1h30 quand le signal sortira.
PLAYABLE_MARGIN_MIN = int(os.environ.get("ODDS_API_PLAYABLE_MARGIN_MIN", "30"))

log = logging.getLogger("PREDATOR.odds_api")

BASE_URL     = "https://api.the-odds-api.com/v4"
PINNACLE_KEY = "pinnacle"
XBET_KEY     = "onexbet"
# Clé OddsAPI de chaque book d'exécution (core.constants.EXECUTION_BOOKS).
# Un book absent d'ici n'est pas demandé à OddsAPI : Bet365 n'y existe qu'en
# version australienne (`bet365_au`), dont les lignes ne sont pas celles que
# l'opérateur voit — il est servi par odds-api.io et titan007.
ODDS_API_BOOK_KEYS = {"1xbet": XBET_KEY}
CIRCA_KEY    = "circa"        # Circa Sports — sharp US book
CRIS_KEY     = "bookmaker"    # Bookmaker.eu — CRIS network

# ── Sport keys actifs — sélection RENTABILITÉ MAXIMALE ───────────────
# Critères de sélection :
#   1. Lag Pinnacle→Melbet documenté (source d'edge réel)
#   2. Kelly fraction élevée (≥ 0.18 — confiance marché)
#   3. Volume quotidien suffisant (≥ 3 matchs/jour en moyenne)
#   4. Données Pinnacle + Melbet confirmées disponibles
#
# EXCLUS (signal/bruit trop faible) :
#   - Cricket / Darts (Kelly 0.10–0.15, marchés peu efficients)
#   - Ligues Scandi / Irlande / Chine / Japon / Corée soccer (lag faible, volumes bas)
#   - Copa Sudamericana / Brazil B / Chile / Colombia (Pinnacle peu liquide)
#   (Mis à jour le 2026-08-22 : la boxe est entrée en Phase 1, l'Argentine est
#    scannée, et le tennis — exclu pour une raison SAISONNIÈRE, la transition
#    gazon — est servi depuis la Phase 3 par des clés dynamiques, voir
#    discover_tennis_keys. Un bloc d'exclusions qu'on ne relit pas contredit
#    le dictionnaire qu'il précède.)
#
# Budget : voir reports/refonte_scope_2026-08.md §4 — le chiffre d'origine
# (« ~2 640/mois pour 11 clés ») a dérivé deux fois, on ne le recopie plus ici.
# L'ORDRE DE CE DICTIONNAIRE EST PORTEUR (2026-09-17) : le plafond de dépense
# d'un scan de fond (allocation × créneaux dus/8 × BACKGROUND_SHARE) ne suffit
# pas à payer toutes les ligues peuplées, et c'est cet ordre qui décide
# lesquelles. Il était trié par NOMBRE DE MATCHS : MLB et ses 9 matchs
# passaient devant La Liga et ses 2, donc devant le seul segment que le ledger
# démontre (Big 5 : 25-5, +10,17 u, borne basse de Wilson 66 % pour un point
# mort à 61 %). Le volume ne départage plus que DANS une famille — voir le tri
# dans fetch_odds, gardé par tests/test_ordre_de_depense.py.
# Chiffres HORS TAXE (TAX_RATE=0, décision opérateur — règle 11).
SPORT_KEYS = {
    # (Retiré 2026-08-06 — la Coupe du Monde 2026 est terminée, instruction
    # opérateur. Elle occupait la priorité 1 ; la ligue ne rend plus que des
    # 404 hors saison, mais la garder gardait aussi vivant le calendrier 168h
    # de run_engine.py, supprimé dans le même commit.)

    # ── PRIORITÉ 1 — Big 5 européen ──────────────────────────────────
    # MESURÉ le 2026-09-17 sur les recommandés jouables post-A6 : 25-5
    # (83 %), borne basse de Wilson 66 % pour un point mort à 61 % — le SEUL
    # segment du livre dont la borne BASSE passe le point mort. Hors Big 5 :
    # 29-20 (59 %), +1,11 u, rien de démontré. C'est la raison de la première
    # place ; elle tombe si la mesure tombe (n=30, à relire à 60 réglés).
    "soccer_epl":                            "soccer",      # EPL — reprise 21/08, Pinnacle+1xBet ✓
    "soccer_spain_la_liga":                  "soccer",      # La Liga — reprise 16/08, Pinnacle+1xBet ✓
    "soccer_germany_bundesliga":             "soccer",      # Bundesliga — reprise 28/08, Pinnacle+1xBet ✓
    "soccer_italy_serie_a":                  "soccer",      # Serie A — reprise 22/08, Pinnacle+1xBet ✓
    "soccer_france_ligue_one":               "soccer",      # Ligue 1 — reprise 22/08, Pinnacle+1xBet ✓

    # ── PRIORITÉ 2 — coupes d'Europe (même famille de marchés) ───────
    # Placées derrière le Big 5 le 2026-09-17 : même écosystème sharp, mais
    # n=6 au ledger (UEL 3-0, LdC 1-2) — rien de démontré, un voisinage.
    "soccer_uefa_champs_league":             "soccer",                 # LdC — phase de ligue mi-sept.
    "soccer_uefa_europa_league":             "soccer",                 # UEL — idem

    # ── PRIORITÉ 3 — Playoffs/saisons Amérique du Nord (sharps) ──────
    # (baseball_mlb retiré le 2026-09-17, voir le bloc RETIRÉS plus bas)
    "basketball_nba":                        "basketball",  # NBA Finals — marché le + sharp au monde
    # (basketball_wnba retirée le 2026-09-24, voir le bloc RETIRÉS plus bas)
    "icehockey_nhl":                         "hockey",      # NHL Stanley Cup Finals — mouvement max

    # ── PRIORITÉ 4 — Amérique du Sud (lag SA soirée) ─────────────────
    "soccer_conmebol_copa_libertadores":     "soccer",      # R16/QF — lag SA maximal documenté
    "soccer_brazil_campeonato":              "soccer",      # Série A Brésil — marché sharp actif
    "soccer_usa_mls":                        "soccer",      # MLS — volumes élevés, 1XBet actif
    # (soccer_argentina_primera_division retirée le 2026-10-09, voir le bloc RETIRÉS plus bas)
    "soccer_mexico_ligamx":                  "soccer",      # Liga MX — actif été

    # ── PRIORITÉ 5 — Australie (marchés Pinnacle très sharps) ────────
    "aussierules_afl":                       "aussierules", # AFL — ~9 matchs/semaine, Pinnacle ✓
    "rugbyleague_nrl":                       "rugbyleague", # NRL — ~8 matchs/semaine, Pinnacle ✓

    # ── PRIORITÉ 6 — football US (saison régulière seulement) ────────
    # NFL : présaison ACCEPTÉE depuis le 2026-10-06 (décision opérateur,
    # voir SEASON_OPENS).
    "americanfootball_nfl":                  "americanfootball",       # NFL — saison régulière uniquement
    # Football universitaire (NCAAF) : RETIRÉ le 2026-10-06, décision
    # opérateur — voir LIGUES_RETIREES. La NFL reste achetée.

    # ── PRIORITÉ 7 — Euroleague basket ───────────────────────────────
    "basketball_euroleague":                 "euroleague_basketball",  # mécaniques basketball, Kelly dédiée

    # ── PRIORITÉ 8 — sports de combat (flux OddsAPI réel, Phase 1) ───
    # Le MMA était pricé par recherche web (fetch_mma_events, supprimé) : son
    # +37,5% de ROI sur 8 paris n'était validable par aucun CLV réel. h2h
    # seulement (_MARKETS_BY_SPORT). Les semaines sans carte ne coûtent rien :
    # le pré-vol _events_in_window (0 crédit) rend 0 et la ligue est sautée.
    "mma_mixed_martial_arts":                "mma",         # UFC/PFL/Bellator — cartes ven-dim

    # ── PRIORITÉ 9 — élargissement du foot (2026-09-22, décision opérateur) ──
    # DERNIÈRE famille, et c'est voulu : ces ligues n'ont AUCUNE ligne au
    # ledger. Elles ne doivent jamais préempter le budget d'une famille
    # mesurée — un jour saturé les refuse en premier, par construction de
    # `rangs_par_famille`.
    # POURQUOI maintenant : mesuré le 2026-09-22, 10 des 11 ligues de foot
    # scannées avaient ZÉRO match sur 7 jours (trêve internationale) — seules
    # MLS et Liga MX jouaient. Le moteur n'était pas en panne, son univers
    # dormait. Budget et couverture de règlement : voir LIGUES_EN_ESSAI.
    "soccer_uefa_nations_league":            "soccer",      # 41 matchs/7j — sélections A
    "soccer_england_league1":                "soccer",      # D3 anglaise — samedi 14h UTC
    "soccer_england_league2":                "soccer",      # D4 anglaise — samedi 14h UTC
    "soccer_spain_segunda_division":         "soccer",      # D2 espagnole — ven-lun
    "soccer_brazil_serie_b":                 "soccer",      # D2 brésilienne — soirées 19-23h UTC
    # ── Second élargissement (2026-10-01, décision opérateur) ────────
    # Même famille, même rang : dernières servies. MESURÉ ce jour-là : la
    # demande réelle d'OddsAPI (`meta.oddsapi_demande_<jour>`) tournait à ~90
    # crédits/jour pour ~145 d'allocation, soit un tiers du pool perdu à la
    # recharge. `ops.py books` (1 crédit par ligue) : Pinnacle ET 1xbet sur
    # 12/12, 9/9, 9/9, 8/9, 9/9, 9/9 et 10/10 de leurs matchs — le Portugal,
    # sondé en même temps, n'a pas 1xbet (0/9) et n'entre pas.
    "soccer_efl_champ":                      "soccer",      # D2 anglaise — sam 11h30/14h, soirs 18h45
    "soccer_netherlands_eredivisie":         "soccer",      # Pays-Bas — ven soir, sam 14-19h, dim 10-15h
    "soccer_belgium_first_div":              "soccer",      # Belgique — ven soir, sam 14-19h, dim 11-17h
    "soccer_turkey_super_league":            "soccer",      # Turquie — 10h30, 13h, 16h, 17h UTC
    "soccer_germany_bundesliga2":            "soccer",      # D2 allemande — ven 16h30, sam/dim 11h, sam 18h30
    "soccer_france_ligue_two":               "soccer",      # D2 française — ven 18h, sam 12h, lun 18h45
    "soccer_italy_serie_b":                  "soccer",      # D2 italienne — sam/dim 13-17h, ven 18h30
    # (boxing_boxing retirée le 2026-09-17 : aucune source de scores n'existe,
    #  donc la politique de dépense ne pouvait PAS la payer — voir le bloc
    #  RETIRÉES ci-dessous. Elle ne coûtait que des pré-vols gratuits, mais
    #  elle faisait mentir cette liste.)
}

# Frontières de FAMILLE : la première clé de chaque bloc « PRIORITÉ n »
# ci-dessus. Le rang d'une ligue est l'indice de sa famille, et le volume de
# matchs départage DANS la famille — la logique d'origine (un 422 coupe sur la
# ligue la moins fournie) survit là où elle a du sens, sans laisser un sport à
# 9 matchs préempter le budget d'une famille mieux mesurée. Huit noms, pas une
# seconde liste de ligues : chacun doit exister dans SPORT_KEYS (gardien
# tests/test_ordre_de_depense.py).
DEBUTS_DE_FAMILLE = (
    "soccer_epl",                          # 1. Big 5
    "soccer_uefa_champs_league",           # 2. coupes d'Europe
    "basketball_nba",                      # 3. Amérique du Nord
    "soccer_conmebol_copa_libertadores",   # 4. Amérique du Sud
    "aussierules_afl",                     # 5. Australie
    "americanfootball_nfl",                # 6. football US
    "basketball_euroleague",               # 7. Euroleague
    "mma_mixed_martial_arts",              # 8. sports de combat
    "soccer_uefa_nations_league",          # 9. élargissement foot (non mesuré)
)


def rangs_par_famille(cles=None) -> dict[str, int]:
    """clé de ligue → indice de sa famille, dérivé de l'ORDRE de SPORT_KEYS.
    Une clé inconnue (tennis dynamique) n'a pas de famille : l'appelant la
    met en dernier."""
    rangs, famille = {}, -1
    for cle in (cles if cles is not None else SPORT_KEYS):
        if cle in DEBUTS_DE_FAMILLE:
            famille += 1
        rangs[cle] = max(famille, 0)
    return rangs


# Ligues EN ESSAI : entrées avec leur budget chiffré et leur critère de
# retrait DATÉ, comme l'exige la règle 13 (AUDIT.md §3bis). Une ligue qui ne
# peut pas énoncer ce qui la fera sortir n'a pas de raison d'entrer.
#
# Budget MESURÉ le 2026-09-22 par `ops.py ligues soccer` (0 crédit) et par le
# rejeu des fenêtres proposées sur les coups d'envoi réels des 10 jours
# suivants : **18,6 crédits/jour pour les cinq**, quand le pool laissait
# 50 à 90 crédits inutilisés par jour et s'apprêtait à en perdre ~400 en fin
# de cycle. Couverture de règlement vérifiée sur les 6 prochains matchs de
# chaque ligue : 6/6 pour les cinq (ESPN `soccer/all`, LiveScore en repli).
#
# ⚠️ Le bloc d'exclusions historique en tête de ce module écartait « Brazil B »
# pour cause de « Pinnacle peu liquide » (août 2026). La référence sharp n'est
# plus Pinnacle seul depuis Matchbook et Smarkets — mais c'est exactement ce
# que le critère « < 20 % des matchs avec prix sharp » va trancher, ligue par
# ligue, sur des lignes réglées.
LIGUES_EN_ESSAI: dict[str, str] = {
    "soccer_uefa_nations_league":
        "2026-09-22, ~5.4 créd/j — retrait le 2026-10-20 si < 20 % des matchs "
        "avec prix sharp, ou sous le point mort sur 30 réglés",
    "soccer_england_league1":
        "2026-09-22, ~0.9 créd/j — retrait le 2026-10-20 si < 20 % des matchs "
        "avec prix sharp, ou sous le point mort sur 30 réglés",
    "soccer_england_league2":
        "2026-09-22, ~1.8 créd/j — retrait le 2026-10-20 si < 20 % des matchs "
        "avec prix sharp, ou sous le point mort sur 30 réglés",
    "soccer_spain_segunda_division":
        "2026-09-22, ~3.6 créd/j — retrait le 2026-10-20 si < 20 % des matchs "
        "avec prix sharp, ou sous le point mort sur 30 réglés",
    "soccer_brazil_serie_b":
        "2026-09-22, ~6.9 créd/j — retrait le 2026-10-20 si < 20 % des matchs "
        "avec prix sharp, ou sous le point mort sur 30 réglés",
    # ── Second lot, 2026-10-01 ────────────────────────────────────────
    # Budget MESURÉ par le rejeu de la VRAIE politique de dépense
    # (`scan_windows.SpendPolicy`, plafond par créneau, poids par jour, ordre
    # des familles) sur les coups d'envoi réels du 2 au 15 octobre (flux
    # `events`, 0 crédit) : **156 crédits la semaine de matchs pour les sept,
    # soit ~22,7/jour**. Semaine du 9 au 15/10, tout compris : 636 crédits
    # engagés sur 1 286 alloués ; pire jour à 82 % de son allocation. Le
    # quota ne peut pas être dépassé (allocation = pool ÷ jours restants).
    # CE QUE ÇA COÛTE AUX AUTRES, mesuré dans le même rejeu : aucun achat en
    # fenêtre favorable d'une ligue déjà scannée n'est perdu (566 crédits sur
    # 566) ; leurs achats DE FOND reculent de 39 crédits sur 14 jours (−9 %),
    # parce que le plafond du fond se compare à la dépense TOTALE du jour.
    # Couverture de règlement : 6/6 sur les prochains matchs de chacune
    # (`ops.py ligues soccer 14` — ESPN `soccer/all`, LiveScore en repli).
    "soccer_efl_champ":
        "2026-10-01, ~3.0 créd/j — retrait le 2026-11-15 si < 20 % des matchs "
        "avec prix sharp, ou sous le point mort sur 30 réglés",
    "soccer_netherlands_eredivisie":
        "2026-10-01, ~3.9 créd/j — retrait le 2026-11-15 si < 20 % des matchs "
        "avec prix sharp, ou sous le point mort sur 30 réglés",
    "soccer_belgium_first_div":
        "2026-10-01, ~4.3 créd/j — retrait le 2026-11-15 si < 20 % des matchs "
        "avec prix sharp, ou sous le point mort sur 30 réglés",
    "soccer_turkey_super_league":
        "2026-10-01, ~4.7 créd/j — retrait le 2026-11-15 si < 20 % des matchs "
        "avec prix sharp, ou sous le point mort sur 30 réglés",
    "soccer_germany_bundesliga2":
        "2026-10-01, ~2.1 créd/j — retrait le 2026-11-15 si < 20 % des matchs "
        "avec prix sharp, ou sous le point mort sur 30 réglés",
    "soccer_france_ligue_two":
        "2026-10-01, ~1.7 créd/j — retrait le 2026-11-15 si < 20 % des matchs "
        "avec prix sharp, ou sous le point mort sur 30 réglés",
    "soccer_italy_serie_b":
        "2026-10-01, ~3.0 créd/j — retrait le 2026-11-15 si < 20 % des matchs "
        "avec prix sharp, ou sous le point mort sur 30 réglés",
}


# Ligues RETIRÉES du scan payant, avec leur date et leur raison. Rien n'est
# effacé ailleurs : les lignes de `signals`/`ai_learning_ledger` restent, leur
# règlement continue (MLB statsapi pour le baseball), et les fenêtres
# favorables de core/scan_windows sont conservées telles quelles pour une
# réouverture. Gardien : tests/test_ordre_de_depense.py.
LIGUES_RETIREES: dict[str, str] = {
    # DÉCISION OPÉRATEUR du 2026-09-17 (option « C »), sur mesure du jour :
    # baseball recommandé+jouable post-A6 = 14-16 (46,7 %) pour un point mort
    # à 53 %, soit −3,56 u en mise plate ; la perte est concentrée sur
    # totals_under (5-11, −6,13 u) quand totals_over rapporte +2,70 u. Le sport
    # consommait ~14 crédits OddsAPI par jour sur les 53 engagés — payés
    # AVANT le Big 5 parce qu'il a plus de matchs. Réouverture : instruction
    # opérateur, ou si le Big 5 retombe sous son point mort sur 60 réglés.
    # Précédent : fantôme du 2026-08-04 (48 paris, 42 %) levé le 2026-09-01
    # « à réévaluer après 30 réglés post-époque » — les 30 sont faits.
    "baseball_mlb": "2026-09-17 — décision opérateur : −3,56 u sur 30 réglés (46,7 % pour 53 % requis)",
    "baseball_kbo": "2026-09-17 — retirée avec le baseball (sport entier)",
    "baseball_npb": "2026-09-17 — retirée avec le baseball (sport entier)",
    # MESURÉ le 2026-09-17 : ESPN n'a AUCUN chemin de boxe (`boxing/boxing`
    # rend HTTP 400), donc `score_sources.sports_reglables()` exclut la boxe,
    # donc `SpendPolicy` refusait déjà de la payer — 0 signal de boxe émis
    # depuis toujours, 0 crédit dépensé, et un pré-vol gratuit brûlé à chaque
    # scan. Elle revient le jour où une source de scores de boxe existe.
    # DÉCISION OPÉRATEUR du 2026-09-24 (« WNBA perd trop, suspends-la
    # définitivement »). Mesure du jour, zone jouable, non shadow, mise plate :
    # 43 décidés, 22-21, −2,26 u, dont 1-3 (−2,08 u) sur les 10 derniers
    # jours. Wilson IC95 [37–65 %] : PAS une preuve de perte (règle 7), une
    # décision opérateur (règle 11), comme l'Argentine du 2026-09-08. Le
    # retrait de la clé coupe la dépense OddsAPI ; le motif « wnba » de
    # `meta.perimetre_ligues_exclues` écarte les libellés des autres sources.
    "basketball_wnba": "2026-09-24 — décision opérateur : 22-21, −2,26 u sur 43 réglés en zone jouable",
    # DÉCISION OPÉRATEUR du 2026-10-09 (règle 11). La Primera División
    # argentine est exclue du périmètre depuis le 2026-09-08 par
    # `meta.perimetre_ligues_exclues` (INCIDENTS.md), mais sa clé restait
    # ACHETÉE : chaque match payé était écarté à l'émission. Mesuré le
    # 2026-10-09 : `meta.scan_paid_soccer_argentina_primera_division` posé au
    # scan de 09:11 UTC, 4 crédits, 0 signal possible. Le retrait de la clé
    # coupe la dépense ; les motifs de la clé meta restent, ils écartent les
    # libellés des autres sources.
    "soccer_argentina_primera_division": "2026-10-09 — décision opérateur : exclue du périmètre depuis le 2026-09-08, payée pour rien",
    # DÉCISION OPÉRATEUR du 2026-10-06 (« Supprimer ncaa, garde nfl »), règle
    # 11 : une décision de périmètre, PAS une preuve de perte (règle 7 — sept
    # lignes réglées en base ce jour-là, rien de mesurable). Le retrait de la
    # clé coupe le pré-vol et l'achat différé OddsAPI ; le vrai chemin
    # d'émission était odds-api.io (OddsAPI n'y cote pas 1xbet), fermé le même
    # jour dans `core.odds_api_io.ROUTAGE_PAR_LIGUE`. Le sport-type
    # `college_football` reste partout ailleurs (Kelly, seuil, affichage,
    # ESPN) : les lignes passées se lisent et se règlent toujours (règle 9).
    "americanfootball_ncaaf": "2026-10-06 — décision opérateur : NCAA retirée, NFL conservée",
    "boxing_boxing": "2026-09-17 — aucune source de scores (ESPN 400), jamais payable, 0 signal émis",
}


def sports_payes() -> frozenset:
    """Les sport-types que le scan peut réellement payer. Dérivé de
    SPORT_KEYS — jamais tenu à la main (règle 6)."""
    return frozenset(SPORT_KEYS.values())


def sports_au_perimetre() -> frozenset:
    """Les sport-types que le moteur émet encore aujourd'hui : ceux de
    SPORT_KEYS, plus le tennis dont les clés sont DYNAMIQUES (un tournoi
    n'existe que quelques jours, voir discover_tennis_keys) et n'apparaissent
    donc jamais dans la table statique.

    Sert à ne plus parler à l'opérateur d'un sport RETIRÉ (LIGUES_RETIREES) :
    le 2026-09-20 le digest criait toutes les 2 h une anomalie de bande
    d'edge sur le baseball, sorti du scan payant le 2026-09-17 — un diagnostic
    sur un sport qu'on n'achète plus ne peut plus rien corriger. Les lignes
    continuent d'être réglées, mesurées et apprises : c'est la PAROLE qui
    s'arrête, pas la mesure (règle n°9).
    """
    return sports_payes() | {TENNIS_SPORT}

# ── Ouverture de saison : une ligue n'est pas scannée avant cette date ───
# Le pré-vol ne distingue pas présaison et saison régulière : un match NFL
# d'août est un match. La présaison NFL était refusée jusqu'au 2026-10-06
# (lignes molles, rotations imprévisibles — instruction opérateur d'alors).
# DÉCISION OPÉRATEUR du 2026-10-06, règle 11 (« NFL présaison accepté ») : plus
# de date par défaut, donc plus de garde. Le mécanisme reste : poser
# NFL_SEASON_START=YYYY-MM-DD dans l'environnement referme la porte sans
# toucher au code ; aucune date = pas de garde.
SEASON_OPENS: dict[str, str] = {
    "americanfootball_nfl": os.environ.get("NFL_SEASON_START", ""),
}


def _season_open(sport_key: str, now: datetime) -> bool:
    """False si la ligue a une date d'ouverture et qu'on est avant (0 crédit)."""
    raw = SEASON_OPENS.get(sport_key)
    if not raw:
        return True
    try:
        opens = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
    except ValueError:
        log.warning("SEASON_OPENS[%s]=%r illisible — garde ignoré", sport_key, raw)
        return True
    return now >= opens


# ── Tennis : clés OddsAPI DYNAMIQUES (Phase 3, 2026-08-22) ──────────────
# OddsAPI ne sert pas le tennis comme un sport permanent : chaque tournoi a
# sa propre clé, qui apparaît quelques jours avant et disparaît après
# (`tennis_atp_cincinnati_open`, `tennis_wta_us_open`…). Une entrée statique
# dans SPORT_KEYS serait morte onze mois sur douze et fausse le douzième.
#
# On résout donc les clés au DÉBUT de chaque fetch_odds, via GET /v4/sports —
# endpoint GRATUIT, déjà utilisé par le pool pour sonder les clés — filtrées
# sur une liste blanche de tournois : les quatre Grands Chelems, les
# Masters / WTA 1000 et, depuis le 2026-09-30, les ATP / WTA 500 (décision
# opérateur, voir la liste). Un Challenger ou un ATP 250 n'a ni la
# liquidité ni le lag : exclu.
#
# Pourquoi le tennis, et pourquoi maintenant : toute l'infrastructure existe
# depuis des mois (marchés h2h+totals ci-dessous, Matchbook id 9 demandé en
# prod, odds-api.io, poids de consensus dédiés, contexte de settlement) — il
# ne manquait que les clés. L'exclusion historique (« saison de transition
# gazon ») était saisonnière, pas structurelle.
#
# Coupe-circuit : TENNIS_DYNAMIC=0 dans l'environnement rend {} sans appel.
TENNIS_TOURNAMENTS: tuple = (
    # Grands Chelems (slugs OddsAPI : aus_open_singles, french_open, wimbledon, us_open)
    "aus_open", "french_open", "wimbledon", "us_open",
    # Masters 1000 / WTA 1000
    "indian_wells", "miami", "monte_carlo", "madrid", "italian_open", "rome",
    "canadian", "canada", "cincinnati", "shanghai", "paris_masters",
    "china_open", "wuhan", "dubai", "qatar", "doha",
    # Masters de fin de saison
    "finals",
    # ── ATP 500 / WTA 500 — ÉLARGISSEMENT, décision opérateur du 2026-09-30 ──
    # (« tennis élargir ») : du 11 au 28/09, AUCUN signal tennis, faute de
    # tournoi 1000 entre l'US Open et Pékin ; OddsAPI servait pourtant l'ATP
    # de Tokyo. Budget : ~21 crédits/semaine par tournoi ouvert
    # (`ops.py ligues tennis`), soit ~40-60/semaine les semaines de 500, sous
    # le plafond par créneau de SpendPolicy (le tennis passe en DERNIER).
    # Revue le 2026-11-16 : si les recommandés réglés de ces tournois restent
    # sous n=10, ou que le digest les montre sous le point mort, les retirer
    # d'ici — décision opérateur.
    # Noms ATP 500
    "rotterdam", "abn_amro", "dallas", "rio", "rio_open", "acapulco",
    "mexican", "mexican_open", "barcelona", "munich", "halle", "queens",
    "queen_s", "hamburg", "washington", "citi_open", "japan_open", "tokyo",
    "basel", "swiss_indoors", "vienna", "erste_bank",
    # Noms WTA 500
    "brisbane", "adelaide", "abu_dhabi", "linz", "charleston", "stuttgart",
    "berlin", "eastbourne", "bad_homburg", "monterrey", "seoul", "korea",
    "pan_pacific", "ningbo", "merida", "san_diego",
)
_TENNIS_PREFIXES = ("tennis_atp_", "tennis_wta_")
TENNIS_SPORT = "tennis"       # sport-type des clés dynamiques, nommé une fois


def _tournoi_retenu(slug: str) -> bool:
    """Le slug désigne-t-il un tournoi de TENNIS_TOURNAMENTS ? Par MOTS
    entiers (séparés par « _ ») et non par sous-chaîne : depuis
    l'élargissement du 2026-09-30, des noms courts comme « rio » ou « linz »
    capteraient n'importe quel slug qui les contient."""
    bornes = f"_{slug}_"
    return any(f"_{t}_" in bornes for t in TENNIS_TOURNAMENTS)


def discover_tennis_keys(api_key: str, catalogue: list | None = None) -> dict[str, str]:
    """Clés tennis ACTIVES du catalogue, restreintes à TENNIS_TOURNAMENTS
    (Chelems, 1000, 500).

    0 crédit. `catalogue` est la réponse de GET /sports si l'appelant l'a
    déjà — fetch_odds passe celle que probe_key vient de télécharger pour
    sonder la clé, donc en production cette fonction ne fait AUCUN appel
    réseau. Sans catalogue fourni, elle appelle /sports elle-même (gratuit).
    Rend {clé: "tennis"} à fusionner dans keys_to_scan. Toute panne (réseau,
    HTTP ≠ 200, JSON illisible) rend {} ET logge : le scan continue sur les
    clés statiques, jamais d'exception — politique « retour [] + log ».
    """
    if os.environ.get("TENNIS_DYNAMIC", "1") == "0":
        return {}
    if catalogue is None:
        try:
            r = requests.get(f"{BASE_URL}/sports", params={"apiKey": api_key}, timeout=10)
            if r.status_code != 200:
                log.debug("tennis discovery: HTTP %s", r.status_code)
                return {}
            catalogue = r.json() or []
        except Exception as e:
            log.debug("tennis discovery: %s", e)
            return {}

    found: dict[str, str] = {}
    for s in catalogue:
        key = str(s.get("key") or "")
        if not key.startswith(_TENNIS_PREFIXES):
            continue
        if not s.get("active") or s.get("has_outrights"):
            continue
        slug = key.split("_", 2)[-1]          # "tennis_atp_us_open" → "us_open"
        if _tournoi_retenu(slug):
            found[key] = "tennis"
    if found:
        log.info("Tennis : %d tournoi(s) retenu(s) au catalogue — %s",
                 len(found), ", ".join(sorted(found)))
    return found


# Markets fetched per sport (API supports h2h,spreads,totals in one call)
_MARKETS_BY_SPORT = {
    "basketball":       "h2h,spreads,totals",
    "hockey":           "h2h,spreads,totals",  # NHL ML + puck line + O/U
    "americanfootball": "h2h,spreads,totals",  # NFL ML + point spread + O/U
    "baseball":         "h2h,totals",          # MLB ML + O/U (no spreads)
    "rugby":            "h2h,spreads,totals",
    "rugbyleague":      "h2h,spreads,totals",  # NRL — même structure que rugby union
    "aussierules":      "h2h,spreads,totals",  # AFL — ligne = 6.5+ pts typique
    "tennis":           "h2h,totals",
    "darts":            "h2h",
    "cricket":          "h2h",
    "boxing":           "h2h",
    "mma":              "h2h",                 # ML seulement — pas de spreads/totals sur un combat
    "soccer":           "h2h,spreads,totals",
    "euroleague_basketball": "h2h,spreads,totals",  # mêmes marchés que la NBA
    "college_football": "h2h,spreads,totals",       # NCAAF — mêmes marchés que la NFL
}


# ── Extraction helpers ────────────────────────────────────────────────

def _odd(val) -> float:
    try:
        f = float(val)
        return f if f > 1.01 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _extract_h2h(bookmakers: list, bookie_key: str, home: str, away: str) -> dict | None:
    """{"1": float, "X": float, "2": float} — X=0 for binary sports."""
    for bk in bookmakers:
        if bk.get("key") != bookie_key:
            continue
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "h2h":
                continue
            prices = {o["name"]: _odd(o.get("price")) for o in mkt.get("outcomes", [])}
            return {
                "1": prices.get(home, 0.0),
                "X": prices.get("Draw", 0.0),
                "2": prices.get(away, 0.0),
            }
    return None


def _extract_spreads(bookmakers: list, bookie_key: str, home: str, away: str) -> dict | None:
    """{"home": float, "away": float, "point": float} — point is home team's line."""
    for bk in bookmakers:
        if bk.get("key") != bookie_key:
            continue
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "spreads":
                continue
            result: dict = {}
            for o in mkt.get("outcomes", []):
                price = _odd(o.get("price"))
                point = float(o.get("point", 0))
                if o["name"] == home:
                    result["home"]  = price
                    result["point"] = point
                elif o["name"] == away:
                    result["away"]       = price
                    result["away_point"] = point
            if "home" in result and "away" in result and result["home"] > 1.01:
                return result
    return None


def _extract_totals(bookmakers: list, bookie_key: str) -> dict | None:
    """{"over": float, "under": float, "point": float}."""
    for bk in bookmakers:
        if bk.get("key") != bookie_key:
            continue
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "totals":
                continue
            result: dict = {}
            for o in mkt.get("outcomes", []):
                price = _odd(o.get("price"))
                side  = o.get("name", "").lower()
                if side == "over":
                    result["over"]  = price
                    result["point"] = float(o.get("point", 0))
                elif side == "under":
                    result["under"] = price
            if "over" in result and "under" in result:
                return result
    return None


# ── Event parser ──────────────────────────────────────────────────────

def _execution_keys() -> list[str]:
    """Clés OddsAPI des books d'exécution, dans l'ordre de EXECUTION_BOOKS."""
    return [ODDS_API_BOOK_KEYS[b] for b in EXECUTION_BOOKS if b in ODDS_API_BOOK_KEYS]


# ── Prix sharp achetés SANS book d'exécution (2026-09-29) ──────────────
# La NFL est payée ici (décision opérateur du 2026-09-29 : « continue à
# payer ») mais OddsAPI n'y cote pas 1xbet : `_parse_event` jette le match.
# Son prix sharp, lui, est bon — Pinnacle, ou à défaut Circa / Bookmaker.eu,
# les deux books sharp US déjà demandés dans la même requête. On le GARDE,
# au format des exchanges (core/matchbook), pour qu'il serve de référence
# aux matchs dont odds-api.io apporte le prix 1xbet/Bet365 : le crédit
# achète enfin quelque chose. Vidé au début de chaque `fetch_odds`.
SHARP_SANS_EXECUTION: dict[str, dict] = {}
_SHARP_US = ((PINNACLE_KEY, "pinnacle"), (CIRCA_KEY, "circa"), (CRIS_KEY, "bookmaker.eu"))

# ── Achat DIFFÉRÉ des ligues sans book d'exécution (2026-09-29) ────────
# MESURÉ sur les 35 scans standard du 23 au 29/09 : 56 achats de ligue n'ont
# rendu AUCUN match exploitable — jusqu'à ~168 crédits sur 643 engagés (≈ 26 %
# de la dépense), NFL et NCAAF (OddsAPI n'y cote jamais 1xbet), MMA (5/5 sans
# book d'exécution le 29/09), une partie du tennis. Les jours chargés
# (25-26/09), ces crédits manquaient : le plafond refusait des ligues en
# fenêtre favorable.
# Depuis le matin du 29/09, leur Pinnacle sert de référence aux matchs
# qu'odds-api.io rend exécutables. Mais la ligue entière était achetée pour
# les quelques matchs que 1xbet cote ailleurs — et achetée aussi quand il
# n'y en avait AUCUN (Matchbook et Smarkets couvraient 6/6 de ces matchs
# gratuitement, `ops.py chaine americanfootball 96`, 29/09).
# D'où l'achat DIFFÉRÉ, qui tient sur trois faits de la doc OddsAPI v4 :
# `/events` est gratuit (le pré-vol le lit déjà), `/odds` accepte `eventIds`,
# et « if no events are returned, the request will not count against the
# usage quota ». Une ligue MESURÉE sans book d'exécution à son dernier achat
# (SpendPolicy.sharp_seul — mesure en meta, aucune liste écrite ici) n'est
# plus achetée au Tier 1 : après le Tier 2 gratuit, `acheter_sharp_differe`
# l'achète SEULEMENT si un book d'exécution cote un de ses matchs ailleurs,
# et seulement pour ces matchs-là. Sinon : 0 crédit.
# Ce n'est pas le couplage de sources interdit le 2026-09-02 (« le Tier 2
# entier sautait dès qu'OddsAPI rendait UN event ») : la source gratuite
# tourne toujours ; c'est l'ACHAT qui attend de savoir s'il servira.
_PREVOL_EVENTS: dict[str, list] = {}     # ligue -> matchs du pré-vol gratuit (dernier fetch_odds)
DIFFERES: dict[str, str] = {}            # ligue -> sport-type, reportées au dernier fetch_odds
_FENETRE: dict[str, str] = {}            # commenceTimeFrom/To + borne jouable du dernier fetch_odds


def sharp_seul(ev: dict, sport_type: str) -> dict | None:
    """Le prix sharp d'un match sans book d'exécution, au format exchange
    ({"match","home","away","1","X","2","commence_time","_source",
    "totals"?, "spreads"?}), ou None si aucun book sharp ne le cote."""
    home = str(ev.get("home_team", "")).strip()
    away = str(ev.get("away_team", "")).strip()
    if not home or not away:
        return None
    bookmakers = ev.get("bookmakers") or []
    for cle, nom in _SHARP_US:
        h2h = _extract_h2h(bookmakers, cle, home, away)
        if not h2h or h2h["1"] <= 1.01 or h2h["2"] <= 1.01:
            continue
        row = {"match": f"{home} vs {away}", "home": home, "away": away,
               "1": h2h["1"], "X": h2h["X"], "2": h2h["2"],
               "commence_time": ev.get("commence_time", ""), "sport": sport_type,
               "_source": nom}
        tot = _extract_totals(bookmakers, cle)
        if tot:
            row["totals"] = tot
        spr = _extract_spreads(bookmakers, cle, home, away)
        if spr:
            row["spreads"] = spr
        return row
    return None


def sharp_sans_execution() -> dict[str, dict]:
    """Copie du pool du dernier `fetch_odds` — clés « home_away » en
    minuscules, celles que `core.exchange_match.lookup_exchange` essaie en
    premier."""
    return dict(SHARP_SANS_EXECUTION)


def _books(ev: dict) -> set:
    return {b.get("key") for b in (ev.get("bookmakers") or []) if isinstance(b, dict)}


def _sans_execution(recus: list) -> int:
    """Matchs reçus qu'AUCUN book d'exécution ne cote (diagnostic lisible ;
    la mesure qui décide du report compte, elle, les matchs EXPLOITABLES)."""
    exe = set(_execution_keys())
    return sum(1 for ev in recus if isinstance(ev, dict) and not (_books(ev) & exe))


def cout_reel(r, sport_type: str) -> float:
    """Crédits RÉELLEMENT débités par cet appel : l'en-tête `x-requests-last`
    (OddsAPI ne facture que les marchés présents dans la réponse, et rien
    pour une réponse vide), à défaut le coût théorique `league_cost`. Le
    rythme du jour se tient ainsi sur ce qui est dépensé, pas sur un tarif
    supposé (2026-09-29)."""
    try:
        return float(r.headers.get("x-requests-last"))
    except (TypeError, ValueError, AttributeError):
        return float(league_cost(sport_type))


def _pourquoi_inexploitable(recus: list) -> str:
    """Ce qui manque aux matchs d'une ligue payée pour passer `_parse_event` :
    le prix sharp (Pinnacle) ou un book d'exécution. Compté sur les books
    présents, pas sur le parsing — c'est la cause qu'on veut lire."""
    exe = set(_execution_keys())
    sans_pin = sum(1 for ev in recus if PINNACLE_KEY not in _books(ev))
    sans_exe = _sans_execution(recus)
    return (f"sans {PINNACLE_KEY} : {sans_pin}/{len(recus)}, sans book d'exécution "
            f"({'/'.join(sorted(exe)) or 'aucun'}) : {sans_exe}/{len(recus)}")


def _parse_event(ev: dict, sport_type: str) -> dict | None:
    home = str(ev.get("home_team", "")).strip()
    away = str(ev.get("away_team", "")).strip()
    if not home or not away:
        return None

    bookmakers = ev.get("bookmakers", [])

    # Un bloc 1X2 par book d'exécution servi par OddsAPI (jamais un maximum
    # par issue — core/execution_books) ; `odds_1xbet` = bloc de référence.
    h2h_par_book = {}
    for book in EXECUTION_BOOKS:
        key = ODDS_API_BOOK_KEYS.get(book)
        h = _extract_h2h(bookmakers, key, home, away) if key else None
        # Hors football, un bloc qui cote le nul est un 1X2 de TEMPS
        # RÉGLEMENTAIRE (1xbet sur la NHL, 2026-10-01), pas la moneyline que
        # cote Pinnacle : ce book n'est pas un book d'exécution pour ce match,
        # et ses totaux/handicaps (même famille de marchés) ne sont pas lus
        # non plus. La mesure d'exécution (`noter_execution`) voit ainsi la
        # vérité, et la ligue passe en achat DIFFÉRÉ au lieu d'être payée
        # pour rien — son Pinnacle sert alors de référence aux matchs
        # qu'odds-api.io rend exécutables (`sharp_seul`).
        if h and not (sport_type != "soccer" and nul_cote(h)):
            h2h_par_book[book] = h
    pin_h2h   = _extract_h2h(bookmakers, PINNACLE_KEY, home, away)
    if not h2h_par_book or not pin_h2h:
        return None  # Both sides must have h2h for the event to be useful
    xbet_h2h = h2h_par_book[min(h2h_par_book, key=ordre)]

    circa_h2h = _extract_h2h(bookmakers, CIRCA_KEY, home, away)
    cris_h2h  = _extract_h2h(bookmakers, CRIS_KEY,  home, away)

    event = {
        "id":            ev.get("id", f"{home}_{away}"),
        "match":         f"{home} vs {away}",
        "home":          home,
        "away":          away,
        "league":        ev.get("sport_title", ""),
        "sport":         sport_type,
        "sport_id":      {"soccer": 1, "tennis": 3, "basketball": 4, "boxing": 5, "darts": 6, "cricket": 7, "hockey": 8, "americanfootball": 10, "baseball": 11, "rugby": 12, "volleyball": 13, "tabletennis": 14, "handball": 15, "aussierules": 16, "rugbyleague": 17, "euroleague_basketball": 4, "college_football": 10}.get(sport_type, 1),
        "commence_time": ev.get("commence_time", ""),
        "odds_1xbet":    xbet_h2h,
        "h2h_par_book":  h2h_par_book,
        "odds_pinnacle": pin_h2h,
    }
    if circa_h2h:
        event["odds_circa"] = circa_h2h
    if cris_h2h:
        event["odds_cris"] = cris_h2h

    # ── Spreads (binary sports only — tennis/boxing/darts/cricket/baseball have no spreads) ──
    if sport_type not in ("tennis", "boxing", "mma", "darts", "cricket", "baseball", "rugbyleague"):
        xs = fusionner_lignes({b: r for b, r in ((b, _extract_spreads(bookmakers, ODDS_API_BOOK_KEYS[b], home, away))
                                                for b in h2h_par_book) if r}, "spreads")
        ps = _extract_spreads(bookmakers, PINNACLE_KEY, home, away)
        if xs and ps:
            event["spreads_1xbet"]    = xs
            event["spreads_pinnacle"] = ps
            cs = _extract_spreads(bookmakers, CIRCA_KEY, home, away)
            rs = _extract_spreads(bookmakers, CRIS_KEY,  home, away)
            if cs:
                event["spreads_circa"] = cs
            if rs:
                event["spreads_cris"] = rs

    # ── Totals (all sports) ───────────────────────────────────────────
    xt = fusionner_lignes({b: r for b, r in ((b, _extract_totals(bookmakers, ODDS_API_BOOK_KEYS[b]))
                                            for b in h2h_par_book) if r}, "totals")
    pt = _extract_totals(bookmakers, PINNACLE_KEY)
    if xt and pt:
        event["totals_1xbet"]    = xt
        event["totals_pinnacle"] = pt
        ct = _extract_totals(bookmakers, CIRCA_KEY)
        rt = _extract_totals(bookmakers, CRIS_KEY)
        if ct:
            event["totals_circa"] = ct
        if rt:
            event["totals_cris"] = rt

    return event


# ── Pré-vol GRATUIT ───────────────────────────────────────────────────
# Deux endpoints de l'API v4 ne comptent PAS dans le quota (doc éditeur,
# vérifiée 2026-08-01) : `/v4/sports` et `/v4/sports/{key}/events`. Seul
# `/odds` est facturé, au tarif [marchés] × [régions] — soit 3 crédits par
# ligue pour un `h2h,spreads,totals` sur `eu`.
#
# Jusqu'ici on payait ces 3 crédits pour CHAQUE ligue de SPORT_KEYS à chaque
# scan, y compris celles qui n'avaient aucun match dans la fenêtre : en Golden
# Hour (fenêtre 2h) c'est le cas de presque toutes. Demander d'abord la liste
# des matchs — gratuitement — et ne payer que les ligues qui en ont au moins
# un est une économie sans aucune contrepartie : la réponse `/odds` aurait été
# vide de toute façon. Ce n'est pas du rationnement — aucun scan utile n'est
# refusé, jamais.

def _events_in_window(api_key: str, sport_key: str, time_from: str, time_to: str,
                      playable_from: str | None = None) -> tuple[int | None, int | None]:
    """(matchs dans la fenêtre, dont JOUABLES) pour cette ligue — 0 crédit.

    Jouable = coup d'envoi à `playable_from` ou après (T-2h + marge) : un
    match plus proche sortirait FANTÔME par construction (2026-09-03), donc
    un crédit payé pour lui n'achète aucun pari. Un match sans horaire
    lisible compte comme jouable — on ne sait pas. Sans `playable_from`,
    les deux nombres sont égaux.

    Renvoie (None, None) si l'appel échoue : on ne sait pas, donc on laisse
    le scan payant décider. Ne jamais transformer une panne du pré-vol en
    « pas de match » — ce serait une panne silencieuse du pipeline entier.
    """
    try:
        r = requests.get(
            f"{BASE_URL}/sports/{sport_key}/events/",
            params={"apiKey": api_key,
                    "commenceTimeFrom": time_from,
                    "commenceTimeTo": time_to},
            timeout=10,
        )
        if r.status_code == 404:
            return 0, 0          # hors saison
        if r.status_code != 200:
            return None, None
        events = r.json() or []
        # Gardés pour l'achat différé (ids + équipes, gratuit) — voir DIFFERES.
        _PREVOL_EVENTS[sport_key] = [ev for ev in events if isinstance(ev, dict)]
        if not playable_from:
            return len(events), len(events)
        playable = sum(1 for ev in events
                       if not (isinstance(ev, dict) and ev.get("commence_time"))
                       or str(ev["commence_time"]) >= playable_from)
        return len(events), playable
    except Exception as e:
        log.debug("preflight %s: %s", sport_key, e)
        return None, None


# ── Pool de clés OddsAPI (v10.3) ──────────────────────────────────────
#
# POURQUOI. Le 10 août 2026 la clé unique ODDS_API_KEY est tombée à 0 crédit
# et n'a pas été tournée pendant dix jours : 40 runs/jour à « 0 matchs,
# 0 signaux », et rien dans le système ne pouvait faire mieux qu'attendre un
# humain. Une clé épuisée est un événement NORMAL (500 req/mois, soit une clé
# tous les ~2 jours au rythme actuel) — il doit se traiter tout seul.
#
# COMMENT. Le moteur lit désormais un POOL ordonné :
#   1. l'argument explicite `api_key` (tests, scripts)
#   2. `ODDS_API_KEYS` — plusieurs clés séparées par virgule/espace/retour
#      à la ligne, dans app_secrets (Supabase) ou l'environnement
#   3. `ODDS_API_KEY` — la clé « historique », toujours honorée
#   4. `ODDS_API_KEY_2` … `ODDS_API_KEY_9` dans l'environnement
# Chaque clé est sondée via GET /v4/sports (0 crédit) avant d'être utilisée ;
# un 401/403/422 en cours de scan marque la clé morte et le scan REPREND sur
# la suivante, même ligue, sans rien perdre. Le marquage est process-local :
# chaque run re-sonde (gratuit), donc une clé rechargée le 1er du mois
# revient d'elle-même dans la rotation.
#
# Ajouter une clé : `python scripts/rotate_odds_key.py --add <clé>` — elle
# est validée avant d'être écrite dans app_secrets.ODDS_API_KEYS.

POOL_SECRET = "ODDS_API_KEYS"

# Crédits minimum pour qu'une clé soit jugée utilisable. À 0 il reste de quoi
# faire échouer un scan au milieu ; le pré-vol gratuit (`_events_in_window`)
# ne coûte rien mais l'appel /odds qui suit, si. Ce n'est PAS un rationnement
# (décision opérateur 2026-08-01) : la clé est écartée quand elle ne peut
# plus rien payer, pas avant.
MIN_CREDITS = int(os.environ.get("ODDS_MIN_CREDITS", "1"))
_dead_keys: dict[str, str] = {}      # clé -> raison (process-local)
_last_failure: str = ""
_LAST_CATALOGUE: list | None = None  # dernier GET /sports réussi (probe_key) — réutilisé par discover_tennis_keys


def _split_keys(raw: str | None) -> list[str]:
    return [k.strip() for k in re.split(r"[,;\s]+", raw or "") if k.strip()]


def candidate_keys(explicit: str | None = None) -> list[str]:
    """Pool ordonné et dédupliqué — voir le bloc de commentaire ci-dessus."""
    out: list[str] = []

    def add(raw: str | None) -> None:
        for k in _split_keys(raw):
            if k not in out:
                out.append(k)

    add(explicit)
    add(get_secret(POOL_SECRET))
    add(get_secret("ODDS_API_KEY"))
    # L'ENVIRONNEMENT rejoint TOUJOURS le pool, même quand app_secrets a une
    # valeur : get_secret() ne regarde l'env que si la table est VIDE, donc
    # une clé neuve posée dans les secrets GitHub restait invisible tant
    # qu'une clé périmée traînait dans la table (constaté le 2026-08-22 :
    # app_secrets figé au 06/08 sur une clé à 499/500, rotation opérateur
    # sans effet). La clé morte est écartée par la sonde gratuite, la
    # neuve prend le relais — sans toucher à la priorité de la table.
    add(os.environ.get(POOL_SECRET))
    add(os.environ.get("ODDS_API_KEY"))
    for i in range(2, 10):
        add(os.environ.get(f"ODDS_API_KEY_{i}"))
    return out


def mark_dead(key: str, reason: str) -> None:
    global _last_failure
    _dead_keys[key] = reason
    _last_failure = reason


def reset_pool() -> None:
    """Oublie les clés marquées mortes (tests, ou après une rotation)."""
    global _last_failure
    _dead_keys.clear()
    _key_remaining.clear()
    _key_used.clear()
    _last_failure = ""


def pool_status(explicit: str | None = None) -> dict:
    """{'total': n, 'dead': m, 'live': n-m, 'reason': dernière panne} —
    pour les logs et l'alerte Telegram de run_engine.py."""
    keys = candidate_keys(explicit)
    dead = [k for k in keys if k in _dead_keys]
    return {"total": len(keys), "dead": len(dead), "live": len(keys) - len(dead),
            "reason": _last_failure}


def pool_exhausted() -> bool:
    """True quand il EXISTE des clés et qu'elles sont TOUTES mortes —
    le cas qui réclame une rotation humaine, et seulement celui-là."""
    st = pool_status()
    return st["total"] > 0 and st["live"] == 0


# Dernier `x-requests-remaining` vu (sonde gratuite ou réponse payante) —
# c'est ce que la politique de dépense (core/scan_windows.SpendPolicy) lit
# pour sa garde de réserve. None = jamais observé.
_last_remaining: int | None = None
_last_used: int | None = None
# Compteurs PAR CLÉ (dernier en-tête vu, sonde ou réponse payante). C'est la
# base du rythme mensuel (core/scan_windows) : l'allocation du jour se calcule
# sur le POOL ENTIER, pas sur la clé active — sinon 5 comptes de 500 seraient
# gérés comme un seul de 500, cinq fois de suite.
_key_remaining: dict[str, int] = {}
_key_used: dict[str, int] = {}


def _note_key(key: str, remaining, used=None) -> None:
    try:
        _key_remaining[key] = int(remaining)
    except (TypeError, ValueError):
        return
    try:
        _key_used[key] = int(used)
    except (TypeError, ValueError):
        pass


def league_cost(sport_type: str) -> int:
    """Crédits d'un appel /odds pour ce sport : 1 par marché × 1 région (eu)."""
    return len(_MARKETS_BY_SPORT.get(sport_type, "h2h").split(","))


def pool_remaining() -> int | None:
    return _last_remaining


def pool_known_remaining() -> int | None:
    """Somme des crédits restants des clés déjà observées (vivantes), sans
    aucun appel. None si aucune clé n'a encore été vue."""
    seen = [r for k, r in _key_remaining.items() if k not in _dead_keys]
    return sum(seen) if seen else None


def pool_total_remaining(explicit: str | None = None) -> int | None:
    """Crédits restants du POOL ENTIER : sonde (0 crédit) chaque clé pas
    encore observée dans ce process, puis somme des vivantes. Une clé qui ne
    répond pas est marquée morte, comme dans _next_live_key. Un GET /sports
    gratuit par clé et par run, pour piloter tout le pool : c'est le bon
    échange (et l'allocation du jour, core/scan_windows, se recalcule seule
    quand une clé entre ou sort du pool)."""
    for k in candidate_keys(explicit):
        if k in _dead_keys or k in _key_remaining:
            continue
        ok, detail = probe_key(k)
        if not ok:
            mark_dead(k, detail)
    return pool_known_remaining()


def pool_totals() -> dict | None:
    """{'remaining','used','total','pct'} agrégés sur les clés observées —
    None tant que moins de deux clés sont connues (pool_counters suffit)."""
    keys = [k for k in _key_remaining if k not in _dead_keys]
    if len(_key_remaining) < 2:
        return None
    r = sum(_key_remaining[k] for k in keys)
    u = sum(_key_used.get(k, 0) for k in keys) + \
        sum(_key_remaining[k] + _key_used.get(k, 0) for k in _key_remaining if k in _dead_keys)
    total = r + u
    if total <= 0:
        return None
    return {"remaining": r, "used": u, "total": total, "pct": 100.0 * r / total}


def pool_counters() -> dict:
    """{'remaining', 'used', 'total', 'pct'} de la clé ACTIVE (celle que le
    prochain scan utilisera) — None partout si jamais observé. Sert à la
    ligne de log et à l'alerte par paliers de run_engine (Mission 2)."""
    r, u = _last_remaining, _last_used
    if r is None or u is None or (r + u) <= 0:
        return {"remaining": r, "used": u, "total": None, "pct": None}
    return {"remaining": r, "used": u, "total": r + u, "pct": 100.0 * r / (r + u)}


def _note_remaining(raw, used=None) -> None:
    global _last_remaining, _last_used
    try:
        _last_remaining = int(raw)
    except (TypeError, ValueError):
        pass
    if used is not None:
        try:
            _last_used = int(used)
        except (TypeError, ValueError):
            pass


def probe_key(key: str) -> tuple[bool, str]:
    """(vivante?, détail) via GET /v4/sports — 0 crédit consommé."""
    try:
        r = requests.get(f"{BASE_URL}/sports/", params={"apiKey": key}, timeout=10)
    except Exception as e:
        return False, f"appel impossible : {e}"
    if r.status_code in (401, 403):
        return False, f"HTTP {r.status_code} — clé invalide, révoquée ou quota épuisé"
    if r.status_code == 422:
        return False, "HTTP 422 — quota épuisé"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    # Le catalogue est dans la réponse : on le garde pour la découverte des
    # clés tennis (discover_tennis_keys), qui n'a alors AUCUN appel à faire.
    # Une sonde qui télécharge le catalogue et une découverte qui le
    # re-télécharge, c'était deux GET /sports pour une information.
    global _LAST_CATALOGUE
    try:
        _LAST_CATALOGUE = r.json() or []
    except Exception:
        _LAST_CATALOGUE = None
    # Le compteur, pas seulement le code HTTP : mesuré en direct le
    # 2026-08-20, une clé à 499/500 crédits répond encore 200 sur /sports
    # (endpoint gratuit) alors qu'elle n'a plus de quoi payer un scan. La
    # laisser « vivante » ferait perdre la première ligue du scan sur un 401.
    remaining = r.headers.get("x-requests-remaining")
    used = r.headers.get("x-requests-used", "?")
    try:
        left = int(remaining)
    except (TypeError, ValueError):
        left = None
    if left is not None:
        _note_remaining(left, used)
        _note_key(key, left, used)
    if left is not None and left < MIN_CREDITS:
        return False, f"quota épuisé — restantes={left} utilisées={used}"
    return True, f"HTTP 200 — restantes={remaining or '?'} utilisées={used}"


def _next_live_key(keys: list[str], start: int) -> tuple[str | None, int]:
    """Première clé vivante à partir de l'index `start` (sondage gratuit).
    Les clés qui ne répondent pas sont marquées mortes au passage."""
    for i in range(start, len(keys)):
        k = keys[i]
        if k in _dead_keys:
            continue
        ok, detail = probe_key(k)
        if ok:
            log.info("OddsAPI clé #%d/%d active (…%s) — %s", i + 1, len(keys), k[-4:], detail)
            return k, i
        mark_dead(k, detail)
        log.warning("OddsAPI clé #%d/%d (…%s) écartée — %s", i + 1, len(keys), k[-4:], detail)
    return None, len(keys)


# ── Public API ────────────────────────────────────────────────────────

# Une connexion coupée par OddsAPI (« Connection aborted », reset 104) est
# rejouée UNE fois avant d'abandonner la ligue : le 2026-09-08 à 13:11 l'appel
# soccer_uefa_champs_league est tombé ainsi, sans seconde tentative, le jour
# où la Ligue des champions fournissait les seuls signaux — 0 crédit dépensé,
# 0 événement, et la ligue perdue jusqu'au scan suivant. Un reset est un
# incident de transport, pas une réponse : le rejouer ne coûte rien de plus
# que l'appel qui a échoué. Toute autre exception suit toujours la politique
# « retour [] + log » du bloc appelant.
_RETRY_PAUSE_S = 2.0


def _get_retried(url: str, params: dict, sport_key: str):
    """GET OddsAPI, rejoué une fois sur erreur de connexion."""
    import time
    try:
        return requests.get(url, params=params, timeout=15)
    except requests.exceptions.ConnectionError as e:
        log.warning("%s: connexion coupée (%s) — seconde tentative", sport_key, e)
        time.sleep(_RETRY_PAUSE_S)
        return requests.get(url, params=params, timeout=15)


def fetch_odds(api_key: str | None = None, hours_ahead: int = 24,
               sport_keys: dict | None = None, spend_policy=None) -> list[dict]:
    """
    Fetch events in the next `hours_ahead` hours with h2h + spreads + totals.

    `spend_policy` (core/scan_windows.SpendPolicy, optionnel) : consultée
    ligue par ligue APRÈS le pré-vol gratuit — une ligue peuplée mais hors
    fenêtre favorable, payée il y a moins de 180 min, ou sous la réserve
    de crédits, est sautée ET loggée. Sans politique, on paie comme avant.
    Priority: NBA → Tennis Masters → Soccer.
    sport_keys: override the default SPORT_KEYS dict (used by Golden Hour mode).
    Returns [] if API key missing or quota exhausted (engine falls back to Gemini).

    AUCUN RATIONNEMENT, décision opérateur du 2026-08-01 : « laisse OddsAPI
    couler, si c'est fini on aura d'autres [clés] ». Un gouverneur de quota
    mensuel a été écrit puis retiré — il rendait des scans stériles pour
    étaler un budget que l'opérateur préfère dépenser à fond, quitte à
    changer de clé. L'ancien garde `remaining < 50` est parti avec : il
    immobilisait 10% du plan sans jamais le dépenser. Le scan s'arrête
    désormais sur un vrai 422 (quota épuisé côté API), pas avant.
    """
    # Pool de clés : app_secrets (Supabase) d'abord, os.environ en filet —
    # voir le bloc « Pool de clés OddsAPI » plus haut et core/secret_store.py.
    keys = candidate_keys(api_key)
    if not keys:
        log.error("No ODDS_API_KEY — ni dans app_secrets (Supabase) ni dans "
                  "l'environnement (.env / GitHub Secrets / Vercel)")
        return []
    api_key, key_idx = _next_live_key(keys, 0)
    if api_key is None:
        log.critical("OddsAPI : les %d clés du pool sont épuisées/invalides (%s) — "
                     "rotation requise : python scripts/rotate_odds_key.py --add <clé>",
                     len(keys), _last_failure)
        return []
    assert api_key is not None  # narrow type after early return

    SHARP_SANS_EXECUTION.clear()
    DIFFERES.clear()
    _PREVOL_EVENTS.clear()
    keys_to_scan = sport_keys if sport_keys is not None else SPORT_KEYS
    # Tennis : clés éphémères résolues à chaque scan (0 crédit). Fusionnées
    # ICI — et non dans SPORT_KEYS / GOLDEN_SPORT_KEYS — pour couvrir les
    # deux modes sans tenir deux listes de plus (cf. AUDIT.md §1).
    keys_to_scan = {**keys_to_scan, **discover_tennis_keys(api_key, catalogue=_LAST_CATALOGUE)}

    now       = datetime.now(timezone.utc)
    until     = now + timedelta(hours=hours_ahead)
    time_from = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    time_to   = until.strftime("%Y-%m-%dT%H:%M:%SZ")
    # Même format ISO « Z » que l'API : la comparaison de chaînes est exacte.
    playable_lead = timedelta(minutes=PLAYABLE_MIN_MINUTES + PLAYABLE_MARGIN_MIN)
    playable_from = (now + playable_lead).strftime("%Y-%m-%dT%H:%M:%SZ")
    _FENETRE.clear()
    _FENETRE.update({"from": time_from, "to": time_to, "jouable": playable_from})

    # Pré-vol gratuit : on ne garde que les ligues qui ont réellement des
    # matchs dans la fenêtre, triées par PRIORITÉ DÉCLARÉE (l'ordre de
    # SPORT_KEYS), le volume ne départageant qu'à rang égal. Deux effets, le
    # second est le vrai : un 422 interrompt le scan sur les ligues les moins
    # importantes, et le plafond de dépense d'un scan de fond — qui refuse
    # tous les jours des ligues peuplées — est consommé par les ligues qui
    # valent le plus, plus par celles qui jouent le plus souvent (2026-09-17,
    # voir le préambule de SPORT_KEYS).
    populated: list = []
    skipped_empty = 0
    saved = 0
    skipped_season = 0
    skipped_policy = 0
    skipped_phantom: list = []
    for sport_key, sport_type in keys_to_scan.items():
        if not _season_open(sport_key, now):
            skipped_season += 1
            continue
        n_events, n_playable = _events_in_window(api_key, sport_key, time_from, time_to,
                                                 playable_from)
        if n_events == 0:
            skipped_empty += 1
            saved += league_cost(sport_type)
            continue
        if n_playable == 0:
            # Tous les matchs à moins de T-2h (+ marge) : chaque signal en
            # sortirait fantôme. Un crédit ici n'achète RIEN de recommandable
            # — c'était le coût caché des crons de 19:03/21:03 sur le Big 5.
            skipped_phantom.append(sport_key)
            saved += league_cost(sport_type)
            continue
        # Tri et priorité sur les matchs JOUABLES : c'est eux que le crédit
        # achète. Pré-vol en panne (None) : 0 pour le tri, mais on paie.
        populated.append((sport_key, sport_type, n_playable if n_playable is not None else 0))
    # Les plus peuplées d'abord — AVANT la politique de dépense : quand le
    # rythme du jour n'autorise que quelques ligues, ce sont celles qui
    # rapportent le plus de matchs par crédit qui passent, pas les premières
    # du dictionnaire. Même ordre pour l'achat : un 422 interrompt sur les
    # ligues les moins fournies.
    rangs = rangs_par_famille()
    dernier = len(DEBUTS_DE_FAMILLE)      # clés dynamiques (tennis) : en dernier
    populated.sort(key=lambda x: (rangs.get(x[0], dernier), -x[2]))
    scan_plan: list = []
    for sport_key, sport_type, n_events in populated:
        if spend_policy is not None:
            # Ligue mesurée SANS book d'exécution : son sharp ne sert qu'aux
            # matchs qu'un book d'exécution cote AILLEURS, qu'on ne connaît
            # qu'après le Tier 2 — achat reporté (acheter_sharp_differe).
            # Pré-vol en panne (matchs inconnus) : on achète comme avant —
            # une panne du pré-vol ne vaut jamais « pas de match ».
            motif = spend_policy.sharp_seul(sport_key)
            if motif and sport_key in _PREVOL_EVENTS:
                DIFFERES[sport_key] = sport_type
                log.info("DIFFÉRÉ | %s : aucun book d'exécution au dernier achat (%s) — "
                         "acheté après le Tier 2, seulement pour les matchs qu'un book "
                         "d'exécution cote ailleurs", sport_key, motif)
                continue
            pool_left = pool_known_remaining()
            allowed, _why = spend_policy.allow(
                sport_key, sport_type, now,
                pool_left if pool_left is not None else _last_remaining,
                cost=league_cost(sport_type))
            if not allowed:
                skipped_policy += 1
                continue
        scan_plan.append((sport_key, sport_type, n_events))
    if skipped_empty or skipped_phantom:
        log.info("Pré-vol gratuit : %d/%d ligues sans match dans la fenêtre, %d dont tous "
                 "les matchs sont à moins de %d min (fantômes par construction : %s) — "
                 "%d crédits économisés", skipped_empty, len(keys_to_scan),
                 len(skipped_phantom), PLAYABLE_MIN_MINUTES + PLAYABLE_MARGIN_MIN,
                 " ".join(skipped_phantom) or "—", saved)
    if skipped_season:
        log.info("Hors saison : %d ligue(s) avant leur date d'ouverture (SEASON_OPENS) — "
                 "0 crédit, 0 appel", skipped_season)
    if skipped_policy:
        log.info("Politique de dépense : %d ligue(s) peuplée(s) sautée(s) ce scan "
                 "(rythme / fond espacé / réserve) — détail ligne par ligne ci-dessus",
                 skipped_policy)
    if spend_policy is not None and getattr(spend_policy, "allowance", None) is not None:
        planned = sum(league_cost(t) for _k, t, _n in scan_plan)
        left = spend_policy.budget_left(now)
        log.info("RYTHME | allocation %.0f crédits/j — engagés aujourd'hui %.0f (dont %d "
                 "pour ce scan : %d ligue(s)) — encore engageables à cette heure : %.0f",
                 spend_policy.allowance, spend_policy.spent_today + spend_policy.engaged,
                 planned, len(scan_plan), left if left is not None else -1)

    all_events = []
    for sport_key, sport_type, _n in scan_plan:
        events, api_key, key_idx, pool_mort = _acheter_ligue(
            keys, key_idx, api_key, sport_key, sport_type, time_from, time_to, spend_policy)
        all_events.extend(events)
        if pool_mort:
            log.critical("OddsAPI : pool épuisé (%d clés) après %d ligues — "
                         "%d events conservés. Rotation requise : "
                         "python scripts/rotate_odds_key.py --add <clé>",
                         len(keys), list(keys_to_scan).index(sport_key), len(all_events))
            return all_events
    return all_events


def _acheter_ligue(keys: list, key_idx: int, api_key: str, sport_key: str, sport_type: str,
                   time_from: str, time_to: str, spend_policy=None,
                   event_ids: list | None = None) -> tuple[list, str | None, int, bool]:
    """UN appel /odds payant pour une ligue : rotation des clés, compteurs,
    dépouillement (matchs exploitables, prix sharp sans exécution), mesure de
    la couverture d'exécution. Partagé par `fetch_odds` et
    `acheter_sharp_differe` — une seule copie de ces règles.

    `event_ids` : restreint l'achat à ces matchs (paramètre `eventIds`).
    Rend (matchs exploitables, clé courante, index, pool entièrement mort)."""
    markets = _MARKETS_BY_SPORT.get(sport_type, "h2h")
    url = f"{BASE_URL}/sports/{sport_key}/odds/"
    params = {
        "apiKey":           api_key,
        "regions":          "eu",
        "markets":          markets,
        "bookmakers":       ",".join([PINNACLE_KEY, *_execution_keys(), CIRCA_KEY, CRIS_KEY]),
        "oddsFormat":       "decimal",
        "commenceTimeFrom": time_from,
        "commenceTimeTo":   time_to,
    }
    if event_ids:
        params["eventIds"] = ",".join(event_ids)
    events: list = []
    try:
        r = None
        while True:
            params["apiKey"] = api_key
            r = _get_retried(url, params, sport_key)
            if r.status_code not in (401, 403, 422):
                break
            # Clé à sec (422) ou refusée (401/403 — OddsAPI renvoie aussi
            # un 401 OUT_OF_USAGE_CREDITS à 0 crédit) : on la marque
            # morte et on REPREND LA MÊME LIGUE sur la clé suivante du
            # pool. Le scan ne s'arrête que si le pool entier est mort.
            mark_dead(api_key, f"HTTP {r.status_code} sur {sport_key}")
            log.warning("OddsAPI clé #%d (…%s) morte (HTTP %d) — bascule",
                        key_idx + 1, api_key[-4:], r.status_code)
            api_key, key_idx = _next_live_key(keys, key_idx + 1)
            if api_key is None:
                return events, None, key_idx, True
        remaining = r.headers.get("x-requests-remaining", "?")
        used      = r.headers.get("x-requests-used", "?")
        _note_remaining(remaining, used)
        _note_key(api_key, remaining, used)
        if spend_policy is not None and r.status_code == 200:
            spend_policy.note_paid(sport_key, cout_reel(r, sport_type))

        if r.status_code == 404:
            return events, api_key, key_idx, False    # Not in season
        if r.status_code != 200:
            log.warning("%s: HTTP %d", sport_key, r.status_code)
            return events, api_key, key_idx, False

        recus = r.json() or []
        gardes_sharp = 0
        for brut in recus:
            ev_ok = _parse_event(brut, sport_type)
            if ev_ok:
                events.append(ev_ok)
                # Achat ciblé (différé) : on l'a payé pour servir de référence
                # au match que le Tier 2 rend exécutable — son sharp est gardé
                # même quand OddsAPI le rend lui-même exploitable.
                if not event_ids:
                    continue
            row = sharp_seul(brut, sport_type)
            if row:
                SHARP_SANS_EXECUTION[f"{row['home'].lower()}_{row['away'].lower()}"] = row
                gardes_sharp += 1
        # Mesure = matchs EXPLOITABLES (sharp + 1X2 d'un book d'exécution),
        # pas la simple présence d'un book : le 29/09, 1xbet apparaissait sur
        # Browns–Steelers chez OddsAPI sans y coter le 1X2.
        if recus and spend_policy is not None:
            spend_policy.noter_execution(sport_key, len(events), len(recus))
        if gardes_sharp:
            log.info("PAYÉ POUR LE SHARP | %s : %d match(s) sans book d'exécution — leur "
                     "prix sharp sert de référence aux matchs d'odds-api.io (%s)",
                     sport_key, gardes_sharp, _pourquoi_inexploitable(recus))
        elif recus and not events:
            # Payé, reçu, tout jeté — et jusqu'au 2026-09-29 sans un mot :
            # la NFL a été achetée à chaque créneau depuis l'ouverture de
            # sa saison sans qu'OddsAPI y cote jamais 1xbet (0/16 mesuré
            # par `ops.py books`). Le crédit part ; que la raison reste.
            log.warning("PAYÉ SANS RETOUR | %s : %d match(s) reçu(s), 0 exploitable — %s",
                        sport_key, len(recus), _pourquoi_inexploitable(recus))
        if events:
            has_totals  = sum(1 for e in events if "totals_1xbet"  in e)
            has_spreads = sum(1 for e in events if "spreads_1xbet" in e)
            log.info("%s: %d events | totals=%d spreads=%d | used=%s remaining=%s",
                     sport_key, len(events), has_totals, has_spreads, used, remaining)

    except Exception as e:
        log.error("%s: %s", sport_key, e)

    return events, api_key, key_idx, False


# Écart maximal entre les coups d'envoi de deux fiches pour les tenir pour le
# MÊME match : deux équipes NFL/NCAA se rencontrent parfois deux fois dans la
# saison — les noms seuls ne suffisent pas.
_MEME_MATCH_H = 12.0


def _ecart_h(a: str, b: str) -> float | None:
    try:
        ta = datetime.fromisoformat(str(a).replace("Z", "+00:00"))
        tb = datetime.fromisoformat(str(b).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ta.tzinfo is None or tb.tzinfo is None:
        return None
    return abs((ta - tb).total_seconds()) / 3600.0


def matchs_prevol_tennis() -> dict[str, dict] | None:
    """Les matchs de tennis des tournois RETENUS (`TENNIS_TOURNAMENTS` :
    Chelems, 1000, 500) vus au pré-vol gratuit du dernier `fetch_odds`, au
    format des exchanges ({« home_away » en minuscules: {home, away,
    commence_time}}) — celui que `core.exchange_match.lookup_exchange` lit.

    C'est la seule liste qui dise, sans rien écrire à la main (règle n°6),
    QUELS matchs de tennis sont au périmètre : odds-api.io nomme ses tournois
    par ville (« ATP - Tokyo, Japan »), OddsAPI par slug
    (`tennis_atp_japan_open`), et seul le second est filtré par la décision
    opérateur du 2026-09-30 (« 500 et plus, 250 exclus »).

    None si aucun tournoi retenu n'a été pré-volé — Tier 1 éteint, pool mort,
    semaine sans tournoi retenu : l'appelant garde son comportement d'avant.
    Un dict VIDE veut dire « des tournois retenus, aucun match dans la
    fenêtre »."""
    cles = [k for k in _PREVOL_EVENTS if k.startswith(_TENNIS_PREFIXES)]
    if not cles:
        return None
    index: dict[str, dict] = {}
    for cle in cles:
        for ev in _PREVOL_EVENTS[cle]:
            h, a = str(ev.get("home_team", "")).strip(), str(ev.get("away_team", "")).strip()
            if h and a:
                index[f"{h.lower()}_{a.lower()}"] = {
                    "home": h, "away": a, "commence_time": ev.get("commence_time", "")}
    return index


def acheter_sharp_differe(matchs_executables: list, spend_policy=None) -> int:
    """Achète le sharp des ligues DIFFÉRÉES par le dernier `fetch_odds`, et
    seulement pour leurs matchs qu'un book d'exécution cote ailleurs
    (`matchs_executables` : les matchs du Tier 2, odds-api.io en tête).

    Par ligue : les matchs JOUABLES du pré-vol gratuit sont appariés aux
    matchs exécutables (les deux équipes, candidat unique, coups d'envoi à
    moins de `_MEME_MATCH_H` h). Aucun apparié → 0 crédit, et la ligne le
    dit. Sinon UN appel /odds restreint par `eventIds`, soumis à la même
    politique de dépense qu'au Tier 1 ; les prix sharp rejoignent
    `SHARP_SANS_EXECUTION`, que le moteur pose ensuite AVANT les exchanges.
    Rend le nombre de prix sharp gardés. Ne lève jamais."""
    if not DIFFERES:
        return 0
    from core.exchange_match import lookup_exchange     # import tardif : cycle
    index: dict[str, dict] = {}
    for m in matchs_executables or []:
        h, a = str(m.get("home", "")).strip(), str(m.get("away", "")).strip()
        if h and a:
            index[f"{h.lower()}_{a.lower()}"] = {"home": h, "away": a,
                                                  "commence_time": m.get("commence_time", "")}
    keys = candidate_keys()
    api_key, key_idx = _next_live_key(keys, 0) if keys else (None, 0)
    now = datetime.now(timezone.utc)
    avant = len(SHARP_SANS_EXECUTION)
    for sport_key, sport_type in list(DIFFERES.items()):
        jouables = [ev for ev in _PREVOL_EVENTS.get(sport_key, [])
                    if ev.get("id") and str(ev.get("commence_time", "")) >= _FENETRE.get("jouable", "")]
        ids = []
        for ev in jouables:
            hit = lookup_exchange({"home": str(ev.get("home_team", "")),
                                   "away": str(ev.get("away_team", ""))}, index)
            if not hit:
                continue
            ecart = _ecart_h(ev.get("commence_time", ""), hit.get("commence_time", ""))
            if ecart is not None and ecart > _MEME_MATCH_H:
                continue
            ids.append(str(ev["id"]))
        if not ids:
            log.info("DIFFÉRÉ | %s : aucun de ses %d match(s) jouable(s) n'est coté par un "
                     "book d'exécution ailleurs — 0 crédit", sport_key, len(jouables))
            continue
        if api_key is None:
            log.warning("DIFFÉRÉ | %s : %d match(s) à couvrir mais aucune clé OddsAPI vivante",
                        sport_key, len(ids))
            break
        if spend_policy is not None:
            pool_left = pool_known_remaining()
            allowed, _why = spend_policy.allow(
                sport_key, sport_type, now,
                pool_left if pool_left is not None else _last_remaining,
                cost=league_cost(sport_type))
            if not allowed:
                continue                  # motif loggé par la politique (« DÉPENSE | … sauté »)
        n_avant = len(SHARP_SANS_EXECUTION)
        _events, api_key, key_idx, pool_mort = _acheter_ligue(
            keys, key_idx, api_key, sport_key, sport_type,
            _FENETRE.get("from", ""), _FENETRE.get("to", ""), spend_policy, event_ids=ids)
        log.info("DIFFÉRÉ | %s : acheté pour %d match(s) coté(s) ailleurs par un book "
                 "d'exécution (eventIds, sur %d jouable(s)) — %d prix sharp gardé(s)",
                 sport_key, len(ids), len(jouables), len(SHARP_SANS_EXECUTION) - n_avant)
        if pool_mort:
            break
    return len(SHARP_SANS_EXECUTION) - avant
