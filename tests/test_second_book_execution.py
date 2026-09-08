"""
tests/test_second_book_execution.py — le SECOND book d'exécution (2026-09-08).

Le 2026-09-07, « Al-Adalah +0.5 @ 1.85 » sortait d'un line shopping
Bet365 + 1xbet sous une fiche « 1XBET » : le book d'origine n'était stocké
nulle part. L'incident a posé la condition du retour d'un second book : « le
book d'origine STOCKÉ par ligne et affiché sur la fiche ». L'opérateur l'a
rouvert le 2026-09-08 (« Bet365 plus d'autres disponibles, plusieurs à la
fois », règle 11). Ce fichier verrouille la condition, bout en bout :

  1. marchés à ligne : fusion À LIGNE ÉGALE, chaque côté porte son book ;
  2. 1X2 : UN bloc entier par book, départagé sur le prix FINAL exécutable
     (les deux jambes du DNB partent chez le même book) ;
  3. le signal porte `soft_book`, la persistance le garde, le ledger le
     recopie, Telegram et la fiche l'affichent — jamais le book de référence
     par défaut quand le prix n'est pas attribuable.
"""
import logging
from datetime import datetime, timedelta, timezone

import pytest

import run_engine
from core import harvester
from core.execution_books import book_du_cote, choisir_bloc_h2h, fusionner_lignes

log = logging.getLogger("test")


def _now():
    return datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


# ── 1. Marchés à ligne : à ligne égale, chaque côté porte son book ─────────

class TestFusionALigneEgale:
    def test_une_ligne_qu_un_seul_book_cote_entre_attribuee_a_lui(self):
        """Al-Adalah : +0.5 n'existe que chez Bet365 — elle entre, à son nom ;
        les quarts de 1xbet gardent le leur. Aucune ligne n'est fabriquée."""
        fused = fusionner_lignes({
            "1xbet":  {"ladder": [{"point": 0.25, "home": 2.13, "away": 1.70},
                                  {"point": 0.75, "home": 1.53, "away": 2.40}]},
            "bet365": {"ladder": [{"point": 0.5, "home": 1.85, "away": 1.95}]},
        }, "spreads")
        par_point = {r["point"]: r for r in fused["ladder"]}
        assert set(par_point) == {0.25, 0.5, 0.75}
        assert par_point[0.5]["books"] == {"home": "bet365", "away": "bet365"}
        assert par_point[0.25]["books"] == {"home": "1xbet", "away": "1xbet"}
        assert par_point[0.5]["away_point"] == -0.5

    def test_meme_ligne_meilleur_prix_par_cote_et_le_premier_a_egalite(self):
        fused = fusionner_lignes({
            "1xbet":  {"point": 2.5, "over": 1.90, "under": 1.95},
            "bet365": {"point": 2.5, "over": 1.95, "under": 1.95},
        }, "totals")
        assert (fused["over"], fused["under"]) == (1.95, 1.95)
        assert fused["books"] == {"over": "bet365", "under": "1xbet"}   # égalité → 1xbet
        assert book_du_cote(fused, "over") == "bet365"

    def test_jamais_la_ligne_la_mieux_payee(self):
        """Deux lignes différentes restent deux barreaux : la principale est
        la plus ÉQUILIBRÉE, pas celle qui paie le plus (A6)."""
        fused = fusionner_lignes({
            "1xbet":  {"point": 2.5, "over": 1.90, "under": 1.90},
            "bet365": {"point": 3.5, "over": 3.10, "under": 1.30},
        }, "totals")
        assert fused["point"] == 2.5
        assert [r["point"] for r in fused["ladder"]] == [2.5, 3.5]

    def test_un_barreau_incomplet_ne_sort_pas(self):
        assert fusionner_lignes({"1xbet": {"point": 2.5, "over": 1.9}}, "totals") is None
        assert fusionner_lignes({}, "spreads") is None

    def test_sans_attribution_le_book_est_inconnu_pas_le_book_de_reference(self):
        assert book_du_cote({"point": 2.5, "over": 1.9, "under": 1.9}, "over") is None
        assert book_du_cote(None, "over") is None


# ── 2. 1X2 : un bloc entier, départagé sur le prix final ───────────────────

