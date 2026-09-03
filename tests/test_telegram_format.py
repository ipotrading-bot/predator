"""
tests/test_telegram_format.py — format opérateur des messages Telegram.

Demande opérateur 2026-07-21 : les messages ne doivent plus contenir que
l'événement, le favori, la sélection, la cote, l'heure et la valeur. Les
mises Kelly et la bankroll de référence (1000€) sont supprimées — elles
affichaient "Mise 0€ · EV net taxe +0.01€" sur presque chaque ligne, et dans
run_rapport un stake nul faisait carrément DISPARAÎTRE le signal du rapport.

Ces tests verrouillent l'absence de mise/bankroll et la présence des six
champs demandés, sans aucun envoi réseau (_telegram est monkeypatché).
"""
from datetime import datetime, timezone

import pytest

import run_engine
import run_rapport


NOW = datetime(2026, 7, 21, 23, 8, tzinfo=timezone.utc)


def _sig(**over) -> dict:
    """Signal EN MÉMOIRE, tel que `run_engine._emit` le produit.

    Le prix s'y nomme `executable_odd` depuis le 2026-08-27 : c'est la cote
    réellement jouable (DNB synthétique en football), et plus le prix
    dévigorisé que personne n'affiche. Voir `_row()` pour la forme persistée.
    """
    base = {
        "match": "Real Madrid vs Barcelona", "sport": "soccer",
        "league": "La Liga", "market_key": "h2h", "market": "AH 0.0",
        "selection_name": "Barcelona", "executable_odd": 3.40, "sharp_prob": 0.31,
        "edge_pct": 5.4, "risk_flag": "HIGH_VALUE",
        "match_time": "2026-07-21T21:00:00+00:00",
    }
    base.update(over)
    return base


def _row(**over) -> dict:
    """La MÊME ligne, relue de `signals`. La colonne a gardé son nom
    historique `xbet_odd` — `run_engine._save` fait la traduction au point
    unique de persistance. `run_rapport` lit la base, donc lit ce nom-là."""
    row = _sig()
    row["xbet_odd"] = row.pop("executable_odd")
    row.update(over)
    return row


@pytest.fixture
def sent(monkeypatch):
    box: list[str] = []
    monkeypatch.setattr(run_engine, "_telegram", box.append)
    return box


class TestNoStakeNoBankroll:
    @pytest.mark.parametrize("banned", ["Mise", "1000€", "EV net", "Kelly", "€"])
    def test_engine_message_is_money_free(self, sent, banned):
        run_engine._telegram_signals([_sig()], NOW, 46, 0)
        assert banned not in sent[0]

    @pytest.mark.parametrize("banned", ["Mise", "1000€", "Kelly", "€"])
    def test_report_line_is_money_free(self, banned):
        assert banned not in run_rapport._signal_line(_row(), NOW)

    def test_zero_stake_signal_is_still_reported(self):
        # Régression : _signal_line renvoyait None quand la mise Kelly tombait
        # sous MIN_STAKE, effaçant le signal du rapport sans rien dire.
        assert run_rapport._signal_line(_row(xbet_odd=1.01, sharp_prob=0.01), NOW)


class TestRequiredFields:
    def test_event_selection_odds_and_value_are_present(self):
        line = run_rapport._signal_line(_row(), NOW)
        assert "Real Madrid vs Barcelona" in line   # événement
        assert "Barcelona" in line                  # signal proposé
        assert "3.40" in line                       # cote
        assert "+5.4%" in line                      # valeur
        assert "21:00 UTC" in line                  # heure

    def test_kickoff_shows_date_when_not_today(self):
        line = run_rapport._signal_line(_row(match_time="2026-07-22T02:30:00+00:00"), NOW)
        assert "22/07 02:30 UTC" in line

    def test_unknown_kickoff_prints_no_hour_rather_than_a_fake_one(self):
        assert "UTC" not in run_rapport._signal_line(_row(match_time=""), NOW)


