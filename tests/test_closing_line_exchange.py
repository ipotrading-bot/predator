"""
tests/test_closing_line_exchange.py — capture de closing line depuis les prix
d'exchange déjà chargés par le scan (core/closing_line.capture_from_exchange).

CE QUE CES TESTS PROTÈGENT. Depuis l'obsolescence d'OddsAPI (2026-08-26) la
voie `capture_from_scan` est morte et seul l'oracle web-search restait — h2h
favori, sur le budget Groq des scans. Matchbook livre pourtant un prix sharp
réel à chaque scan, REPRICE compris.

Le piège central est testé en premier : `_enrich_from_exchange` n'écrase
`odds_pinnacle` que sur les matchs SANS prix sharp, et api-sports sert
Pinnacle sur 100 % de ses matchs foot — donc sur les matchs qui portent les
signaux, `odds_pinnacle` reste le prix d'ENTRÉE. Une implémentation qui lirait
le match enrichi stockerait ce prix d'entrée comme prix de clôture : CLV nul,
exécution verte, mensonge silencieux. Le test le prouve en donnant au match un
`odds_pinnacle` DIFFÉRENT du prix Matchbook — sans cet écart, une
implémentation fausse passerait aussi.
"""
from datetime import datetime, timedelta, timezone

import pytest

from core import closing_line as cl
from core.constants import (CLOSING_LINE_TIGHTEN_MIN, CLOSING_SRC_EXCHANGE,
                            CLOSING_SRC_ORACLE)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


class _SB:
    """Supabase minimal : rend les signaux demandés, encaisse les updates."""

    def __init__(self, signals):
        self._signals = signals
        self.updates = {}

    def table(self, _name):
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def in_(self, _col, ids):
        self._ids = set(ids)
        return self

    def limit(self, _n):
        return self

    def execute(self):
        return type("R", (), {"data": [s for s in self._signals
                                       if str(s["match_id"]) in self._ids]})()


@pytest.fixture(autouse=True)
def _update_capture(monkeypatch):
    """update_signal_fields est le seul point d'écriture : on l'intercepte."""
    ecrits = {}

    def _fake(sb, sig_id, fields, optional_cols=None):
        ecrits[sig_id] = fields
        return True

    monkeypatch.setattr(cl, "update_signal_fields", _fake)
    return ecrits


def _match(**over):
    m = {"id": "m1", "home": "Moss FK", "away": "Stabaek",
         "commence_time": (NOW + timedelta(minutes=90)).isoformat(),
         "sport": "soccer",
         # PRIX D'ENTRÉE, servi par api-sports : volontairement différent du
         # prix Matchbook ci-dessous. C'est ce qui rend le test discriminant.
         "odds_pinnacle": {"1": 1.80, "X": 3.60, "2": 4.50}}
    m.update(over)
    return m


def _prix(**over):
    row = {"home": "Moss FK", "away": "Stabaek",
           "1": 2.10, "X": 3.40, "2": 3.80, "_source": "matchbook"}
    row.update(over)
    return {"moss fk_stabaek": {**row}}


def _signal(**over):
    s = {"id": 1, "match_id": "m1", "match": "Moss FK vs Stabaek",
         "market_key": "h2h", "selection_name": "Moss FK", "sport": "soccer",
         "status": "active", "xbet_odd": 2.30}
    s.update(over)
    return s


# ── Le piège : le prix stocké doit être celui de l'EXCHANGE ──────────

def test_le_prix_capture_est_celui_de_lexchange_pas_le_pinnacle_dentree(_update_capture):
    """Sur un match foot servi par api-sports, `odds_pinnacle` porte le prix
    d'ENTRÉE et _enrich_from_exchange ne l'a pas touché. Lire le match plutôt
    que le dict de prix produirait un CLV structurellement nul."""
    n = cl.capture_from_exchange(_SB([_signal()]), [_match()], _prix(), NOW)
    assert n == 1
    ecrit = _update_capture[1]
    assert ecrit["closing_source"] == CLOSING_SRC_EXCHANGE
    # DNB Matchbook du côté domicile (2.10 / 3.80), pas le DNB d'entrée.
    from core.math_engine import calc_dnb
    attendu = calc_dnb(2.10, 3.80, 3.40)
    entree = calc_dnb(1.80, 4.50, 3.60)
    assert ecrit["closing_pinnacle_price"] == pytest.approx(attendu, abs=1e-4)
    assert ecrit["closing_pinnacle_price"] != pytest.approx(entree, abs=1e-2), \
        "le prix d'entrée a été stocké comme prix de clôture — CLV nul, panne silencieuse"
    assert ecrit["clv_pct_real"] == pytest.approx(round((2.30 / attendu - 1) * 100, 2))


