"""
tests/test_mesure_books.py — core/mesure_books.py et son branchement.

Le 1X2 foot d'août (~2 recommandés/jour) sortait d'un line shopping titan007
sur une douzaine de books ; depuis « 1xbet seul » (2026-09-07), le DNB de
1xbet ne bat presque jamais Pinnacle. Avant d'ouvrir un compte ailleurs,
l'opérateur veut savoir QUEL book rendrait des paris exécutables. Ces tests
verrouillent :
- la mesure ne lit que des noms EXACTS de books et garde le plafond
  anti-book-figé de titan007 — sans lui, le meilleur prix parmi des
  dizaines de books est un book en retard, et la mesure mentirait ;
- l'EV est calculée comme dans `_process_h2h` (DNB du favori du book contre
  le 1X2 sharp dévigorisé) ;
- la porte relue est celle de `_emit` (règle AH0, plancher, suspect, prob,
  pire devig) ;
- la mesure n'ÉMET jamais : elle n'écrit que `meta.mesure_books_*`, hors
  zone jouable et hors périmètre elle ne mesure rien.
"""
from datetime import datetime, timedelta, timezone

import pytest

import core.mesure_books as mb
import core.titan007 as t7
import run_engine as eng
from core.math_engine import calc_dnb, devig_bounds

_SHARP = {"1": 1.80, "X": 3.80, "2": 4.60}
_MED = {"1": 1.85, "X": 3.70, "2": 4.40}


# ── blocs_candidats ───────────────────────────────────────────────────


def test_seuls_les_noms_exacts_de_la_liste_sont_mesures():
    books = {"Melbet": {"1": 1.9, "X": 3.6, "2": 4.3},
             "Melbet Kenya": {"1": 1.9, "X": 3.6, "2": 4.3},     # pas le même compte
             "BetWhale": {"1": 1.9, "X": 3.6, "2": 4.3}}         # hors liste
    assert set(mb.blocs_candidats(books, _MED, 1.10)) == {"melbet"}


def test_un_cote_fige_au_dessus_de_la_mediane_est_mis_a_zero_donc_le_bloc_tombe():
    # 1.85 × 1,10 = 2,035 : un 2,20 est un book figé, pas un prix.
    books = {"Betway": {"1": 2.20, "X": 3.6, "2": 4.3}}
    assert mb.blocs_candidats(books, _MED, 1.10) == {}


def test_sans_cote_de_nul_pas_de_dnb_donc_pas_de_bloc():
    books = {"Betway": {"1": 1.90, "X": 0, "2": 4.3}}
    assert mb.blocs_candidats(books, _MED, 1.10) == {}


def test_sans_mediane_aucun_prix_nest_credible():
    books = {"Betway": {"1": 1.90, "X": 3.6, "2": 4.3}}
    assert mb.blocs_candidats(books, {}, 1.10) == {}


# ── ev_par_book ───────────────────────────────────────────────────────


def test_l_ev_est_celle_de_process_h2h_favori_a_domicile():
    bloc = {"1": 1.95, "X": 3.6, "2": 4.3}
    (ev, prix, prob, ev_cons), = mb.ev_par_book({"betway": bloc}, _SHARP, "A", "B").values()
    from core.math_engine import executable_price
    attendu_prix = executable_price(bloc, "soccer", "1")
    p, pc = devig_bounds(calc_dnb(1.80, 4.60, 3.80), calc_dnb(4.60, 1.80, 3.80))
    assert prix == pytest.approx(attendu_prix, abs=1e-3)
    assert ev == pytest.approx((p * attendu_prix - 1) * 100, abs=0.01)
    assert prob == pytest.approx(p * 100, abs=0.1)
    assert ev_cons == pytest.approx((pc * attendu_prix - 1) * 100, abs=0.01)


def test_favori_a_l_exterieur_la_probabilite_suit_le_cote_du_book():
    sharp = {"1": 4.60, "X": 3.80, "2": 1.80}
    bloc = {"1": 4.30, "X": 3.6, "2": 1.95}
    (ev, _, prob, _), = mb.ev_par_book({"betway": bloc}, sharp, "A", "B").values()
    p, _ = devig_bounds(calc_dnb(1.80, 4.60, 3.80), calc_dnb(4.60, 1.80, 3.80))
    assert prob == pytest.approx(p * 100, abs=0.1)


