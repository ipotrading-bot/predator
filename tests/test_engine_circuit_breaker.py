"""
tests/test_engine_circuit_breaker.py — run_engine.py : coupe-circuit du
harvest Tier 2 et alertes dédupliquées (v10.3, incident du 2026-08-10).

Contrat :
- un harvest vide pose `meta.harvest_empty_at` ; tant qu'il a moins de
  HARVEST_EMPTY_TTL_H, `_harvest_recently_empty()` rend son âge (→ on saute
  Groq/Tavily) ; un harvest non vide l'efface ;
- `_alert_once()` n'envoie qu'une fois par TTL, et envoie TOUJOURS sans
  Supabase (un doublon vaut mieux qu'un silence) ;
- `_alert_oddsapi_pool_if_dead()` ne parle que si le pool existe ET est
  entièrement mort — « pas de clé » et « pool mort » sont deux messages.
"""
from datetime import datetime, timedelta, timezone

import run_engine as eng
# Appariement slate ↔ exchange : sorti de run_engine.py le 2026-08-26 pour
# que core/closing_line.py puisse l'utiliser sans importer la racine.
from core import exchange_match


class _Q:
    """Chaîne table().select().eq().maybe_single().execute() / upsert().execute()."""
    def __init__(self, store, table):
        self.store, self.table_name, self._key = store, table, None

    def select(self, *_a, **_k): return self
    def eq(self, _col, val): self._key = val; return self
    def maybe_single(self): return self
    def upsert(self, row, **_k): self.store[row["key"]] = row; return self

    def execute(self):
        class R: pass
        r = R(); r.data = self.store.get(self._key); return r


class FakeSB:
    def __init__(self): self.store = {}
    def table(self, name): return _Q(self.store, name)


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def test_empty_harvest_is_remembered_then_forgotten(monkeypatch):
    sb = FakeSB()
    assert eng._harvest_recently_empty(sb) is None
    eng._note_harvest_result(sb, [])
    age = eng._harvest_recently_empty(sb)
    assert age is not None and age < 0.1
    eng._note_harvest_result(sb, [{"match": "A vs B"}])
    assert eng._harvest_recently_empty(sb) is None


def test_old_empty_harvest_does_not_block(monkeypatch):
    sb = FakeSB()
    sb.store["harvest_empty_at"] = {"key": "harvest_empty_at",
                                    "value": _iso(eng._HARVEST_EMPTY_TTL_H + 1)}
    assert eng._harvest_recently_empty(sb) is None


def test_alert_once_dedupes_within_ttl(monkeypatch):
    sent = []
    monkeypatch.setattr(eng, "_telegram", lambda t: sent.append(t))
    sb = FakeSB()
    assert eng._alert_once(sb, "alert_x", "hello") is True
    assert eng._alert_once(sb, "alert_x", "hello") is False
    assert sent == ["hello"]
    sb.store["alert_x"]["value"] = _iso(eng._ALERT_TTL_H + 1)
    assert eng._alert_once(sb, "alert_x", "hello") is True
    assert len(sent) == 2


def test_alert_once_without_db_always_sends(monkeypatch):
    sent = []
    monkeypatch.setattr(eng, "_telegram", lambda t: sent.append(t))
    assert eng._alert_once(None, "alert_x", "a") and eng._alert_once(None, "alert_x", "b")
    assert sent == ["a", "b"]


def test_pool_dead_alert_names_the_cause(monkeypatch):
    sent = []
    monkeypatch.setattr(eng, "_telegram", lambda t: sent.append(t))
    monkeypatch.setattr(eng, "_odds_pool_status",
                        lambda: {"total": 2, "dead": 2, "live": 0, "reason": "HTTP 401"})
    eng._alert_oddsapi_pool_if_dead(FakeSB())
    assert len(sent) == 1 and "2/2" in sent[0] and "rotate_odds_key.py --add" in sent[0]

    sent.clear()
    monkeypatch.setattr(eng, "_odds_pool_status",
                        lambda: {"total": 2, "dead": 1, "live": 1, "reason": "HTTP 401"})
    eng._alert_oddsapi_pool_if_dead(FakeSB())
    assert sent == []                                   # une clé vivante = pas d'alerte

    monkeypatch.setattr(eng, "_odds_pool_status",
                        lambda: {"total": 0, "dead": 0, "live": 0, "reason": ""})
    eng._alert_oddsapi_pool_if_dead(FakeSB())
    assert len(sent) == 1 and "aucune clé" in sent[0]


