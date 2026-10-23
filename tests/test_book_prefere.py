"""
tests/test_book_prefere.py — 1xbet d'abord, Bet365 en complément identifié
(2026-09-27).

L'opérateur ne joue que le book de référence (premier de EXECUTION_BOOKS). La
fusion gardait le MEILLEUR prix par côté : Bet365 à 2,12 contre 1xbet à 2,08
sur la même ligne envoyait le signal chez Bet365, injouable pour lui, alors que
2,08 passait largement. 48 recommandés sur 141 (34 %) du 09/09 au 27/09.

Contrat :
  1. la fusion garde TOUS les prix (`prix_books`), sans changer le meilleur ;
  2. HANDICAPS : le moteur émet chez le book de référence dès qu'il passe les
     barrières ; sinon Bet365 continue, et chaque sortie le dit ;
  3. TOTAUX (décision opérateur du 2026-10-23, « le book sur lequel je joue
     n'est pas important, l'essentiel c'est que le match existe dans
     1xbet ») : le meilleur prix à ligne égale, si 1xbet cote le match ;
  4. aucune équivalence entre lignes différentes (+0.5 ≠ +0.25/+0.75).
"""
import logging
from datetime import datetime, timedelta, timezone

import run_engine
import run_rapport
from core.constants import EXECUTION_BOOKS
from core.execution_books import (MEILLEUR_PRIX_MARCHES, avertissement_hors_reference,
                                  fusionner_lignes, prix_du_book, prix_reference,
                                  reference_cote_le_match)

log = logging.getLogger("test")
REF, SECOND = EXECUTION_BOOKS


def _now():
    return datetime.now(timezone.utc)


def _totals(par_book):
    return {"id": "m1", "commence_time": (_now() + timedelta(hours=5)).isoformat(),
            "totals_1xbet": fusionner_lignes(par_book, "totals"),
            "totals_pinnacle": {"over": 1.90, "under": 1.90, "point": 2.5}}


def _run(m):
    out: list = []
    run_engine._process_totals(m, "A vs B", "soccer", "L1", "⚽", out, None, _now(), log,
                               min_edge=1.0)
    return out


class TestFusion:
    def test_tous_les_prix_sont_gardes_le_meilleur_ne_change_pas(self):
        f = fusionner_lignes({REF: {"point": 2.5, "over": 2.08, "under": 1.70},
                              SECOND: {"point": 2.5, "over": 2.12, "under": 1.68}}, "totals")
        assert f["over"] == 2.12 and f["books"]["over"] == SECOND       # inchangé
        assert prix_du_book(f, "over", REF) == 2.08
        assert prix_du_book(f, "over", SECOND) == 2.12
        assert prix_reference(f, "over") == (REF, 2.08)

    def test_sans_la_cle_rien(self):
        """Source non fusionnée (titan007, slate ancien) : comportement d'avant."""
        assert prix_du_book({"over": 2.1}, "over", REF) == 0.0
        assert prix_reference({}, "over") == (REF, 0.0)