def test_sans_sharp_complet_rien_n_est_mesure():
    assert mb.ev_par_book({"betway": {"1": 1.9, "X": 3.6, "2": 4.3}},
                          {"1": 1.8, "X": 0, "2": 4.6}, "A", "B") == {}


# ── accumuler ─────────────────────────────────────────────────────────


def test_le_meilleur_ev_du_jour_est_garde_par_book_sans_muter_le_blob():
    b1 = mb.accumuler({}, "t7_1", "A vs B", "ENG PR", "2026-10-24T14:00:00Z", True,
                      {"betway": [2.0, 1.6, 64.0, 0.5], "1xbet": [-1.0, 1.5, 64.0, -2.0]})
    b2 = mb.accumuler(b1, "t7_1", "A vs B", "ENG PR", "2026-10-24T14:00:00Z", True,
                      {"betway": [1.0, 1.58, 64.0, 0.1], "1xbet": [0.5, 1.55, 64.0, -0.5]})
    assert b1["matchs"]["t7_1"]["b"]["1xbet"][0] == -1.0
    assert b2["matchs"]["t7_1"]["b"] == {"betway": [2.0, 1.6, 64.0, 0.5],
                                         "1xbet": [0.5, 1.55, 64.0, -0.5]}


# ── passe ─────────────────────────────────────────────────────────────


def _porte(mesure, seuil_foot=4.0):
    return mb.passe(mesure, seuil_foot, 1.5, 1.5, 10.0, 0.50)


def test_porte_regle_ah0_au_dessus_de_1_50_le_plancher_reste_1_5():
    assert _porte([1.6, 1.70, 62.0, 0.2])
    assert not _porte([1.4, 1.70, 62.0, 0.2])


def test_porte_sous_1_50_le_seuil_appris_du_foot_s_applique():
    assert not _porte([3.0, 1.40, 73.0, 0.5])
    assert _porte([4.1, 1.40, 73.0, 0.5])


def test_porte_suspect_prob_et_pire_devig_refusent():
    assert not _porte([10.5, 1.80, 60.0, 5.0])     # SUSPECT_EDGE
    assert not _porte([2.0, 2.10, 48.0, 0.5])      # prob < 50 %
    assert not _porte([2.0, 1.80, 60.0, -0.1])     # négatif sous le pire devig


# ── classement ────────────────────────────────────────────────────────


def test_l_apport_ne_compte_que_les_matchs_qu_aucun_clone_1xbet_ne_sert():
    blob = {"matchs": {
        "m1": {"maj": True, "b": {"betway": [2.0, 1.7, 62, 0.3], "melbet": [2.0, 1.7, 62, 0.3]}},
        "m2": {"maj": False, "b": {"betway": [2.0, 1.7, 62, 0.3], "1xbet": [-1.0, 1.6, 62, -2]}},
    }}
    rows = {r["book"]: r for r in mb.classement([blob], _porte)}
    assert rows["betway"]["passent"] == 2 and rows["betway"]["apport"] == 1
    assert rows["betway"]["majeures"] == 1
    assert rows["melbet"]["apport"] == 0
    assert rows["1xbet"]["passent"] == 0 and rows["1xbet"]["ev_moy"] is None


# ── titan007 : la mesure voyage avec le match ─────────────────────────


@pytest.fixture
def _t7(monkeypatch):
    monkeypatch.setattr(t7.daily_quota, "spent", lambda bucket: 0)
    monkeypatch.setattr(t7.daily_quota, "add", lambda bucket, n: None)
    monkeypatch.setattr(t7.daily_quota, "_db", lambda: None)   # mémoire sans-cotes inerte
    monkeypatch.setattr(t7.time, "sleep", lambda s: None)
    ko = datetime.now(timezone.utc) + timedelta(hours=6)
    site = ko + timedelta(hours=t7.SITE_UTC_OFFSET_H)
    row = [""] * 69
    row[0], row[4], row[7], row[10] = "9", "ENG PR", "Arsenal", "Fulham"
    row[11], row[36], row[43] = site.strftime("%H:%M"), f"{site.month}-{site.day}", str(site.year)
    books = [("Pinnacle", 1.80, 3.80, 4.60), ("1xBet", 1.84, 3.70, 4.40),
             ("Bet 365", 1.83, 3.70, 4.40), ("Bwin", 1.85, 3.60, 4.30),
             ("Betway", 1.95, 3.60, 4.30), ("Melbet", 1.84, 3.70, 4.40)]

    def fake_get(url):
        if "bfdata_ut" in url:
            return f'A[0] = "{"^".join(row)}";'
        recs = []
        for name, o1, ox, o2 in books:
            parts = [""] * 21
            parts[2], parts[10], parts[11], parts[12] = name, str(o1), str(ox), str(o2)
            recs.append('"' + "|".join(parts) + '"')
        return "var game = Array(" + ",".join(recs) + ");\n"
    monkeypatch.setattr(t7, "_get", fake_get)


