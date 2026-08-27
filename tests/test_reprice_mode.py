"""
tests/test_reprice_mode.py — le mode REPRICE (2026-08-22).

REPRICE=1 relit le slate soft photographié par les scans complets
(meta.cache_soft_slate) et le recompare à un prix sharp Matchbook FRAIS —
l'équivalent d'un « odds screen » professionnel, à coût zéro. L'invariant
absolu du mode : AUCUNE source payante ni recherche web n'est touchée —
chaque fetch interdit est remplacé ici par une sentinelle qui fait échouer
le test s'il est appelé.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import run_engine as eng


# ── FakeSB : meta (key/value TEXT JSON) + signals (lignes dict) ──────────

class _R:
    def __init__(self, data):
        self.data = data


class _MetaQ:
    def __init__(self, store):
        self.store, self._key = store, None

    def select(self, *_a, **_k):
        return self

    def eq(self, _col, val):
        self._key = val
        return self

    def maybe_single(self):
        return self

    def upsert(self, row, **_k):
        self.store[row["key"]] = row
        return self

    def execute(self):
        return _R(self.store.get(self._key))


class _SignalsQ:
    def __init__(self, rows):
        self.rows = rows
        self._filters, self._update, self._insert = {}, None, None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def update(self, payload):
        self._update = dict(payload)
        return self

    def insert(self, payload):
        self._insert = dict(payload)
        return self

    def delete(self):
        self._update = None
        self._insert = None
        return self

    def execute(self):
        if self._insert is not None:
            row = dict(self._insert)
            row.setdefault("id", len(self.rows) + 1)
            self.rows.append(row)
            return _R([row])
        if self._update is not None:
            hit = [r for r in self.rows
                   if all(r.get(c) == v for c, v in self._filters.items())]
            for r in hit:
                r.update(self._update)
            return _R(hit)
        return _R([r for r in self.rows
                   if all(r.get(c) == v for c, v in self._filters.items())])


class FakeSB:
    def __init__(self):
        self.meta = {}
        self.signals = []

    def table(self, name):
        return _MetaQ(self.meta) if name == "meta" else _SignalsQ(self.signals)


def _meta_row(value, age_h=0.5):
    return {"value": json.dumps(value),
            "updated_at": (datetime.now(timezone.utc)
                           - timedelta(hours=age_h)).isoformat()}


def _slate_match(hours_ahead=5.0):
    ko = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
    return {
        "id": "rp1", "match": "Boca Juniors vs River Plate",
        "home": "Boca Juniors", "away": "River Plate",
        "league": "Copa", "sport": "soccer",
        "commence_time": ko.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "odds_1xbet": {"1": 1.75, "X": 4.20, "2": 7.00},
        "_soft_source": "odds_api_io",
    }


def _mb_prices():
    # Chiffres calés sur le régime post-A2 : le DNB exécutable du soft vaut
    # 1.3333 contre une probabilité sharp de 0,8171, soit +8,94 % d'EV brute
    # et +3,50 % NETTE de la taxe de 20 %. L'ancien jeu (soft 2.10/3.55/4.00
    # contre 2.00/3.60/4.20) donnait +2,49 % brut mais −4,42 % net : le signal
    # ne sortait plus, et le test ne mesurait plus le mode REPRICE.
    return {"boca juniors_river plate": {
        "1": 1.55, "X": 4.00, "2": 6.50,
        "home": "Boca Juniors", "away": "River Plate", "_source": "matchbook",
    }}


def _sentinel(name):
    def _boom(*_a, **_k):
        raise AssertionError(f"{name} appelé en mode REPRICE — source interdite")
    return _boom


def cabler_reprice(monkeypatch):
    """run() câblé en REPRICE pur : sentinelles sur tout ce qui paie.

    Fonction ORDINAIRE, et non fixture, pour qu'un autre fichier de tests
    puisse la réutiliser sans importer une fixture — un import de fixture
    oblige à réexporter un nom que le paramètre de test masque ensuite, ce que
    pyflakes signale à juste titre. La fixture `reprice_env` ci-dessous n'est
    plus qu'une enveloppe.
    """
    sb = FakeSB()
    telegrams = []

    monkeypatch.setattr(eng, "REPRICE", True)
    monkeypatch.setattr(eng, "GOLDEN_HOUR", False)
    monkeypatch.setattr(eng, "GUERRILLA", False)
    monkeypatch.delenv("BETFAIR_APP_KEY", raising=False)

    monkeypatch.setattr(eng, "_arm_global_timeout", lambda: None)
    monkeypatch.setattr(eng, "get_db", lambda write=True: sb)
    monkeypatch.setattr(eng, "_purge_old_signals", lambda _sb: None)
    monkeypatch.setattr(eng, "_load_thresholds", lambda _sb: {})
    monkeypatch.setattr(eng, "_load_segment_thresholds", lambda _sb: {})
    monkeypatch.setattr(eng, "_load_edge_ceilings", lambda _sb: {})
    monkeypatch.setattr(eng, "_load_odds_ceilings", lambda _sb: {})
    monkeypatch.setattr(eng, "_load_sport_ranking", lambda _sb: [])
    monkeypatch.setattr(eng.time, "sleep", lambda _s: None)
    monkeypatch.setattr(eng._risk_manager, "check_circuit_breaker", lambda _sb: False)
    monkeypatch.setattr(eng, "_suggest_systems_by_window",
                        lambda _sigs, _log, _sb=None: [])
    monkeypatch.setattr(eng, "_last_look_reprice", lambda s, _log: s)
    monkeypatch.setattr(eng, "_telegram", lambda t: telegrams.append(t))

    # Sentinelles : toute source payante ou recherche web fait ÉCHOUER le test.
    for fn in ("fetch_odds", "fetch_matches", "fetch_pinnacle_prices",
               "fetch_estimated_prices",
               "fetch_betfair_prices", "_api_sports_all", "_odds_api_io_all",
               "_titan007_fetch", "capture_from_scan"):
        monkeypatch.setattr(eng, fn, _sentinel(fn))

    mb_calls = []

    def _mb_fetch(**kw):
        mb_calls.append(kw)
        return _mb_prices()
    monkeypatch.setattr(eng, "fetch_matchbook_prices", _mb_fetch)

    return sb, telegrams, mb_calls


@pytest.fixture
def reprice_env(monkeypatch):
    return cabler_reprice(monkeypatch)


def test_reprice_emits_from_cache_without_touching_paid_sources(reprice_env):
    sb, _telegrams, mb_calls = reprice_env
    sb.meta["cache_soft_slate"] = _meta_row([_slate_match()])
    eng.run()
    assert mb_calls, "Matchbook doit être interrogé (source gratuite du mode)"
    saved = [r for r in sb.signals if r.get("status") == "active"]
    assert len(saved) == 1
    assert saved[0]["match"] == "Boca Juniors vs River Plate"
    assert saved[0]["edge_pct"] > 1.5          # EV vraie au-dessus du plancher
    # Le slate n'a PAS été ré-écrit (sinon le TTL ne serait jamais atteint).
    assert json.loads(sb.meta["cache_soft_slate"]["value"])[0]["id"] == "rp1"
    original_ts = _meta_row([_slate_match()])["updated_at"][:13]
    assert sb.meta["cache_soft_slate"]["updated_at"][:13] == original_ts


def test_reprice_empty_cache_exits_quietly(reprice_env):
    sb, telegrams, mb_calls = reprice_env
    eng.run()   # aucun cache_soft_slate
    assert mb_calls == [], "cache vide → exit AVANT le fetch Matchbook"
    assert telegrams == [], "un tick muet ne spamme pas Telegram"
    assert "harvest_empty_at" not in sb.meta, "le coupe-circuit n'est pas touché"
    assert "last_scan" in sb.meta            # heartbeat quand même


def test_reprice_expired_cache_is_a_miss(reprice_env):
    sb, _t, mb_calls = reprice_env
    sb.meta["cache_soft_slate"] = _meta_row([_slate_match()],
                                            age_h=eng._TTL_SOFT_SLATE + 1)
    eng.run()
    assert mb_calls == []
    assert sb.signals == []


def test_reprice_discards_matches_without_sharp_price(reprice_env):
    sb, _t, _mb = reprice_env
    unmatched = _slate_match()
    unmatched.update({"id": "rp2", "match": "Nacional vs Penarol",
                      "home": "Nacional", "away": "Penarol"})
    sb.meta["cache_soft_slate"] = _meta_row([_slate_match(), unmatched])
    eng.run()
    # Matchbook ne couvre que Boca-River : Nacional-Penarol est écarté SANS
    # recherche web (les sentinelles l'auraient dit).
    assert all(r["match"] == "Boca Juniors vs River Plate" for r in sb.signals)


class TestTrimSoftSlate:
    def test_keeps_only_needed_keys(self):
        m = dict(_slate_match(), totals_1xbet={"over": 1.9, "under": 1.9, "point": 2.5},
                 totals_pinnacle={"over": 1.87, "under": 1.93, "point": 2.5},
                 _oracle_price=1.5, extra_junk="x")
        (row,) = eng._trim_soft_slate([m])
        assert row["totals_1xbet"]["point"] == 2.5
        for banned in ("totals_pinnacle", "_oracle_price", "extra_junk"):
            assert banned not in row

    def test_match_without_soft_odds_is_dropped(self):
        m = _slate_match()
        del m["odds_1xbet"]
        assert eng._trim_soft_slate([m]) == []

    def test_real_pinnacle_is_kept_but_exchange_and_estimated_are_not(self):
        real = dict(_slate_match(), odds_pinnacle={"1": 2.0, "X": 3.6, "2": 4.2})
        exch = dict(_slate_match(), id="e1",
                    odds_pinnacle={"1": 2.0, "X": 3.6, "2": 4.2}, _exchange="matchbook")
        esti = dict(_slate_match(), id="e2",
                    odds_pinnacle={"1": 2.0, "X": 3.6, "2": 4.2}, _estimated=True)
        rows = eng._trim_soft_slate([real, exch, esti])
        assert "odds_pinnacle" in rows[0]      # Pinnacle réel (api-sports/Titan)
        assert "odds_pinnacle" not in rows[1]  # l'exchange doit repricer frais
        assert "odds_pinnacle" not in rows[2]  # un prix estimé ne se fige pas


def test_full_scan_writes_trimmed_slate(reprice_env, monkeypatch):
    """Un scan NON-reprice photographie le slate pour le tick suivant."""
    sb, _t, _mb = reprice_env
    monkeypatch.setattr(eng, "REPRICE", False)
    monkeypatch.setattr(eng, "GUERRILLA", True)
    match = dict(_slate_match(), junk_key="drop-me")
    monkeypatch.setattr(eng, "fetch_matches", lambda: [match])
    monkeypatch.setattr(eng, "fetch_pinnacle_prices", lambda ms: ms)
    eng.run()
    slate = json.loads(sb.meta["cache_soft_slate"]["value"])
    assert len(slate) == 1 and slate[0]["id"] == "rp1"
    assert "junk_key" not in slate[0]


def test_dedup_systems_for_telegram(reprice_env):
    sb, _t, _mb = reprice_env
    system = {"k": 1, "legs": [{"match_id": "rp1", "market_key": "h2h",
                                "selection_name": "Boca Juniors"}]}
    first = eng._dedup_systems_for_telegram(sb, [system])
    assert first == [system], "premier passage : le combo part"
    second = eng._dedup_systems_for_telegram(sb, [system])
    assert second == [], "même combo dans le TTL : silence"
    other = {"k": 1, "legs": [{"match_id": "rp9", "market_key": "h2h",
                               "selection_name": "River Plate"}]}
    assert eng._dedup_systems_for_telegram(sb, [other]) == [other], \
        "un combo DIFFÉRENT n'est pas bloqué"
