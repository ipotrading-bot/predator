"""
tests/test_wiz_engine.py — WIZ (PAIM v10.0) : scoring, parsing, doctrine.

Ces tests ne vérifient pas surtout que le code « marche » — ils verrouillent
les quatre règles de doctrine qu'une modification bien intentionnée du
scoring casserait sans bruit :

  R1  Wiz ne touche pas au quantitatif → le classement reste dominé par
      l'edge (test_edge_domine_le_classement).
  R3  Le consensus est CONTRARIAN → un argument Tier C "pour" doit DÉGRADER
      le score (test_tier_c_pour_degrade_le_score). C'est le test le plus
      important du fichier : l'erreur naturelle est de traiter le consensus
      comme une confirmation, et rien d'autre ne la rattraperait.
  R4  Jamais de donnée inventée → un argument dont l'URL n'est pas dans les
      sources RÉELLEMENT consultées est jeté (test_argument_source_inventee_rejete).
  Verdict borné vers le bas uniquement (test_verdict_*).
"""
import json

from core.constants import (
    WIZ_NEUTRAL_CONFIDENCE,
    WIZ_TIER_WEIGHTS,
    WIZ_W_EDGE,
)
from core.wiz_engine import (
    INDISPONIBLE,
    analyze_match,
    build_prompt,
    build_queries,
    cap_confidence,
    decide_verdict,
    extract_json,
    rank_score,
    unavailable,
    validate,
    weighted_score,
)

# Résultats de recherche de référence — les URLs ici sont le SEUL set
# d'URLs légitimes ; tout le reste est considéré comme inventé.
RESULTS = [
    {"title": "Team news", "url": "https://sky.example/news", "description": "Star striker ruled out"},
    {"title": "Preview",   "url": "https://bbc.example/prev", "description": "Both teams at full strength"},
]

CTX = {
    "match_id":        "abc123",
    "match":           "Real Madrid vs Barcelona",
    "sport":           "soccer",
    "league":          "La Liga",
    "kickoff":         "2026-07-24T19:00:00+00:00",
    "markets":         ["1X2 - Real Madrid"],
    "market_keys":     ["h2h"],
    "signal_ids":      [1, 2],
    "edge_pct":        6.0,
    "consensus_score": 80,
}


def _arg(tier="A", direction="pour", poids=1.0, url="https://sky.example/news"):
    return {"texte": "x", "source_url": url, "tier": tier, "direction": direction, "poids": poids}


def _flag(sev="haute", url="https://sky.example/news"):
    return {"texte": "titulaire absent", "source_url": url, "severite": sev}


# ══════════════════════════════════════════════════════════════════════
# Requêtes de recherche
# ══════════════════════════════════════════════════════════════════════

def test_deux_axes_de_recherche_maximum():
    """2 axes Tier A par match — au-delà, on dilue ce qui explique un edge."""
    qs = build_queries("A vs B", "soccer", ["h2h", "totals", "spreads"], CTX["kickoff"])
    assert len(qs) == 2


def test_requete_adaptee_au_sport():
    assert "starting pitcher" in build_queries("A vs B", "baseball", ["h2h"])[0]
    assert "goalie" in build_queries("A vs B", "hockey", ["h2h"])[0]
    assert "back-to-back" in build_queries("A vs B", "basketball", ["h2h"])[0]
    # Sport inconnu : on retombe sur la requête générique, pas d'exception.
    assert build_queries("A vs B", "sport_inexistant", ["h2h"])[0]


def test_totals_bascule_sur_la_meteo():
    """Sur un total, la météo déplace la ligne plus qu'une absence isolée."""
    assert "weather" in build_queries("A vs B", "soccer", ["totals"])[1]
    assert "weather" not in build_queries("A vs B", "soccer", ["h2h"])[1]


def test_match_sans_nom_ne_produit_aucune_requete():
    assert build_queries("", "soccer", ["h2h"]) == []