class TestMoteur:
    """Totaux : MEILLEUR prix à ligne égale (décision opérateur 2026-10-23),
    pourvu que 1xbet cote le match. Handicaps : référence d'abord (09-27)."""

    def test_totaux_le_meilleur_prix_est_retenu(self):
        out = _run(_totals({REF: {"point": 2.5, "over": 2.08, "under": 1.70},
                            SECOND: {"point": 2.5, "over": 2.12, "under": 1.68}}))
        assert len(out) == 1
        assert out[0]["soft_book"] == SECOND and out[0]["executable_odd"] == 2.12

    def test_totaux_la_reference_ne_passe_pas_bet365_continue(self):
        out = _run(_totals({REF: {"point": 2.5, "over": 1.98, "under": 1.80},
                            SECOND: {"point": 2.5, "over": 2.12, "under": 1.68}}))
        assert len(out) == 1
        assert out[0]["soft_book"] == SECOND and out[0]["executable_odd"] == 2.12

    def test_totaux_ligne_cotee_par_bet365_seul_le_match_existe_chez_1xbet(self):
        """1xbet ne cote que 2.75 : la ligne 2.5 du sharp n'existe que chez
        Bet365. Le match existe chez 1xbet : signal Bet365, aucun prix 1xbet
        inventé d'une autre ligne."""
        out = _run(_totals({REF: {"point": 2.75, "over": 2.40, "under": 1.55},
                            SECOND: {"point": 2.5, "over": 2.12, "under": 1.68}}))
        assert len(out) == 1
        assert out[0]["soft_book"] == SECOND and out[0]["executable_odd"] == 2.12

    def test_totaux_match_absent_chez_1xbet_aucun_signal(self):
        """« L'essentiel c'est que le match existe dans 1xbet » (2026-10-23)."""
        out = _run(_totals({SECOND: {"point": 2.5, "over": 2.12, "under": 1.68}}))
        assert out == []

    def test_totaux_le_1x2_1xbet_suffit_a_prouver_le_match(self):
        m = _totals({SECOND: {"point": 2.5, "over": 2.12, "under": 1.68}})
        m["h2h_par_book"] = {REF: {"1": 2.0, "X": 3.4, "2": 3.6}}
        out = _run(m)
        assert len(out) == 1 and out[0]["soft_book"] == SECOND

    def test_totaux_la_reference_meilleure_rien_ne_change(self):
        out = _run(_totals({REF: {"point": 2.5, "over": 2.12, "under": 1.70},
                            SECOND: {"point": 2.5, "over": 2.08, "under": 1.68}}))
        assert out[0]["soft_book"] == REF and out[0]["executable_odd"] == 2.12

    def test_handicaps_la_reference_d_abord(self):
        bloc = fusionner_lignes({REF: {"point": -0.5, "home": 1.98, "away": 1.90},
                                 SECOND: {"point": -0.5, "home": 2.08, "away": 1.86}}, "spreads")
        sides: list = []
        run_engine._emit_book_prefere(bloc, "home", 2.08, sides, None, _now(), log,
                                      "A vs B", "soccer", "L1", "spreads_home", "SOC PS -0.5",
                                      1.90, 0.52, "⚽", selection_name="A -0.5", min_edge=1.0,
                                      match_time=(_now() + timedelta(hours=5)).isoformat())
        assert len(sides) == 1
        assert sides[0]["soft_book"] == REF and sides[0]["executable_odd"] == 1.98

    def test_seuls_les_totaux_sont_au_meilleur_prix(self):
        assert MEILLEUR_PRIX_MARCHES == frozenset({"totals"})


class TestAffichage:
    def test_avertissement(self):
        assert avertissement_hors_reference(REF) is None
        assert avertissement_hors_reference(None) is None
        assert avertissement_hors_reference("pinnacle") is None      # inconnu : muet
        texte = avertissement_hors_reference("Bet365", "spreads_home")
        assert SECOND in texte and REF in texte and "*" not in texte and "_" not in texte
        # Totaux au meilleur prix (2026-10-23) : Bet365 n'y est pas un repli.
        assert avertissement_hors_reference("Bet365", "totals_over") is None

    def test_reference_cote_le_match(self):
        f = fusionner_lignes({SECOND: {"point": 2.5, "over": 2.1, "under": 1.7}}, "totals")
        assert not reference_cote_le_match(None, f)
        assert reference_cote_le_match({REF: {"1": 2.0}}, f)
        g = fusionner_lignes({REF: {"point": 3.5, "over": 3.0, "under": 1.4}}, "totals")
        assert reference_cote_le_match(None, f, g)
        assert reference_cote_le_match(None, {"over": 2.1, "point": 2.5})    # non attribué : on ne sait pas

    def _sig(self, book, market_key="spreads_home"):
        return {"match": "A vs B", "sport": "soccer", "selection_name": "A -0.5",
                "market_key": market_key, "executable_odd": 2.12, "xbet_odd": 2.12,
                "edge_pct": 6.0, "soft_book": book, "risk_flag": "VALUE"}

    def test_telegram(self):
        dehors = run_engine._signal_block(self._sig(SECOND), _now())
        dedans = run_engine._signal_block(self._sig(REF), _now())
        assert "⚠️" in dehors and "seulement" in dehors and "seulement" not in dedans
        for t in (dehors, dedans):
            assert t.count("*") % 2 == 0 and t.count("`") % 2 == 0

    def test_totaux_bet365_nomme_sans_avertissement(self):
        t = run_engine._signal_block(self._sig(SECOND, "totals_over"), _now())
        d = run_rapport._signal_line(self._sig(SECOND, "totals_over"), _now())
        assert f"chez *{SECOND}*" in t and "seulement" not in t
        assert f"chez *{SECOND}*" in d and "seulement" not in d

    def test_digest_nomme_le_book(self):
        dehors = run_rapport._signal_line(self._sig(SECOND), _now())
        dedans = run_rapport._signal_line(self._sig(REF), _now())
        assert f"chez *{SECOND}*" in dehors and "seulement" in dehors
        assert f"chez *{REF}*" in dedans and "seulement" not in dedans
        for t in (dehors, dedans):
            assert t.count("*") % 2 == 0 and t.count("`") % 2 == 0

    def test_les_books_restent_ceux_de_loperateur(self):
        """Règle 11 : rien ne coupe Bet365."""
        assert EXECUTION_BOOKS == ("1xbet", "bet365")