def test_breaker_still_queries_api_sports(monkeypatch):
    """Le coupe-circuit protège le quota Groq/Tavily, pas la source gratuite.

    Constaté en production (run engine du 2026-08-20 18:30) : le harvest
    entier était sauté, donc api-sports — gratuit, quota propre — ne
    tournait pas, et le scan restait sans aucune source.
    """
    import inspect
    src = inspect.getsource(eng.run)
    head = src[src.index("skipped_age = _harvest_recently_empty(sb)"):]
    branch = head[:head.index("else:")]
    assert "_api_sports_all(" in branch, "api-sports doit rester appelé quand le harvest est sauté"
    assert "_odds_api_io_all(" in branch, (
        "odds-api.io aussi : c'est la source SOFT principale, et la sauter "
        "quand api-sports est en panne laisse le scan sans aucune source")
    assert "fetch_matches()" not in branch, "le harvest web coûteux doit rester sauté"


# ── Retournement des prix d'exchange ──────────────────────────────────

def test_flipping_an_exchange_row_swaps_sides_and_the_handicap_sign():
    """L'exchange peut nommer le match dans l'autre sens. Inverser 1 et 2
    sans inverser le handicap comparerait l'edge à la mauvaise ligne."""
    flipped = exchange_match.flip_exchange_prices({
        "1": 1.50, "X": 4.20, "2": 6.00, "_source": "matchbook",
        "totals": {"over": 1.90, "under": 1.98, "point": 2.5},
        "spreads": {"home": 1.86, "away": 2.17, "point": -1.5, "away_point": 1.5},
    })
    assert (flipped["1"], flipped["2"]) == (6.00, 1.50)
    assert flipped["X"] == 4.20                      # le nul est symétrique
    assert flipped["totals"] == {"over": 1.90, "under": 1.98, "point": 2.5}
    assert flipped["spreads"] == {"home": 2.17, "away": 1.86,
                                  "point": 1.5, "away_point": -1.5}
    assert flipped["_source"] == "matchbook"


def test_flipping_without_side_markets_is_harmless():
    flipped = exchange_match.flip_exchange_prices({"1": 2.0, "X": 3.3, "2": 3.6})
    assert (flipped["1"], flipped["2"]) == (3.6, 2.0)
    assert "totals" not in flipped and "spreads" not in flipped


# ── Enrichissement par l'exchange ─────────────────────────────────────

class _Log:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass


_MB = {"barcelona_real madrid": {"home": "Barcelona", "away": "Real Madrid",
                                 "1": 2.10, "X": 3.60, "2": 3.40, "_source": "matchbook",
                                 "totals": {"over": 1.90, "under": 1.98, "point": 2.5},
                                 "spreads": {"home": 1.86, "away": 2.17,
                                             "point": -1.5, "away_point": 1.5}}}


def test_exchange_fills_a_match_that_has_no_sharp_price():
    """Le cas qui bloquait tout : odds-api.io ramène le prix SOFT, l'exchange
    doit poser le SHARP, sinon aucun edge n'est calculable."""
    m = {"match": "Barcelona vs Real Madrid", "home": "Barcelona", "away": "Real Madrid",
         "odds_1xbet": {"1": 2.30, "X": 3.40, "2": 3.10}}
    assert eng._enrich_from_exchange([m], _MB, _Log()) == 1
    assert m["odds_pinnacle"] == {"1": 2.10, "X": 3.60, "2": 3.40}
    assert m["totals_pinnacle"]["point"] == 2.5
    assert m["spreads_pinnacle"]["point"] == -1.5
    assert m["_exchange"] == "matchbook"


