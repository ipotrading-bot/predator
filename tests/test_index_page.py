"""Gardiens de la page d'accueil (templates/index.html) — revue 2026-09-09.

La suite ne rend aucun template ; ces tests lisent la SOURCE et vérifient
les invariants qui ont déjà divergé une fois :

- « top » dans le bandeau compte les seuls TOP VALEUR, comme le badge de
  carte (il comptait aussi les VALUE : « 2 top » au-dessus d'une seule carte
  TOP VALEUR).
- Le TTL de fraîcheur d'une cote est UNE constante (core.constants) lue par
  le moteur (slate du REPRICE) et injectée au dashboard, jamais recopiée.
- Pas de <meta refresh> (rechargeait la fiche ouverte) ni de
  `user-scalable=no` (bloque le zoom) dans les templates.
- Les tables de sport et de drapeau viennent du serveur (règle 6).
"""
import re
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
INDEX = (RACINE / "templates" / "index.html").read_text(encoding="utf-8")
API = (RACINE / "api" / "index.py").read_text(encoding="utf-8")
CSS = (RACINE / "api" / "static" / "css" / "predator.css").read_text(encoding="utf-8")


def test_le_compteur_top_ne_compte_que_les_top_valeur():
    m = re.search(r"\{% set elites = (.+?) %\}", INDEX)
    assert m, "le compteur « top » a disparu du bandeau"
    assert "'equalto', 'HIGH_VALUE'" in m.group(1), (
        "« top » doit compter les seuls HIGH_VALUE, définition du badge "
        "TOP VALEUR — sinon le bandeau contredit les cartes.")


def test_le_ttl_de_fraicheur_est_une_seule_constante():
    from core.constants import SOFT_SLATE_TTL_H
    moteur = (RACINE / "run_engine.py").read_text(encoding="utf-8")
    assert "SOFT_SLATE_TTL_H" in moteur, "run_engine ne dérive plus son TTL de core.constants"
    assert re.search(r'CACHE_SOFT_SLATE_TTL_H",\s*"\d', moteur) is None, (
        "le TTL du slate soft est de nouveau un littéral dans run_engine")
    rendu = API[API.index('render_template("index.html"'):][:600]
    assert "soft_ttl_h=SOFT_SLATE_TTL_H" in rendu
    assert "{{ soft_ttl_h" in INDEX
    assert SOFT_SLATE_TTL_H > 0


def test_aucun_template_ne_bloque_le_zoom_ni_ne_recharge_seul():
    for f in (RACINE / "templates").glob("*.html"):
        src = f.read_text(encoding="utf-8")
        assert "user-scalable=no" not in src, f.name
        assert "maximum-scale" not in src, f.name
        assert 'http-equiv="refresh"' not in src, f.name


def test_les_tables_de_la_page_viennent_du_serveur():
    # Emoji : Jinja ET JS lisent `sport_emoji` / `_SPORT_EMOJI`, plus de
    # dictionnaire littéral {'soccer':'⚽',…} dans le template.
    assert re.search(r"\{% set E = \{", INDEX) is None
    assert re.search(r"const E=\{soccer", INDEX) is None
    assert "sport_emoji.get(" in INDEX
    # Drapeaux : une table Jinja, servie au JS par tojson.
    assert INDEX.count("'HIGH_VALUE':'TOP VALEUR'") == 1
    assert "const _FLAG_FR     = {{ FLAG_FR | tojson }}" in INDEX
    # La table est définie AVANT la branche « aucun signal », sinon le JS
    # de l'état vide reçoit un Undefined.
    assert INDEX.index("{% set FLAG_FR") < INDEX.index("{% if signals %}")


def test_la_selection_a_sa_propre_ligne_et_la_fiche_ne_propose_aucune_mise():
    assert 'class="leg-l1"' in INDEX and 'class="leg-l2"' in INDEX
    assert ".leg-l1, .leg-l2" in CSS
    # AUCUNE mise n'est proposée sur le dashboard (décision opérateur
    # 2026-09-09, après le retrait de la bankroll mensuelle) : ni montant, ni
    # pourcentage, ni fraction de Kelly, ni champ de saisie. `kelly_pct` reste
    # calculé et persisté — il refuse un signal non misable et pondère le ROI
    # du ledger — mais il ne se montre nulle part.
    for interdit in ("kelly", "Kelly", "stake", "bankroll", "€", 'href="/bank"',
                     "sig-mise", "m-stake"):
        assert interdit not in INDEX, interdit
    for interdit in ("sig-mise", "m-stake", "stake-n", "m-kelly", "bankroll"):
        assert interdit not in CSS, interdit


def test_un_seul_etat_actif_pour_les_puces_de_filtre():
    assert "day-on" not in INDEX and "day-on" not in CSS


def test_aucune_mise_ne_sort_du_dashboard_meme_sur_une_ligne_ancienne():
    """Le moteur n'écrit plus de mise, mais les lignes ÉMISES AVANT le
    2026-09-09 portent encore « Mise conseillée X% de bankroll » dans leur
    `advice`, que la fiche affiche tel quel — et une colonne `stake_xof`
    morte traîne dans le JSON servi. `api.index._sans_mise` retire les deux
    au point de LECTURE : la base garde ses lignes (règle 9), rien n'en sort.
    """
    from api.index import _sans_mise
    lignes = [
        {"advice": "EV +2.1% — cote soft 1.88 vs sharp 1.83 (prob. 54.3%). "
                   "Mise conseillée 0.56% de bankroll (Kelly fractionnaire).",
         "stake_xof": 0},
        {"advice": "EV +5.0% — cote soft 2.10 vs sharp 2.02 (prob. 49.0%). "
                   "Mise conseillée 1.20% du capital (Kelly fractionnaire). "
                   "DNB synthétique — exposition TOTALE à répartir chez le MÊME "
                   "book : 70.0% sur X et 30.0% sur le nul (@ 3.10), "
                   "soit 0.84% et 0.36% de bankroll.",
         "stake_xof": 1200},
        {"advice": None, "kelly_pct": 0.56},
    ]
    for ligne in _sans_mise(lignes):
        a = ligne.get("advice") or ""
        for interdit in ("Mise conseillée", "bankroll", "capital", "soit "):
            assert interdit not in a, (interdit, a)
        assert "stake_xof" not in ligne and "kelly_pct" not in ligne
        # Ce qui RESTE doit être propre : ni débris de décimale, ni ponctuation
        # orpheline (« .56% », « (@ 3.10), »).
        assert not a.startswith(".") and ".56%" not in a
        assert a == "" or a.endswith(".")
    # La répartition entre les DEUX jambes d'un DNB survit : c'est un ratio,
    # pas un montant — elle reste vraie quelle que soit la somme engagée.
    assert "70.0% sur X" in lignes[1]["advice"]


def test_les_routes_servent_les_signaux_decapes():
    """Les deux surfaces qui exposent un signal passent par le décapeur."""
    src = (RACINE / "api" / "index.py").read_text(encoding="utf-8")
    assert src.count("_sans_mise(") >= 3          # définition + page + API JSON
    assert "jsonify(_sans_mise(" in src
