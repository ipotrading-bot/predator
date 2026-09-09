"""Gardiens de la page /bank et de la mise en francs de bout en bout
(2026-09-09) : la colonne `stake_xof` existe partout où elle doit, la
navigation a QUATRE entrées identiques sur toutes les pages, la page reste
sobre (chiffres, explications en title=), et le moteur cadence APRÈS la
partition fantôme, sans jamais bloquer le scan.
"""
import re
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
PAGES = ("index", "performance", "system", "bank")
BANK = (RACINE / "templates" / "bank.html").read_text(encoding="utf-8")


def _texte(p):
    return (RACINE / p).read_text(encoding="utf-8")


class TestLaMiseVoyageDeBoutEnBout:
    def test_la_migration_pose_la_colonne_sur_les_quatre_tables(self):
        sql = _texte("sql/migrate_v10_14_stake_xof.sql")
        for table in ("signals", "ai_learning_ledger", "signals_archive", "ai_learning_ledger_archive"):
            assert re.search(rf"ALTER TABLE {table}\s+ADD COLUMN IF NOT EXISTS stake_xof integer", sql), table
        assert "UPDATE" not in sql, "pas de backfill : une mise non décidée ne se devine pas en base"

    def test_le_moteur_pose_la_mise_apres_la_partition_fantome_et_la_fige(self):
        src = _texte("run_engine.py")
        assert '"stake_xof"}' in src.split("_OPTIONAL_COLS = ")[1][:900]
        assert '"stake_xof"' in src.split("_FIGES_AU_RAFRAICHISSEMENT = ")[1][:80]
        i_part = src.index("recommandes, shadowed = _shadow_partition(signals)")
        i_stake = src.index("assign_stakes(recommandes, ctx)")
        i_save = src.index("# ── B. Bulk-save balanced signals")
        assert i_part < i_stake < i_save
        # Jamais bloquant : l'appel est sous un try qui logge et continue.
        bloc = src[i_part:i_save]
        assert "except Exception" in bloc and "signaux sans mise" in bloc

    def test_un_rafraichissement_ne_remplit_la_mise_que_si_elle_est_nulle(self):
        # Figée : jamais réécrite. Mais une ligne active sans mise (d'avant la
        # migration) la reçoit une fois — filtre IS NULL, pas d'écrasement.
        src = _texte("run_engine.py")
        bloc = src[src.index("def _rafraichir():"):src.index("def _rafraichir_sans_contrainte()")]
        assert '.is_("stake_xof", "null")' in bloc
        assert 'update({"stake_xof": int(mise)})' in bloc

    def test_le_ledger_recopie_la_mise_au_reglement(self):
        assert '"stake_xof":              sig.get("stake_xof")' in _texte("core/db.py")

    def test_telegram_reste_sans_mise(self):
        # Décision opérateur 2026-07-21 (tests/test_telegram_format.py) : la
        # mise en francs vit sur le dashboard et /bank, pas dans les messages.
        src = _texte("run_engine.py")
        bloc = src[src.index("def _signal_block("):src.index("def _telegram_signals(")]
        assert "stake_xof" not in bloc


class TestNavigationAQuatreEntrees:
    @pytest.mark.parametrize("page", PAGES)
    def test_les_deux_barres_portent_accueil_systeme_perf_bank(self, page):
        html = _texte(f"templates/{page}.html")
        haut = re.search(r'<div class="nav-pages">(.*?)</div>', html, re.S).group(1)
        assert re.findall(r'href="([^"]+)"', haut) == ["/", "/system", "/performance", "/bank"], page
        bas = re.search(r'<nav class="bnav">(.*?)</nav>', html, re.S).group(1)
        assert re.findall(r'<a href="([^"]+)"', bas) == ["/", "/system", "/performance", "/bank"], page
        assert 'aria-label="Bank"' in bas

    @pytest.mark.parametrize("page", PAGES)
    def test_la_page_courante_est_marquee_une_seule_fois(self, page):
        html = _texte(f"templates/{page}.html")
        bas = re.search(r'<nav class="bnav">(.*?)</nav>', html, re.S).group(1)
        assert bas.count('class="on"') == 1, page


class TestLaPageBankResteSobre:
    def test_aucune_phrase_rendue_de_plus_de_14_mots(self):
        corps = re.sub(r"\{#.*?#\}", "", BANK, flags=re.S)
        corps = corps[corps.index("<body>"):corps.index("<script>")]
        for m in re.finditer(r"<p[^>]*>(.*?)</p>", corps, re.S):
            assert len(m.group(1).split()) <= 14, m.group(1)[:80]

    def test_les_explications_vivent_en_title(self):
        assert BANK.count("title=") >= 10
        for mot in ("Budget du jour", "reconstituée", "Rendement", "ROI", "Restant"):
            assert mot in BANK, mot

    def test_un_axe_par_courbe_et_une_serie(self):
        # Deux graphiques, jamais un double axe : restant et cumul séparés.
        assert BANK.count(' chart"') == 2
        assert 'id="c-jour"' in BANK and 'id="c-cumul"' in BANK

    def test_la_route_existe_et_lit_le_mois(self):
        api = _texte("api/index.py")
        assert '@app.route("/bank")' in api
        rendu = api[api.index('render_template("bank.html"'):][:300]
        for var in ("months", "mois", "mois_label", "sport_emoji"):
            assert var in rendu, var