def test_kickoff_illisible_ne_casse_pas():
    qs = build_queries("A vs B", "soccer", ["h2h"], kickoff="pas une date")
    assert len(qs) == 2


# ══════════════════════════════════════════════════════════════════════
# Prompt
# ══════════════════════════════════════════════════════════════════════

def test_prompt_contient_le_contexte_et_les_axes_de_recherche():
    """Depuis l'abandon de Brave, on n'injecte plus des résultats mais les
    AXES que le connecteur web_search doit couvrir."""
    p = build_prompt(CTX)
    assert "Real Madrid vs Barcelona" in p
    assert "+6.00%" in p
    assert "RECHERCHE WEB" in p.upper()
    assert "team news" in p          # axe Tier A du soccer


def test_prompt_exige_la_recherche_du_faux_edge():
    """R2 : la mission première doit être explicite dans le prompt."""
    p = build_prompt(CTX).lower()
    assert "piège" in p
    assert "contrarian" in p


def test_prompt_interdit_le_consensus_en_red_flag():
    """Constaté live : le modèle classait le consensus public en red flag
    « haute », soit ~4x le poids prévu pour un drapeau jaune."""
    p = build_prompt(CTX)
    assert "red_flags" in p and "drapeau JAUNE" in p


# ══════════════════════════════════════════════════════════════════════
# Parsing défensif
# ══════════════════════════════════════════════════════════════════════

def test_json_nu():
    assert extract_json('{"verdict":"NEUTRE"}')["verdict"] == "NEUTRE"


def test_json_entoure_de_backticks_markdown():
    txt = 'Voici mon analyse :\n```json\n{"verdict":"ALERTE"}\n```\nVoilà.'
    assert extract_json(txt)["verdict"] == "ALERTE"


def test_json_avec_preambule_et_conclusion():
    txt = 'Après analyse, {"verdict":"VETO","wiz_confidence":10} — attention.'
    assert extract_json(txt)["verdict"] == "VETO"


def test_json_avec_accolade_dans_une_chaine():
    """Un find/rfind naïf casserait ici ; le compteur ignore les chaînes."""
    txt = '{"verdict":"NEUTRE","resume":"le score {2-1} tient"}'
    assert extract_json(txt)["resume"] == "le score {2-1} tient"


def test_json_illisible_retourne_none_sans_lever():
    assert extract_json("désolé, je ne peux pas répondre") is None
    assert extract_json("") is None
    assert extract_json(None) is None
    assert extract_json("{ceci n'est pas du json") is None
    assert extract_json('["une","liste"]') is None   # pas un objet


# ══════════════════════════════════════════════════════════════════════
# R4 — jamais de donnée inventée
# ══════════════════════════════════════════════════════════════════════

def test_argument_source_inventee_rejete():
    """Le garde-fou central de R4 : une URL absente des résultats = argument jeté."""
    parsed = {
        "verdict": "CONFIRME", "wiz_confidence": 90, "resume": "ok",
        "arguments": [
            _arg(url="https://sky.example/news"),          # légitime
            _arg(url="https://hallucine.example/invente"),  # inventée
        ],
        "red_flags": [],
    }
    clean = validate(parsed, RESULTS)
    assert len(clean["arguments"]) == 1
    assert clean["arguments"][0]["source_url"] == "https://sky.example/news"


def test_red_flag_source_inventee_rejete():
    parsed = {"verdict": "VETO", "wiz_confidence": 5, "resume": "",
              "arguments": [], "red_flags": [_flag(url="https://faux.example/x")]}
    assert validate(parsed, RESULTS)["red_flags"] == []


def test_verdict_tranche_sans_source_valide_devient_indisponible():
    """Un verdict affirmé dont aucun argument ne survit n'a rien étayé."""
    def fake_search(_p, **_k):
        return json.dumps({
            "verdict": "VETO", "wiz_confidence": 5, "resume": "je le sens mal",
            "arguments": [_arg(url="https://invente.example/x")],
            "red_flags": [],
        }), RESULTS, "modele-test"

    row = analyze_match(CTX, search_fn=fake_search)
    assert row["verdict"] == INDISPONIBLE
    assert row["wiz_confidence"] is None