def test_titan007_attache_les_candidats_et_leur_sharp(_t7):
    (m,) = t7.fetch_matches(hours_ahead=24)
    mesure = m["_mesure_books"]
    assert {"1xbet", "betway", "melbet", "bet 365", "bwin"} <= set(mesure["blocs"])
    assert mesure["sharp"]["1"] == 1.80
    # Le prix d'exécution, lui, reste celui de 1xbet — la mesure ne le touche pas.
    assert m["odds_1xbet"]["1"] == 1.84


def test_la_fusion_de_sources_garde_la_mesure(monkeypatch):
    # Un match vu d'abord par odds-api.io : sans la recopie, la mesure des
    # grandes ligues (servies par les deux sources) disparaîtrait.
    from core import harvester as hv
    base = {"match": "Arsenal vs Fulham", "home": "Arsenal", "away": "Fulham",
            "odds_1xbet": {"1": 1.84, "X": 3.7, "2": 4.4}}
    monkeypatch.setattr(hv, "_fetch_from_odds_api_io", lambda sid: [dict(base)])
    monkeypatch.setattr(hv, "_t7_fetch",
                        lambda: [dict(base, _mesure_books={"blocs": {}, "sharp": _SHARP})])
    monkeypatch.setattr(hv, "_measure_consensus", lambda sid, merged: None)
    (m,) = hv._fetch_multi_book(hv._T7_SPORT_ID)
    assert m["_soft_source"] == "odds_api_io"
    assert m["_mesure_books"]["sharp"] == _SHARP


# ── le branchement moteur : mesure seule ──────────────────────────────


class _Q:
    def __init__(self, sb, table):
        self.sb, self.table_name, self._key = sb, table, None
        sb.tables.add(table)

    def select(self, *_a, **_k): return self
    def eq(self, _col, val): self._key = val; return self
    def maybe_single(self): return self
    def upsert(self, row, **_k): self.sb.store[row["key"]] = row; return self

    def execute(self):
        class R: pass
        r = R(); r.data = self.sb.store.get(self._key); return r


class _SB:
    def __init__(self): self.store, self.tables = {}, set()
    def table(self, name): return _Q(self, name)


class _Log:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): raise AssertionError(a[0] % a[1:])


def _match(league="ENG PR", heures=6.0, sport="soccer"):
    now = datetime.now(timezone.utc)
    ko = (now + timedelta(hours=heures)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": f"t7_{league}_{heures}", "match": "Arsenal vs Fulham", "home": "Arsenal",
            "away": "Fulham", "league": league, "sport": sport, "commence_time": ko,
            "_mesure_books": {"blocs": {"betway": {"1": 1.95, "X": 3.6, "2": 4.3}},
                              "sharp": _SHARP}}


def test_le_moteur_ne_fait_qu_ecrire_meta_et_jamais_signals():
    sb, now = _SB(), datetime.now(timezone.utc)
    n = eng._mesurer_books_candidats(sb, [_match()], now, _Log())
    assert n == 1
    assert sb.tables == {"meta"}
    import json
    blob = json.loads(sb.store[mb.cle_du_jour(now)]["value"])
    (entree,) = blob["matchs"].values()
    assert entree["maj"] is True and "betway" in entree["b"]


def test_hors_zone_jouable_jeunes_et_autre_sport_ne_sont_pas_mesures():
    sb, now = _SB(), datetime.now(timezone.utc)
    hors = [_match(heures=1.0), _match(heures=30.0),
            _match(league="UEFA Youth League"), _match(sport="tennis")]
    assert eng._mesurer_books_candidats(sb, hors, now, _Log()) == 0
    assert mb.cle_du_jour(now) not in sb.store


def test_sans_base_rien_n_est_mesure():
    assert eng._mesurer_books_candidats(None, [_match()], datetime.now(timezone.utc), _Log()) == 0