def test_a_real_sharp_price_is_never_overwritten():
    """Pinnacle reste la RÉFÉRENCE : l'exchange ne l'écrase jamais.

    Depuis A5 le match n'est plus sauté pour autant — l'exchange le
    CONTRE-EXPERTISE et, s'il est d'accord (ici 0,89 pt d'écart), entre au
    consensus sous `odds_exchange`. Le compteur d'enrichissement reste à 0 :
    aucun prix n'a été posé. L'invariant de `capture_from_exchange` tient
    donc toujours — voir tests/test_closing_line_exchange.py.
    """
    m = {"match": "Barcelona vs Real Madrid", "home": "Barcelona", "away": "Real Madrid",
         "odds_pinnacle": {"1": 2.05, "X": 3.50, "2": 3.55}}
    assert eng._enrich_from_exchange([m], _MB, _Log()) == 0
    assert m["odds_pinnacle"] == {"1": 2.05, "X": 3.50, "2": 3.55}
    assert m["odds_exchange"] == {"1": 2.10, "X": 3.60, "2": 3.40}
    assert "_sharp_conflict" not in m


def test_la_contre_expertise_pose_AUSSI_totals_et_handicaps():
    """Le trou qui tuait deux marchés sur trois (2026-08-27).

    Ces lignes n'étaient posées que dans le rôle BOUCHE-TROU. Dès qu'un match
    avait déjà un prix sharp 1X2 — titan007 et la recherche web en servent sur
    la quasi-totalité du foot — l'exchange partait en contre-expertise et ses
    totals/handicaps étaient jetés. Aucune autre source ne les cote : le plan
    gratuit d'odds-api.io ne sert aucun book sharp. `_process_totals` et
    `_process_spreads` n'étaient donc JAMAIS appelés, faute des deux côtés.
    Symptôme : le run 19:20 de ce jour-là ne porte pas un seul LINESKIP, alors
    que Matchbook cotait 55 totals et 40 handicaps.
    """
    m = {"match": "Barcelona vs Real Madrid", "home": "Barcelona", "away": "Real Madrid",
         "odds_pinnacle": {"1": 2.05, "X": 3.50, "2": 3.55},
         "totals_1xbet": {"over": 1.95, "under": 1.90, "point": 2.5}}
    assert eng._enrich_from_exchange([m], _MB, _Log()) == 0   # aucun 1X2 posé
    assert m["odds_pinnacle"] == {"1": 2.05, "X": 3.50, "2": 3.55}
    assert m["totals_pinnacle"]["point"] == 2.5
    assert m["spreads_pinnacle"]["point"] == -1.5


def test_un_conflit_sharp_ne_pose_AUCUNE_ligne():
    """Les deux avis sharp se contredisent : l'un des deux carnets est périmé.
    On ne sait pas lequel — donc on n'en tire ni 1X2, ni total, ni handicap."""
    m = {"match": "Barcelona vs Real Madrid", "home": "Barcelona", "away": "Real Madrid",
         "odds_pinnacle": {"1": 1.40, "X": 5.00, "2": 8.00}}
    assert eng._enrich_from_exchange([m], _MB, _Log()) == 0
    assert m["_sharp_conflict"]["pts"] > 0
    assert "totals_pinnacle" not in m and "spreads_pinnacle" not in m


def test_une_ligne_sharp_deja_posee_n_est_jamais_ecrasee():
    m = {"match": "Barcelona vs Real Madrid", "home": "Barcelona", "away": "Real Madrid",
         "odds_pinnacle": {"1": 2.05, "X": 3.50, "2": 3.55},
         "totals_pinnacle": {"over": 1.80, "under": 2.05, "point": 3.5}}
    eng._enrich_from_exchange([m], _MB, _Log())
    assert m["totals_pinnacle"]["point"] == 3.5


def test_an_ai_estimated_price_is_replaced():
    m = {"match": "Barcelona vs Real Madrid", "home": "Barcelona", "away": "Real Madrid",
         "odds_pinnacle": {"1": 2.00, "X": 3.30, "2": 3.80}, "_estimated": True}
    assert eng._enrich_from_exchange([m], _MB, _Log()) == 1
    assert m["odds_pinnacle"]["1"] == 2.10
    assert "_estimated" not in m


def test_match_named_the_other_way_round_is_flipped():
    m = {"match": "Real Madrid vs Barcelona", "home": "Real Madrid", "away": "Barcelona",
         "odds_1xbet": {"1": 3.10, "X": 3.40, "2": 2.30}}
    assert eng._enrich_from_exchange([m], _MB, _Log()) == 1
    assert m["odds_pinnacle"] == {"1": 3.40, "X": 3.60, "2": 2.10}
    assert m["spreads_pinnacle"]["point"] == 1.5      # signe inversé avec les côtés