def test_champs_invalides_corriges_vers_le_neutre():
    """Un tier ou un poids aberrant ne doit pas faire tomber l'analyse entière."""
    parsed = {
        "verdict": "NEUTRE", "wiz_confidence": 999, "resume": "x",
        "arguments": [{"texte": "y", "source_url": "https://bbc.example/prev",
                       "tier": "Z", "direction": "peut-être", "poids": 42}],
        "red_flags": [{"texte": "z", "source_url": "https://bbc.example/prev",
                       "severite": "catastrophique"}],
    }
    clean = validate(parsed, RESULTS)
    assert clean["arguments"][0]["tier"] == "B"          # défaut modérateur
    assert clean["arguments"][0]["direction"] == "pour"
    assert clean["arguments"][0]["poids"] == 1.0         # borné à [0,1]
    assert clean["red_flags"][0]["severite"] == "moyenne"
    assert clean["wiz_confidence"] == 100.0              # borné à [0,100]


def test_verdict_inconnu_devient_indisponible():
    parsed = {"verdict": "SUPER_BON", "wiz_confidence": 90, "resume": "",
              "arguments": [], "red_flags": []}
    assert validate(parsed, RESULTS)["verdict"] == INDISPONIBLE


# ══════════════════════════════════════════════════════════════════════
# R3 — le consensus est CONTRARIAN
# ══════════════════════════════════════════════════════════════════════

def test_tier_c_a_un_poids_negatif():
    """La doctrine est encodée dans le signe du coefficient, pas dans le prompt."""
    assert WIZ_TIER_WEIGHTS["C"] < 0
    assert WIZ_TIER_WEIGHTS["A"] > WIZ_TIER_WEIGHTS["B"] > 0


def test_tier_c_pour_degrade_le_score():
    """Le consensus public dans NOTRE sens = drapeau jaune, pas confirmation.

    C'est l'inversion demandée par R3. Sans elle, un signal massivement
    suivi par le public remonterait dans le classement — exactement l'erreur
    que la doctrine interdit.
    """
    assert weighted_score([_arg(tier="C", direction="pour")], []) < 0


def test_tier_c_ne_peut_jamais_ameliorer_le_score():
    """R3 littéral : le consensus n'est JAMAIS une confirmation.

    `direction` est ignoré pour le Tier C, dans les deux sens. Deux raisons :

    1. Doctrine — faire remonter un signal parce que le public est à
       l'opposé, c'est encore traiter le consensus comme prédictif.
    2. Constaté live le 2026-07-23 (mistral-small-latest) : le modèle écrit
       tier=C/direction="contre" pour dire « ce consensus est un mauvais
       signe » — il remplit le champ avec son JUGEMENT, pas avec le FAIT.
       Combiné au poids de tier négatif, ça donnait une double négation :
       0.3 × (-0.35) × (-1) = +0.105. Un risque signalé par le modèle
       AMÉLIORAIT le score, c'est-à-dire exactement l'inverse de R3.
    """
    assert weighted_score([_arg(tier="C", direction="contre")], []) < 0
    assert weighted_score([_arg(tier="C", direction="pour")], []) < 0
    # Et les deux sens pèsent pareil : `direction` ne joue plus aucun rôle.
    assert weighted_score([_arg(tier="C", direction="contre")], []) == \
        weighted_score([_arg(tier="C", direction="pour")], [])


def test_tier_c_pese_moins_qu_un_tier_a():
    """Contrarian ne veut pas dire décisif — le Tier A doit dominer."""
    melange = weighted_score([_arg(tier="A", direction="pour"),
                              _arg(tier="C", direction="pour")], [])
    assert melange > 0   # l'info terrain l'emporte sur le drapeau consensus


