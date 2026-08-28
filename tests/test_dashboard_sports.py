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
        "avec le repli générique 🎯 sur le dashboard.")


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


class TestCompteARebours:
    """Le « prochain scan » du dashboard dérive des crons réels de scan.yml.

    Mesuré le 2026-08-28 : le compte à rebours visait :00/:30 en dur alors que
    le tick golden est horaire depuis le 2026-07-23 — et il ne tournait même
    pas sur l'état vide, tué par un TypeError d'initialisation (voir plus
    bas). Règle n°6 : la cadence vit dans scripts/ci_scan_mode.py::CRON_MODES,
    api/index.py la dérive, le template ne fait que la recevoir.
    """

    def test_les_specs_couvrent_tous_les_crons_de_scan(self):
        from api.index import _SCAN_CRONS
        from scripts.ci_scan_mode import CRON_MODES
        assert len(_SCAN_CRONS) == len(CRON_MODES)
        for spec in _SCAN_CRONS:
            assert 0 <= spec["m"] < 60
            assert spec["h"] is None or all(0 <= h < 24 for h in spec["h"])

    def test_un_cron_exotique_leve_au_lieu_de_mentir(self, monkeypatch):
        # Un cron que le parseur ne comprend pas doit faire échouer l'import
        # (donc la suite), jamais afficher un compte à rebours faux.
        import api.index as api
        monkeypatch.setattr(api, "_CRON_MODES", {"3 2 * * 1": "standard"})
        with pytest.raises(ValueError):
            api._scan_cron_specs()

    def test_le_template_recoit_les_crons_sans_cadence_en_dur(self):
        src = TestPasDeTableDupliquee.JS.read_text(encoding="utf-8")
        m = re.search(r"const SCAN_CRONS\s*=\s*(.+)", src)
        assert m, "SCAN_CRONS a disparu de index.html"
        assert m.group(1).strip().startswith("{{"), (
            "SCAN_CRONS est codé en dur dans index.html — "
            "il doit venir de api/index.py (scan_crons).")
        api_src = (Path(__file__).resolve().parent.parent
                   / "api" / "index.py").read_text(encoding="utf-8")
        rendu = api_src[api_src.index('render_template("index.html"'):][:400]
        assert "scan_crons" in rendu, "scan_crons n'est pas passé à index.html"

    def test_l_initialisation_des_filtres_est_gardee_sur_l_etat_vide(self):
        # L'état vide ne rend ni #sport-chips ni #signals-list : une init
        # inconditionnelle jetait un TypeError qui tuait tout le script
        # restant, compte à rebours compris — précisément sur la page que
        # l'opérateur regarde quand il n'y a aucun signal (2026-08-28).
        src = TestPasDeTableDupliquee.JS.read_text(encoding="utf-8")
        garde = src.index("if(document.getElementById('signals-list'))")
        assert garde < src.index("_buildSportChips();"), (
            "l'init des filtres n'est plus gardée par la présence de "
            "#signals-list — l'état vide re-cassera tout le script.")
