"""
tests/test_dashboard_sports.py — tout sport ACTIF doit être présentable.

POURQUOI (mesuré le 2026-08-22) : `aussierules` et `rugbyleague` sont des
sports actifs — `core.odds_api.SPORT_KEYS` les produit, `learning_layer`
leur donne un seuil — mais ils n'avaient ni emoji ni libellé. Le dashboard
les rendait « 🎯 rugbyleague ». Un sport ajouté au scan n'échoue jamais
faute d'emoji : il s'affiche juste mal, indéfiniment, sans qu'aucun test ni
aucun log ne le signale.

Pire, `templates/index.html` gardait sa PROPRE copie JS de ces tables. Elle
avait divergé : il lui manquait en plus `euroleague_basketball`. Les tables
sont désormais injectées depuis api/index.py — ce fichier vérifie qu'il n'en
reste qu'une, et qu'elle couvre tout le périmètre en production.

L'invariant des sport-keys (CLAUDE.md) porte sur QUATRE fichiers ; ce test
en tient le maillon d'affichage.
"""
import re
from pathlib import Path

import pytest

from api.index import (_SPORT_EMOJI, _SPORT_LABEL, _SPORT_LABEL_SHORT,
                       _SPORT_ORDER)
from core.constants import RETIRED_SPORTS
from core.learning_layer import SPORT_DEFAULTS
from core.odds_api import SPORT_KEYS

# Les sports réellement produits par le scan (valeurs de SPORT_KEYS), moins
# ceux qui ont été retirés du périmètre.
SPORTS_ACTIFS = sorted(set(SPORT_KEYS.values()) - RETIRED_SPORTS)

TABLES = {
    "_SPORT_EMOJI": _SPORT_EMOJI,
    "_SPORT_LABEL": _SPORT_LABEL,
    "_SPORT_LABEL_SHORT": _SPORT_LABEL_SHORT,
    "_SPORT_ORDER": _SPORT_ORDER,
}


def test_le_perimetre_actif_nest_pas_vide():
    # Garde-fou du garde-fou : si SPORT_KEYS devient vide ou change de forme,
    # les tests ci-dessous passeraient à vide sans rien prouver.
    assert len(SPORTS_ACTIFS) >= 8


@pytest.mark.parametrize("nom", sorted(TABLES))
def test_tout_sport_actif_est_couvert(nom):
    manquants = [s for s in SPORTS_ACTIFS if s not in TABLES[nom]]
    assert not manquants, (
        f"{nom} n'a pas d'entrée pour {manquants} — ces sports s'afficheront "
        "avec le repli générique 🎯 sur le dashboard et sur /wiz.")


def test_les_seuils_appris_couvrent_le_meme_perimetre():
    # SPORT_DEFAULTS (core/learning_layer.py) et SPORT_KEYS (core/odds_api.py)
    # sont deux des quatre fichiers de l'invariant : un sport scanné sans
    # seuil par défaut est émis sans garde-fou d'edge.
    assert not set(SPORTS_ACTIFS) - set(SPORT_DEFAULTS)


def test_les_sports_retires_gardent_leur_emoji():
    # Les lignes historiques du ledger et de /performance restent affichées
    # (CLAUDE.md : « données historiques conservées »).
    assert not [s for s in RETIRED_SPORTS if s not in _SPORT_EMOJI]


class TestPasDeTableDupliquee:
    """Une table recopiée dans un template ne se met jamais à jour deux fois."""

    JS = Path(__file__).resolve().parent.parent / "templates" / "index.html"

    def test_le_template_ne_redefinit_pas_les_tables_en_dur(self):
        src = self.JS.read_text(encoding="utf-8")
        for nom in ("_SPORT_EMOJI", "_SPORT_LABEL", "_SPORT_ORDER"):
            m = re.search(rf"const {nom}\s*=\s*(.+)", src)
            assert m, f"{nom} a disparu de index.html"
            valeur = m.group(1).strip()
            assert valeur.startswith("{{"), (
                f"{nom} est de nouveau codé en dur dans index.html "
                f"({valeur[:40]}…) — il doit venir de api/index.py.")

    def test_les_variables_injectees_sont_bien_passees_au_template(self):
        api_src = (Path(__file__).resolve().parent.parent
                   / "api" / "index.py").read_text(encoding="utf-8")
        rendu = api_src[api_src.index('render_template("index.html"'):][:400]
        for var in ("sport_emoji", "sport_label_short", "sport_order"):
            assert var in rendu, f"{var} n'est pas passé à index.html"
