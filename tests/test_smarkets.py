"""
tests/test_smarkets.py — core/smarkets.py (exchange sharp, sans clé, 2026-09-08).

Troisième exchange derrière Betfair et Matchbook, en COMBLEMENT : un match
qu'aucun autre exchange ne cote n'est plus un « MARCHÉ MORT ». Une erreur
ici ne casse rien de visible — elle fabrique un prix sharp faux, donc un
edge faux. D'où les garde-fous verrouillés ci-dessous :

- une cote Smarkets est une PROBABILITÉ en centièmes de % : 4854 → 2,06 ;
  `offers` se backent (meilleur = prix le plus bas), `bids` se layent
  (meilleur = prix le plus haut). Inverser les deux donnerait un milieu
  décalé du mauvais côté sur chaque match ;
- carnet vide d'un côté, croisé ou trop large → pas de prix ;
- « A at B » (US) inverse domicile/extérieur, comme chez Matchbook ;
- le handicap asiatique porte la ligne DOMICILE signée dans `param` ;
- budget journalier tenu par core/daily_quota, 429 rejoué UNE fois,
  401/403/451 → {} en le disant ;
- règle 13 : critère de retrait daté dans la docstring, source au registre,
  appelée par le moteur hors REPRICE, sondée par ops.py.
"""
import logging
import pathlib

import pytest

import core.smarkets as sm

RACINE = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _sans_reseau_ni_pause(monkeypatch):
    compteur = {"smarkets": 0}
    monkeypatch.setattr(sm.daily_quota, "spent", lambda b: compteur.get(b, 0))
    monkeypatch.setattr(sm.daily_quota, "add", lambda b, n: compteur.__setitem__(b, compteur.get(b, 0) + n))
    monkeypatch.setattr(sm, "_PAUSE_S", 0.0)
    monkeypatch.setattr(sm, "_RETRY_429_S", 0.0)
    yield compteur


def _q(bids, offers):
    return {"bids": [{"price": p, "quantity": 1000} for p in bids],
            "offers": [{"price": p, "quantity": 1000} for p in offers]}


# ── Cotes ──────────────────────────────────────────────────────────────────

def test_une_cote_est_une_probabilite_en_centiemes_de_pour_cent():
    assert sm._odds(5000) == 2.0
    assert sm._odds(4854) == pytest.approx(2.0601, abs=1e-3)
    assert sm._odds(0) is None and sm._odds(10000) is None and sm._odds("x") is None


def test_le_milieu_prend_le_meilleur_offer_en_back_et_le_meilleur_bid_en_lay():
    """Relevé réel (Al-Qadisiyah) : bids 4854/4808…, offers 4902/4950… →
    back = 10000/4902 = 2.04, lay = 10000/4854 = 2.06, milieu 2.05."""
    mid = sm.mid_from_quote(_q(bids=[4854, 4808, 4673], offers=[4902, 4950, 5000]))
    assert mid == pytest.approx(2.05, abs=0.005)


def test_carnet_vide_croise_ou_trop_large_ne_donne_pas_de_prix():
    assert sm.mid_from_quote(None) is None
    assert sm.mid_from_quote(_q(bids=[], offers=[4902])) is None       # une seule jambe
    assert sm.mid_from_quote(_q(bids=[4902], offers=[4854])) is None   # croisé : lay < back
    assert sm.mid_from_quote(_q(bids=[9000], offers=[3000])) is None   # 1.11 contre 3.33


# ── Marchés ────────────────────────────────────────────────────────────────

def _contracts(mid, *names):
    return [{"id": f"{mid}-{i}", "market_id": mid, "name": n} for i, n in enumerate(names)]