# ══════════════════════════════════════════════════════════════════════
# Pondération et verdict
# ══════════════════════════════════════════════════════════════════════

def test_argument_contre_compte_negativement():
    assert weighted_score([_arg(tier="A", direction="contre")], []) < 0
    assert weighted_score([_arg(tier="A", direction="pour")], []) > 0


def test_red_flags_pesent_par_severite():
    haute   = weighted_score([], [_flag("haute")])
    moyenne = weighted_score([], [_flag("moyenne")])
    basse   = weighted_score([], [_flag("basse")])
    assert haute < moyenne < basse < 0


def test_verdict_optimiste_corrige_vers_le_bas():
    """Un CONFIRME contredit par ses propres arguments est ramené au réel."""
    verdict, score = decide_verdict("CONFIRME",
                                    [_arg(tier="A", direction="contre")],
                                    [_flag("haute")])
    assert score < 0
    assert verdict in ("ALERTE", "VETO")


def test_verdict_prudent_jamais_remonte():
    """Asymétrie volontaire : on ne promeut jamais un verdict prudent."""
    verdict, _ = decide_verdict("ALERTE", [_arg(tier="A", direction="pour")], [])
    assert verdict == "ALERTE"


def test_un_red_flag_haute_severite_force_au_moins_alerte():
    """Le cas que Wiz existe pour attraper ne doit pas se diluer."""
    verdict, _ = decide_verdict(
        "CONFIRME",
        [_arg(tier="A", direction="pour"), _arg(tier="A", direction="pour")],
        [_flag("haute")])
    assert verdict in ("ALERTE", "VETO")


def test_indisponible_est_absorbant():
    verdict, score = decide_verdict(INDISPONIBLE, [_arg()], [_flag()])
    assert verdict == INDISPONIBLE
    assert score == 0.0


def test_faisceau_lourd_donne_veto():
    verdict, _ = decide_verdict("NEUTRE", [_arg(tier="A", direction="contre")],
                                [_flag("haute"), _flag("haute")])
    assert verdict == "VETO"


# ══════════════════════════════════════════════════════════════════════
# Plafond de confiance par verdict
# ══════════════════════════════════════════════════════════════════════
#
# Régression constatée LIVE le 2026-07-23 (premier appel réel à
# mistral-small-latest) : le modèle a rendu verdict=VETO avec
# wiz_confidence=75, lisant le champ comme « ma confiance dans mon
# analyse » au lieu de « probabilité que le pari passe ». Comme la
# confiance pèse +0.35 dans rank_score, le match qualifié de PIÈGE
# remontait en TÊTE du classement (0.6445 contre 0.557 pour un NEUTRE de
# même edge). Aucun test unitaire ne pouvait l'attraper avant : les stubs
# renvoyaient des paires verdict/confiance cohérentes, ce qu'un vrai
# modèle ne garantit pas.

def test_veto_ne_peut_pas_porter_une_confiance_haute():
    assert cap_confidence("VETO", 75.0) <= 15.0
    assert cap_confidence("ALERTE", 90.0) <= 40.0


def test_plafond_ne_remonte_jamais_une_confiance_basse():
    """Le plafond borne, il ne corrige pas vers le haut."""
    assert cap_confidence("VETO", 5.0) == 5.0
    assert cap_confidence("CONFIRME", 30.0) == 30.0


def test_confirme_et_neutre_ne_sont_pas_plafonnes():
    assert cap_confidence("CONFIRME", 95.0) == 95.0
    assert cap_confidence("NEUTRE", 88.0) == 88.0


def test_indisponible_na_pas_de_confiance():
    assert cap_confidence(INDISPONIBLE, 80.0) is None
    assert cap_confidence("NEUTRE", None) is None


def test_un_veto_ne_remonte_jamais_devant_un_neutre_a_edge_egal():
    """Le vrai invariant : c'est le classement affiché qui compte."""
    veto = rank_score(7.4, cap_confidence("VETO", 75.0), 80, "VETO")
    neutre = rank_score(7.4, cap_confidence("NEUTRE", 50.0), 80, "NEUTRE")
    assert veto < neutre