class TestBlocH2H:
    PIN = {"1": 1.85, "X": 3.60, "2": 4.40}

    def test_le_bloc_gagnant_est_celui_du_meilleur_prix_final(self):
        book, bloc, prix, fav = choisir_bloc_h2h(
            {"1xbet":  {"1": 1.95, "X": 3.50, "2": 4.00},
             "bet365": {"1": 2.05, "X": 3.50, "2": 4.00}}, "soccer", "A", "B")
        assert book == "bet365" and fav == "A"
        assert bloc == {"1": 2.05, "X": 3.50, "2": 4.00}       # le bloc ENTIER, pas un mix
        assert prix > 1.01

    def test_un_book_qui_voit_l_autre_favori_n_entre_pas_en_concurrence(self):
        """Comparer deux favoris, c'est comparer deux paris : le book de
        référence fixe le favori, l'autre bloc est ignoré."""
        book, _, _, fav = choisir_bloc_h2h(
            {"1xbet":  {"1": 1.95, "X": 3.50, "2": 4.00},
             "bet365": {"1": 4.00, "X": 3.50, "2": 1.95}}, "soccer", "A", "B")
        assert (book, fav) == ("1xbet", "A")

    def test_sans_prix_executable_rien(self):
        assert choisir_bloc_h2h({}, "soccer", "A", "B") == (None, {}, 0.0, "")
        assert choisir_bloc_h2h({"1xbet": {"1": 1.95, "X": 0, "2": 4.0}},
                                "soccer", "A", "B")[0] is None      # foot sans nul : refus

    def test_le_moteur_emet_au_prix_du_bloc_gagnant_et_nomme_le_book(self):
        m = {"id": "m1", "commence_time": (_now() + timedelta(hours=4)).isoformat(),
             "odds_1xbet":   {"1": 1.95, "X": 3.50, "2": 4.00},
             "h2h_par_book": {"1xbet":  {"1": 1.95, "X": 3.50, "2": 4.00},
                              "bet365": {"1": 2.05, "X": 3.50, "2": 4.00}},
             "odds_pinnacle": self.PIN}
        out: list = []
        run_engine._process_h2h(m, "A vs B", "soccer", "L1", "A", "B", "⚽",
                                out, None, _now(), log, min_edge=1.0)
        assert len(out) == 1, "le bloc Bet365 porte l'edge, 1xbet non"
        sig = out[0]
        assert sig["soft_book"] == "bet365"
        assert sig["selection_name"] == "A"
        seul_1xbet: list = []
        m1 = {**m, "h2h_par_book": {"1xbet": m["h2h_par_book"]["1xbet"]}}
        run_engine._process_h2h(m1, "A vs B", "soccer", "L1", "A", "B", "⚽",
                                seul_1xbet, None, _now(), log, min_edge=1.0)
        assert seul_1xbet == [], "au prix 1xbet seul, pas de signal"

    def test_la_cote_du_nul_vient_du_bloc_retenu(self):
        assert run_engine._dnb_draw_odd({"1": 2.05, "X": 3.50, "2": 4.00}, "soccer") == 3.50
        assert run_engine._dnb_draw_odd({"1": 2.05, "X": 3.50, "2": 4.00}, "tennis") == 0.0


# ── 3. Le book voyage jusqu'à la fiche ─────────────────────────────────────

class TestLeBookVoyage:
    def _emit(self, **kw):
        signals: list = []
        run_engine._emit(signals, None, _now(), log, "Lille vs Reims", "soccer",
                         "L1", "totals_over", "SOC Over 2.5", 1.30, 1.25, 0.83, "⚽",
                         selection_name="Over 2.5", min_edge=1.5,
                         match_time=(_now() + timedelta(hours=4)).isoformat(), **kw)
        return signals

    def test_le_signal_porte_son_book_et_la_persistance_le_garde(self):
        (sig,) = self._emit(soft_book="bet365")
        assert sig["soft_book"] == "bet365"
        assert "soft_book" in run_engine._OPTIONAL_COLS, \
            "sans ça, le repli « schéma en retard » ne saurait pas retirer la colonne"
        assert "soft_book" not in run_engine._FIGES_AU_RAFRAICHISSEMENT, \
            "le book accompagne le prix : rafraîchi avec lui, jamais figé à part"

    def test_un_prix_non_attribuable_reste_sans_book(self):
        """Repli sharp, slate d'avant : NULL, jamais le book de référence —
        c'est l'étiquette « 1XBET » sur un prix Bet365 qu'on retire."""
        (sig,) = self._emit()
        assert sig["soft_book"] is None

    def test_les_marches_a_ligne_prennent_le_book_du_cote_retenu(self):
        m = {"id": "m1", "commence_time": (_now() + timedelta(hours=4)).isoformat(),
             "totals_1xbet":    {"point": 2.5, "over": 2.10, "under": 1.80,
                                 "books": {"over": "bet365", "under": "1xbet"}},
             "totals_pinnacle": {"point": 2.5, "over": 1.90, "under": 1.95}}
        out: list = []
        run_engine._process_totals(m, "A vs B", "soccer", "L1", "⚽",
                                   out, None, _now(), log, min_edge=1.0)
        assert [s["soft_book"] for s in out] == ["bet365"]

    def test_telegram_dit_chez_qui(self):
        s = {"sport": "soccer", "match": "A vs B", "market_key": "totals_over",
             "selection_name": "Over 2.5", "executable_odd": 1.85, "edge_pct": 3.2,
             "soft_book": "bet365"}
        bloc = run_engine._signal_block(s, _now())
        assert "`@ 1.85` · valeur `+3.2%` · chez *bet365*" in bloc
        s.pop("soft_book")
        assert "chez" not in run_engine._signal_block(s, _now())

    def test_le_ledger_recopie_le_book(self):
        import inspect
        from core import db
        src = inspect.getsource(db.log_to_ledger)
        assert '"soft_book":' in src and 'sig.get("soft_book")' in src

    def test_le_cache_reprice_garde_les_blocs_par_book(self):
        assert "h2h_par_book" in run_engine._SLATE_KEYS