class TestFavourite:
    def test_outsider_pick_names_the_favourite(self):
        line = run_rapport._signal_line(_row(), NOW)
        assert "Favori : Real Madrid" in line

    def test_favourite_pick_is_tagged_inline_not_repeated(self):
        line = run_rapport._signal_line(
            _sig(selection_name="Real Madrid", sharp_prob=0.62), NOW)
        assert "(favori)" in line
        assert "Favori :" not in line

    def test_totals_market_claims_no_favourite(self):
        # Aucun moneyline n'est stocké pour un totals — en nommer un serait
        # l'inventer.
        line = run_rapport._signal_line(
            _sig(market_key="totals_under", selection_name="Under 2.75"), NOW)
        assert "avori" not in line

    def test_engine_and_report_agree(self):
        s = _sig()
        assert run_engine._favourite(s) == run_rapport._favourite(s) == "Real Madrid"


class TestEmptyAndSingles:
    def test_no_signals_message_stays_one_short_line(self, sent):
        run_engine._telegram_signals([], NOW, 8, 0)
        assert "Aucun pari recommandé · 8 matchs analysés" in sent[0]
        assert "€" not in sent[0]
        assert "écarté" not in sent[0]

    def test_no_session_jargon_in_header(self, sent):
        # « EU-CLOSE 🎯 🎯 » : libellé interne, icône doublée — parti le 2026-09-03.
        run_engine._telegram_signals([], NOW, 8, 0)
        run_engine._telegram_signals([_sig()], NOW, 8, 0)
        for msg in sent:
            for jargon in ("EU-CLOSE", "OVERNIGHT", "EU-OPEN", "EU-MID"):
                assert jargon not in msg

    def test_fully_phantom_run_counts_its_discards(self, sent):
        # Un scan standard intégralement fantôme ne se tait plus : il compte.
        fantomes = [_sig(shadow_reason="t_minus_2h"), _sig(shadow_reason="t_minus_2h"),
                    _sig(shadow_reason="shadow_sport")]
        run_engine._telegram_signals([], NOW, 13, 0, fantomes)
        assert "Aucun pari recommandé · 13 matchs analysés" in sent[0]
        assert "2 écarté(s) (< 2 h)" in sent[0]
        assert "1 écarté(s) (sport en observation)" in sent[0]

    def test_header_with_signals_also_counts_discards(self, sent):
        run_engine._telegram_signals([_sig()], NOW, 13, 0, [_sig(shadow_reason="t_minus_2h")])
        assert "1 pari(s) recommandé(s) · 13 matchs analysés · 1 écarté(s) (< 2 h)" in sent[0]

    def test_every_signal_is_sent_as_a_single_no_combo(self, sent):
        # Décision opérateur 2026-09-03 : plus de combiné, chaque pari en simple.
        legs = [_sig(), _sig(match="A vs B", selection_name="A", sharp_prob=0.55, edge_pct=2.1)]
        run_engine._telegram_signals(legs, NOW, 46, 0)
        assert "2 pari(s) recommandé(s)" in sent[0]
        assert "Real Madrid vs Barcelona" in sent[0] and "A vs B" in sent[0]
        assert "Combiné" not in sent[0]
        assert "→ Barcelona `@ 3.40` · valeur `+5.4%`" in sent[0]
        assert "→ A (favori) `@ 3.40` · valeur `+2.1%`" in sent[0]

    def test_urgent_kickoff_first(self, sent):
        soon = _sig(match="Soon vs Later", match_time="2026-07-21T23:30:00+00:00", edge_pct=1.5)
        run_engine._telegram_signals([_sig(), soon], NOW, 46, 0)
        assert sent[0].index("Soon vs Later") < sent[0].index("Real Madrid vs Barcelona")

    def test_long_list_is_chunked_between_signals(self, sent):
        many = [_sig(match=f"Club {i} Longnom vs Adversaire {i} Longnom") for i in range(80)]
        run_engine._telegram_signals(many, NOW, 200, 0)
        assert len(sent) > 1
        assert all(len(m) <= 4000 for m in sent)
        body = "".join(sent[1:])
        assert body.count("*Club ") == body.count("valeur `+") == 80