def test_confiance_incoherente_du_modele_est_bornee_de_bout_en_bout():
    """Reproduit la réponse live exacte qui a révélé le bug."""
    def fake_search(_p, **_k):
        return json.dumps({
            "verdict": "VETO", "wiz_confidence": 75,
            "resume": "Titulaire absent, la cote élevée s'explique.",
            "arguments": [_arg(tier="A", direction="contre", poids=1.0)],
            "red_flags": [_flag("moyenne")],
        }), RESULTS, "mistral-small-latest"

    row = analyze_match(CTX, search_fn=fake_search)
    assert row["verdict"] == "VETO"
    assert row["wiz_confidence"] <= 15.0
    assert row["wiz_rank_score"] < rank_score(CTX["edge_pct"], 50.0,
                                              CTX["consensus_score"], "NEUTRE")


def test_prompt_desambiguise_la_confiance():
    """La correction mécanique est le garde-fou, mais le prompt ne doit pas
    entretenir l'ambiguïté qui a causé le bug."""
    p = build_prompt(CTX)
    assert "PROBABILITÉ" in p
    assert "BASSE" in p


# ══════════════════════════════════════════════════════════════════════
# R1 — le quantitatif garde la primauté
# ══════════════════════════════════════════════════════════════════════

def test_edge_domine_le_classement():
    """Critère d'acceptation : à confiance Wiz nulle, le fort edge reste devant."""
    fort   = rank_score(10.0, 0.0, consensus_score=50, verdict="NEUTRE")
    faible = rank_score(1.0, 0.0, consensus_score=50, verdict="NEUTRE")
    assert fort > faible


def test_edge_domine_meme_contre_une_confiance_maximale():
    """Wiz module, il ne décide pas : il ne doit pas pouvoir inverser un
    écart d'edge important à lui seul."""
    fort_sans_wiz = rank_score(15.0, 0.0, consensus_score=0, verdict="NEUTRE")
    faible_max_wiz = rank_score(0.0, 100.0, consensus_score=0, verdict="CONFIRME")
    assert fort_sans_wiz > faible_max_wiz
    assert WIZ_W_EDGE > 0.35   # W_WIZ


def test_wiz_departage_a_edge_egal():
    """Là où Wiz sert : deux edges identiques, la confiance fait la différence."""
    assert rank_score(5.0, 90.0, 50, "CONFIRME") > rank_score(5.0, 10.0, 50, "ALERTE")


def test_indisponible_est_neutre_pas_penalisant():
    """L'absence d'information n'est pas une information négative."""
    indispo = rank_score(5.0, None, 50, INDISPONIBLE)
    neutre  = rank_score(5.0, WIZ_NEUTRAL_CONFIDENCE, 50, "NEUTRE")
    assert indispo == neutre
    assert indispo > rank_score(5.0, 0.0, 50, "ALERTE")     # pas au fond du classement
    assert indispo < rank_score(5.0, 100.0, 50, "CONFIRME")  # pas en tête non plus


def test_consensus_absent_est_neutre():
    """consensus_score est NULL sur les signaux à source unique (harvester/oracle)."""
    assert rank_score(5.0, 50.0, None, "NEUTRE") == rank_score(5.0, 50.0, 50, "NEUTRE")


def test_rank_score_borne_entre_0_et_1():
    assert rank_score(0.0, 0.0, 0, "VETO") == 0.0
    assert abs(rank_score(999.0, 100.0, 100, "CONFIRME") - 1.0) < 1e-9
    # Valeurs aberrantes : bornées, jamais d'exception
    assert 0.0 <= rank_score(-5.0, -20.0, 500, "NEUTRE") <= 1.0
    assert 0.0 <= rank_score(None, None, None, INDISPONIBLE) <= 1.0