def test_le_cote_du_signal_decide_du_prix(_update_capture):
    """Un signal sur l'extérieur doit être tarifé avec le prix extérieur."""
    cl.capture_from_exchange(_SB([_signal(selection_name="Stabaek")]),
                             [_match()], _prix(), NOW)
    from core.math_engine import calc_dnb
    assert _update_capture[1]["closing_pinnacle_price"] == \
        pytest.approx(calc_dnb(3.80, 2.10, 3.40), abs=1e-4)


def test_cote_non_resolu_nest_jamais_devine(_update_capture):
    """Une sélection qui ne désigne ni l'une ni l'autre équipe : on refuse
    plutôt que d'écrire un CLV du mauvais côté."""
    n = cl.capture_from_exchange(_SB([_signal(selection_name="Rosenborg")]),
                                 [_match()], _prix(), NOW)
    assert n == 0 and not _update_capture


# ── Les refus ────────────────────────────────────────────────────────

def test_football_sans_prix_de_nul_est_refuse(_update_capture):
    """calc_dnb rend 0.0 sans nul, et un repli sur le moneyline comparerait un
    prix d'entrée DNB à une clôture ML — faux, et invisible."""
    n = cl.capture_from_exchange(_SB([_signal()]), [_match()], _prix(X=0.0), NOW)
    assert n == 0 and not _update_capture


def test_un_sport_sans_nul_utilise_le_moneyline_brut(_update_capture):
    """Basket : pas de DNB, le prix de l'exchange EST la référence."""
    m = _match(sport="basketball", home="Lakers", away="Celtics")
    prix = {"lakers_celtics": {"home": "Lakers", "away": "Celtics",
                               "1": 1.90, "X": 0.0, "2": 2.05, "_source": "matchbook"}}
    cl.capture_from_exchange(_SB([_signal(sport="basketball", selection_name="Lakers",
                                          match="Lakers vs Celtics")]), [m], prix, NOW)
    assert _update_capture[1]["closing_pinnacle_price"] == pytest.approx(1.90)


def test_match_hors_fenetre_ignore(_update_capture):
    """Au-delà de CLOSING_LINE_WINDOW_MIN (240) ce n'est pas une clôture."""
    loin = _match(commence_time=(NOW + timedelta(hours=9)).isoformat())
    assert cl.capture_from_exchange(_SB([_signal()]), [loin], _prix(), NOW) == 0
    assert not _update_capture


def test_match_deja_commence_ignore(_update_capture):
    """Après le coup d'envoi l'exchange cote du live, pas une clôture."""
    passe = _match(commence_time=(NOW - timedelta(minutes=5)).isoformat())
    assert cl.capture_from_exchange(_SB([_signal()]), [passe], _prix(), NOW) == 0


def test_totals_et_spreads_ignores(_update_capture):
    """Le payload d'exchange ne porte pas la LIGNE que nous avons pariée :
    grader ces marchés comparerait deux paris différents."""
    sigs = [_signal(id=2, market_key="totals", selection_name="Over 2.5"),
            _signal(id=3, market_key="spreads", selection_name="Moss FK -1.5")]
    assert cl.capture_from_exchange(_SB(sigs), [_match()], _prix(), NOW) == 0
    assert not _update_capture


def test_match_absent_de_lexchange_ignore(_update_capture):
    assert cl.capture_from_exchange(_SB([_signal()]), [_match()],
                                    {"autre_match": {"home": "A", "away": "B",
                                                     "1": 2.0, "X": 3.0, "2": 3.0}}, NOW) == 0


