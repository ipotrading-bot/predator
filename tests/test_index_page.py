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


def test_la_selection_a_sa_propre_ligne_et_la_bankroll_vient_des_constantes():
    assert 'class="leg-l1"' in INDEX and 'class="leg-l2"' in INDEX
    assert ".leg-l1, .leg-l2" in CSS
    # En francs CFA depuis le 2026-09-09 (BANKROLL_REF_XOF) ; la clé de stockage
    # locale a changé avec la devise pour ne pas relire des euros en francs.
    assert 'value="{{ bankroll_ref_xof }}"' in INDEX, "la bankroll par défaut était 1000 en dur"
    assert 'value="1000"' not in INDEX and "€" not in INDEX
    assert "_BR_KEY='predator_bankroll_xof'" in INDEX


def test_un_seul_etat_actif_pour_les_puces_de_filtre():
    assert "day-on" not in INDEX and "day-on" not in CSS