def test_unknown_match_is_left_untouched():
    m = {"match": "Lorient vs Brest", "home": "Lorient", "away": "Brest",
         "odds_1xbet": {"1": 2.0}}
    assert eng._enrich_from_exchange([m], _MB, _Log()) == 0
    assert "odds_pinnacle" not in m


def test_tier2_matches_are_enriched_before_the_web_search():
    """Le Tier 1.5 tourne AVANT le tri sharp du Tier 2 : sans ce second
    passage, les matchs d'odds-api.io repartaient sans prix sharp (run du
    19:07) — écartés « Échec prix Sharp » alors que Matchbook les cotait."""
    import inspect
    src = inspect.getsource(eng.run)
    after_t2 = src[src.index("xbet_matches = fetch_matches()"):]
    call = after_t2.index("_enrich_from_exchange(xbet_matches")
    tri = after_t2.index("for m in xbet_matches[:MAX_MATCHES]")
    assert call < tri, "l'exchange doit servir AVANT le tri sharp"


# ── Appariement des noms entre fournisseurs ───────────────────────────

_PRICES = {
    "club juventud italiana_delfin sc": {
        "home": "Club Juventud Italiana", "away": "Delfin SC",
        "1": 5.50, "X": 4.10, "2": 1.62, "_source": "matchbook"},
}


def test_exact_key_still_wins():
    m = {"home": "Club Juventud Italiana", "away": "Delfin SC"}
    assert exchange_match.lookup_exchange(m, _PRICES)["1"] == 5.50


def test_fuzzy_match_across_providers():
    """Mesuré le 2026-08-20 : sur 13 matchs, la clé exacte en appariait 0 et
    le flou 8. C'est ce seul écart de nommage qui tenait le pipeline à zéro
    signal alors que les deux sources fonctionnaient."""
    m = {"home": "Cde Juventud Italiana", "away": "Delfin SC"}
    hit = exchange_match.lookup_exchange(m, _PRICES)
    assert hit is not None and hit["1"] == 5.50


def test_fuzzy_match_reversed_flips_the_prices():
    m = {"home": "Delfin SC", "away": "Cde Juventud Italiana"}
    hit = exchange_match.lookup_exchange(m, _PRICES)
    assert hit["1"] == 1.62 and hit["2"] == 5.50


def test_ambiguous_fuzzy_match_is_refused():
    """Deux prétendants = on ne sait pas lequel est le bon. Poser le mauvais
    prix sharp donnerait un edge faux, sans rien casser de visible."""
    prices = dict(_PRICES)
    prices["juventud italiana fc_delfin sc"] = {
        "home": "Juventud Italiana FC", "away": "Delfin SC",
        "1": 5.90, "X": 4.00, "2": 1.60}
    m = {"home": "Cde Juventud Italiana", "away": "Delfin SC"}
    assert exchange_match.lookup_exchange(m, prices) is None


def test_unrelated_match_finds_nothing():
    m = {"home": "Arsenal", "away": "Chelsea"}
    assert exchange_match.lookup_exchange(m, _PRICES) is None


def test_missing_team_names_are_refused():
    assert exchange_match.lookup_exchange({"home": "", "away": "Delfin SC"}, _PRICES) is None


def test_price_row_without_team_names_never_matches():
    """`strict_team_match` rend True si un nom est vide : sans garde, une
    ligne de prix incomplète s'apparierait à tous les matchs du scan."""
    prices = {"x_y": {"1": 2.0, "X": 3.0, "2": 4.0}}          # ni home ni away
    m = {"home": "Club Juventud Italiana", "away": "Delfin SC"}
    assert exchange_match.lookup_exchange(m, prices) is None


def test_very_short_names_are_not_fuzzy_matched():
    prices = {"a_b": {"home": "A", "away": "B", "1": 2.0, "X": 3.0, "2": 4.0}}
    assert exchange_match.lookup_exchange({"home": "Barcelona", "away": "Real Madrid"}, prices) is None