def test_entrees_vides_ne_lancent_rien(_update_capture):
    assert cl.capture_from_exchange(None, [_match()], _prix(), NOW) == 0
    assert cl.capture_from_exchange(_SB([]), [], _prix(), NOW) == 0
    assert cl.capture_from_exchange(_SB([]), [_match()], {}, NOW) == 0


def test_appariement_flou_des_noms(_update_capture):
    """« CSD Macara » côté soft, « Deportivo Macara » côté exchange : la clé
    exacte n'apparie rien, c'est lookup_exchange qui tranche. Mesuré le
    2026-08-20 : sur 13 matchs, la clé exacte en appariait 0, le flou 8.
    """
    m = _match(home="CSD Macara", away="Delfin SC")
    prix = {"deportivo macara_delfin sc": {"home": "Deportivo Macara",
                                           "away": "Delfin SC",
                                           "1": 2.10, "X": 3.40, "2": 3.80,
                                           "_source": "matchbook"}}
    sig = _signal(selection_name="CSD Macara", match="CSD Macara vs Delfin SC")
    assert cl.capture_from_exchange(_SB([sig]), [m], prix, NOW) == 1


def test_un_nom_a_suffixe_ne_sapparie_pas_et_cest_un_refus_pas_une_erreur(_update_capture):
    """LIMITE CONNUE, mesurée le 2026-08-26 et documentée ici plutôt que
    découverte en production : `_normalize_team` ne translittère PAS la
    ligature « æ ». « Stabaek » et « Stabæk » ne s'apparient que par le RATIO
    de similarité (0,857 ≥ 0,60), pas par une normalisation — et dès qu'un
    côté porte un suffixe de club (« Stabæk Fotball ») le ratio tombe à 0,476
    et l'appariement échoue.

    Le comportement attendu ici est le REFUS silencieux, pas une exception ni
    un prix posé au hasard : mieux vaut aucun CLV qu'un CLV faux. Ce test
    fige ce contrat ; élargir `_normalize_team` toucherait l'appariement de
    tout le pipeline (edge compris) et ne se décide pas dans un commit sur la
    closing line."""
    prix = {"moss_stabaek fotball": {"home": "Moss", "away": "Stabæk Fotball",
                                     "1": 2.10, "X": 3.40, "2": 3.80,
                                     "_source": "matchbook"}}
    assert cl.capture_from_exchange(_SB([_signal()]), [_match()], prix, NOW) == 0
    assert not _update_capture


# ── Le prix d'exchange n'est pas re-pricé par l'oracle ───────────────

def test_needs_refresh_protege_un_prix_exchange_recent():
    """Un prix exact doit tenir CLOSING_LINE_TIGHTEN_MIN (90), pas
    CLOSING_LINE_REFRESH_MIN (20) : l'oracle web-search ne doit pas dépenser du
    budget pour écraser un vrai prix par une estimation du favori."""
    from core.audit_engine import _needs_refresh
    base = {"closing_pinnacle_price": 1.95,
            "match_time": (NOW + timedelta(minutes=30)).isoformat()}
    recent = (NOW - timedelta(minutes=CLOSING_LINE_TIGHTEN_MIN - 10)).isoformat()

    assert _needs_refresh({**base, "closing_captured_at": recent,
                           "closing_source": CLOSING_SRC_EXCHANGE}, NOW) is False
    # ...alors que l'estimation de l'oracle, elle, se rafraîchit dès 20 min.
    assert _needs_refresh({**base, "closing_captured_at": recent,
                           "closing_source": CLOSING_SRC_ORACLE}, NOW) is True


def test_needs_refresh_laisse_repricer_un_prix_exchange_perime():
    from core.audit_engine import _needs_refresh
    vieux = (NOW - timedelta(minutes=CLOSING_LINE_TIGHTEN_MIN + 10)).isoformat()
    assert _needs_refresh({"closing_pinnacle_price": 1.95,
                           "closing_captured_at": vieux,
                           "closing_source": CLOSING_SRC_EXCHANGE,
                           "match_time": (NOW + timedelta(minutes=30)).isoformat()},
                          NOW) is True