# ── Fusion entre SOURCES : book par book, jamais une issue d'un book dans
#    le bloc d'un autre ──────────────────────────────────────────────────────

class TestFusionEntreSources:
    def _m(self, home, away, par_book):
        ref = par_book[min(par_book, key=lambda b: ["1xbet", "bet365"].index(b))]
        return {"match": f"{home} vs {away}", "home": home, "away": away,
                "odds_1xbet": dict(ref), "h2h_par_book": {b: dict(v) for b, v in par_book.items()}}

    def test_deux_sources_fusionnent_book_par_book(self):
        existing = self._m("A", "B", {"1xbet": {"1": 2.00, "X": 3.1, "2": 3.5}})
        cand = self._m("A", "B", {"1xbet":  {"1": 2.10, "X": 3.0, "2": 3.6},
                                  "bet365": {"1": 2.05, "X": 3.2, "2": 3.4}})
        assert harvester._fusionner_h2h(existing, cand) is True
        assert existing["h2h_par_book"]["1xbet"] == {"1": 2.10, "X": 3.1, "2": 3.6}
        assert existing["h2h_par_book"]["bet365"] == {"1": 2.05, "X": 3.2, "2": 3.4}
        assert existing["odds_1xbet"] == existing["h2h_par_book"]["1xbet"]

    def test_sans_attribution_on_retombe_sur_le_meilleur_par_issue(self):
        existing = {"odds_1xbet": {"1": 2.00, "X": 3.1, "2": 3.5}}
        cand = {"odds_1xbet": {"1": 2.10, "X": 3.0, "2": 3.6}}
        assert harvester._fusionner_h2h(existing, cand) is True
        assert existing["odds_1xbet"] == {"1": 2.10, "X": 3.1, "2": 3.6}


# ── La migration et la doc qui vont avec ──────────────────────────────────

def test_la_migration_pose_la_colonne_sur_les_quatre_tables():
    import pathlib
    sql = (pathlib.Path(__file__).resolve().parent.parent
           / "sql" / "migrate_v10_13_soft_book.sql").read_text(encoding="utf-8")
    for table in ("signals", "ai_learning_ledger", "signals_archive", "ai_learning_ledger_archive"):
        assert f"ALTER TABLE {table}\n  ADD COLUMN IF NOT EXISTS soft_book text" in sql, table
    assert "DELETE" not in sql.upper() or "-- " in sql   # règle 9 : jamais de suppression


@pytest.mark.parametrize("nom", ["1xbet", "bet365"])
def test_chaque_book_de_la_liste_est_servi_par_au_moins_une_source(nom):
    """Règle 13, version book : un book qu'aucune source ne sert n'a pas sa
    place dans la liste. odds-api.io sert les deux slots du compte ; titan007
    ne connaît que les noms de `SOFT_BOOKS` ; OddsAPI, sa table de clés."""
    from core.odds_api import ODDS_API_BOOK_KEYS
    from core.titan007 import SOFT_BOOKS
    servi = nom in ODDS_API_BOOK_KEYS or any(nom.replace(" ", "") == s.replace(" ", "") for s in SOFT_BOOKS)
    assert servi, nom