def test_le_1x2_lit_les_trois_issues_et_refuse_une_somme_de_probas_perimee():
    cs = _contracts("m1", "Al-Qadisiyah", "Draw", "Al Ahli Jeddah")
    quotes = {"m1-0": _q([4854], [4902]), "m1-1": _q([2500], [2564]), "m1-2": _q([2564], [2632])}
    odds = sm.winner_odds(cs, quotes, "Al-Qadisiyah", "Al Ahli Jeddah")
    assert odds and odds["1"] == pytest.approx(2.05, abs=0.01) and odds["X"] > 3.9 and odds["2"] > 3.8
    # Carnet périmé : trois issues à 1.5 → somme 2.0, hors [0.90 ; 1.12]
    perime = {k: _q([6600], [6700]) for k in quotes}
    assert sm.winner_odds(cs, perime, "Al-Qadisiyah", "Al Ahli Jeddah") is None


def test_un_nom_qui_matche_les_deux_equipes_nest_attribue_a_aucune():
    assert sm._side("America", "America MG", "America RN") is None
    assert sm._side("Draw", "A", "B") == "X"


def test_le_total_prend_la_ligne_dans_param_et_les_contrats_over_under():
    m = {"market_type": {"name": "OVER_UNDER", "param": 2.5}}
    cs = _contracts("t", "Under 2.5 goals", "Over 2.5 goals")
    row = sm.totals_row(m, cs, {"t-0": _q([5000], [5100]), "t-1": _q([4900], [5000])})
    assert row["point"] == 2.5 and row["under"] > 1.9 and row["over"] > 1.9


def test_le_handicap_asiatique_porte_la_ligne_domicile_signee():
    """« Asian Handicap A -1.5 / B +1.5 » : param = -1.5 (domicile), les
    contrats « A -1.5 » / « B +1.5 » désignent l'équipe par leur nom."""
    m = {"market_type": {"name": "ASIAN_HANDICAP", "param": -1.5}}
    cs = _contracts("h", "Al-Qadisiyah -1.5", "Al Ahli Jeddah +1.5")
    row = sm.handicap_row(m, cs, {"h-0": _q([4000], [4100]), "h-1": _q([5900], [6000])},
                          "Al-Qadisiyah", "Al Ahli Jeddah")
    assert row["point"] == -1.5 and row["away_point"] == 1.5
    assert row["home"] > 2.4 and row["away"] < 1.7


# ── Réseau, budget, retrait ────────────────────────────────────────────────

class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload


def _cabler(monkeypatch, *, events_status=200, quotes_429_first=False):
    from datetime import datetime, timedelta, timezone
    start = (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    appels = {"n": 0, "quotes": 0, "urls": []}

    def fake_get(url, params=None, headers=None, timeout=None):
        appels["n"] += 1
        appels["urls"].append(url)
        if url.endswith("/events/"):
            return _Resp({"events": [{"id": "1", "name": "Raiders at Texans", "start_datetime": start},
                                     {"id": "2", "name": "A vs B", "start_datetime": start}]},
                         status=events_status)
        if url.endswith("/markets/"):
            return _Resp({"markets": [
                {"id": "w1", "event_id": "1", "state": "open", "market_type": {"name": "WINNER_2_WAY"}},
                {"id": "w2", "event_id": "2", "state": "open", "market_type": {"name": "WINNER_3_WAY"}},
                {"id": "t2", "event_id": "2", "state": "open", "market_type": {"name": "OVER_UNDER", "param": 2.5}},
                {"id": "x2", "event_id": "2", "state": "open", "market_type": {"name": "FIRST_HALF_OVER_UNDER", "param": 0.5}},
            ]})
        if url.endswith("/contracts/"):
            return _Resp({"contracts": _contracts("w1", "Raiders", "Texans")
                          + _contracts("w2", "A", "Draw", "B") + _contracts("t2", "Over 2.5 goals", "Under 2.5 goals")})
        if url.endswith("/quotes/"):
            appels["quotes"] += 1
            if quotes_429_first and appels["quotes"] == 1:
                return _Resp({}, status=429)
            return _Resp({"w1-0": _q([6600], [6700]), "w1-1": _q([3300], [3400]),
                          "w2-0": _q([4854], [4902]), "w2-1": _q([2500], [2564]), "w2-2": _q([2564], [2632]),
                          "t2-0": _q([5000], [5100]), "t2-1": _q([4900], [5000])})
        raise AssertionError(url)

    monkeypatch.setattr(sm.requests, "get", fake_get)
    return appels


def test_bout_en_bout_at_inverse_domicile_et_le_sous_marche_est_ignore(monkeypatch):
    appels = _cabler(monkeypatch)
    out = sm.fetch_smarkets_prices(sports=["soccer"], hours_ahead=24)
    assert set(out) == {"texans_raiders", "a_b"}
    texans = out["texans_raiders"]
    # Houston reçoit (« at ») : Texans = « 1 » (3.0), Raiders = « 2 » (1.5)
    assert texans["home"] == "Texans" and texans["away"] == "Raiders"
    assert texans["2"] < 1.6 < 2.9 < texans["1"]
    ab = out["a_b"]
    assert ab["_source"] == "smarkets" and ab["X"] > 3.9
    assert ab["totals"]["point"] == 2.5 and [r["point"] for r in ab["totals"]["ladder"]] == [2.5]
    assert "spreads" not in ab
    assert not any("x2" in u for u in appels["urls"]), "FIRST_HALF_OVER_UNDER n'est jamais demandé"


def test_un_429_est_rejoue_une_fois_et_compte_deux_appels(monkeypatch, _sans_reseau_ni_pause):
    appels = _cabler(monkeypatch, quotes_429_first=True)
    out = sm.fetch_smarkets_prices(sports=["soccer"], hours_ahead=24)
    assert len(out) == 2 and appels["quotes"] == 2
    assert _sans_reseau_ni_pause["smarkets"] == appels["n"]


def test_un_geoblocage_rend_vide_en_le_disant(monkeypatch, caplog):
    _cabler(monkeypatch, events_status=403)
    with caplog.at_level(logging.WARNING, logger="PREDATOR.smarkets"):
        assert sm.fetch_smarkets_prices(sports=["soccer"], hours_ahead=24) == {}
    assert any("géoblocage" in r.getMessage() for r in caplog.records)


def test_le_budget_journalier_arrete_la_source(monkeypatch, _sans_reseau_ni_pause, caplog):
    appels = _cabler(monkeypatch)
    _sans_reseau_ni_pause["smarkets"] = sm.DAILY_BUDGET
    with caplog.at_level(logging.WARNING, logger="PREDATOR.smarkets"):
        assert sm.fetch_smarkets_prices(sports=["soccer"], hours_ahead=24) == {}
    assert appels["n"] == 0
    assert any("budget" in r.getMessage() for r in caplog.records)


def test_un_sport_inconnu_ne_coute_rien(monkeypatch):
    appels = _cabler(monkeypatch)
    assert sm.fetch_smarkets_prices(sports=["curling"], hours_ahead=24) == {}
    assert appels["n"] == 0


def test_le_critere_de_retrait_est_ecrit_et_la_source_est_au_registre():
    """AUDIT.md §3bis / règle 13 : critère de retrait DATÉ dans la docstring,
    source au registre, appelée par le moteur hors REPRICE, sondée par
    ops.py. Retirer l'un sans les autres fait tomber ce test."""
    from core.source_adapter import CALL_ORDER
    doc = sm.__doc__ or ""
    assert "CRITÈRE DE RETRAIT" in doc
    assert "2026-09-22" in doc.split("CRITÈRE DE RETRAIT", 1)[1]
    assert "SMARKETS_DAILY_BUDGET" in doc and sm.DAILY_BUDGET == 2000
    assert "smarkets" in CALL_ORDER
    moteur = (RACINE / "run_engine.py").read_text(encoding="utf-8")
    assert "fetch_smarkets_prices(" in moteur
    assert "if not _SMARKETS_OFF and not REPRICE:" in moteur
    ops = (RACINE / "scripts" / "ops.py").read_text(encoding="utf-8")
    assert "from core.smarkets import probe" in ops