# ══════════════════════════════════════════════════════════════════════
# Orchestration — aucun chemin ne doit lever
# ══════════════════════════════════════════════════════════════════════

def test_analyse_nominale():
    def fake_search(_p, **_k):
        return json.dumps({
            "verdict": "ALERTE", "wiz_confidence": 35,
            "resume": "Attaquant titulaire absent.",
            "arguments": [_arg(tier="A", direction="contre",
                               url="https://sky.example/news")],
            "red_flags": [_flag("haute", "https://sky.example/news")],
        }), RESULTS, "modele-test"

    row = analyze_match(CTX, search_fn=fake_search)
    assert row["verdict"] in ("ALERTE", "VETO")
    assert row["match_id"] == "abc123"
    assert row["signal_ids"] == [1, 2]
    assert row["sources_count"] == 2
    assert row["queries_used"] == 1   # un seul appel : recherche + raisonnement
    assert row["model_used"] == "modele-test"
    assert 0.0 <= row["wiz_rank_score"] <= 1.0


def test_reponse_sans_aucune_source_donne_indisponible():
    """R4 : sans page réellement consultée, tout ce que le modèle affirme
    serait de la connaissance interne. On refuse de l'écrire."""
    row = analyze_match(CTX, search_fn=lambda _p, **_k: (
        json.dumps({"verdict": "CONFIRME", "wiz_confidence": 90, "resume": "de mémoire",
                    "arguments": [], "red_flags": []}), [], "m"))
    assert row["verdict"] == INDISPONIBLE
    assert row["wiz_confidence"] is None
    assert row["sources_count"] == 0


def test_ia_morte_donne_indisponible():
    row = analyze_match(CTX, search_fn=lambda _p, **_k: (None, [], None))
    assert row["verdict"] == INDISPONIBLE
    assert "IA indisponible" in row["resume"]


def test_json_illisible_donne_indisponible():
    row = analyze_match(CTX,
                        search_fn=lambda _p, **_k: ("je ne peux pas", RESULTS, "m"))
    assert row["verdict"] == INDISPONIBLE
    assert row["model_used"] == "m"


def test_fournisseur_qui_leve_ne_casse_pas_le_run():
    """Une exception réseau côté fournisseur doit dégrader, pas propager."""
    def boom(*_a, **_k):
        raise RuntimeError("connexion perdue")

    row = analyze_match(CTX, search_fn=boom)
    assert row["verdict"] == INDISPONIBLE


def test_match_sans_nom_ne_consomme_rien():
    """Pas de nom = rien à chercher : on n'appelle même pas le fournisseur."""
    def jamais(*_a, **_k):
        raise AssertionError("le fournisseur ne doit pas être appelé")

    row = analyze_match({**CTX, "match": ""}, search_fn=jamais)
    assert row["verdict"] == INDISPONIBLE
    assert row["queries_used"] == 0


def test_unavailable_produit_une_ligne_complete():
    """Un échec s'écrit en base avec son motif — un taux d'INDISPONIBLE qui
    monte doit être visible, pas silencieux."""
    row = unavailable(CTX, "quota mort")
    assert set(row) == {
        "match_id", "match", "sport", "league", "signal_ids", "verdict",
        "wiz_confidence", "wiz_rank_score", "arguments", "red_flags",
        "resume", "sources_count", "model_used", "queries_used",
    }
    assert row["resume"] == "quota mort"
    assert row["verdict"] == INDISPONIBLE


def test_aucune_cle_de_signal_dans_la_sortie():
    """R1 mécanique : la ligne produite ne peut pas écraser une colonne de
    `signals`, même si quelqu'un l'insérait par erreur dans la mauvaise table."""
    row = analyze_match(CTX, search_fn=lambda _p, **_k: (None, [], None))
    interdites = {"edge_pct", "sharp_prob", "pinnacle_price", "xbet_odd",
                  "kelly_pct", "risk_flag", "status"}
    assert not (set(row) & interdites)
