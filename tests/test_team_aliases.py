"""
tests/test_team_aliases.py — le dictionnaire se construit, se confirme et
s'invalide.

CE QUI EST VÉRIFIÉ
------------------
Le chemin complet du multilingue, sans base ni réseau :
  calendrier chinois + calendrier anglais
    → appariement SANS les noms (core/source_adapter)
      → alias appris (鹿岛鹿角 → Kashima Antlers)
        → confirmé par un second appariement indépendant
          → invalidé si un appariement le contredit.

La base est remplacée par un faux client Supabase en mémoire : le contrat
« dégrade proprement sans base » est vérifié à part.
"""
import pytest

from core import source_adapter as sa
from core import team_aliases as ta
from core.source_adapter import Fixture

PINNACLE = [9.19, 5.14, 1.36]
BETFAIR = [10.0, 5.40, 1.39]


class FakeTable:
    """Assez de Supabase pour ce module, et pas un octet de plus."""

    def __init__(self, store):
        self.store = store
        self._filters = {}
        self._payload = None
        self._mode = None

    def select(self, *_a, **_k):
        self._mode = "select"
        return self

    def insert(self, payload):
        self._mode, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._mode, self._payload = "update", payload
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def limit(self, _n):
        return self

    def maybe_single(self):
        return self

    def _matching(self):
        out = []
        for row in self.store:
            if all(str(row.get(k)) == str(v) for k, v in self._filters.items()):
                out.append(row)
        return out

    def execute(self):
        if self._mode == "insert":
            row = dict(self._payload)
            row["id"] = len(self.store) + 1
            row.setdefault("hits", 0)
            row.setdefault("contradictions", 0)
            self.store.append(row)
            return type("R", (), {"data": [row]})()
        if self._mode == "update":
            for row in self._matching():
                row.update(self._payload)
            return type("R", (), {"data": self._matching()})()
        return type("R", (), {"data": self._matching()})()


class FakeDB:
    def __init__(self):
        self.rows = []

    def table(self, _name):
        return FakeTable(self.rows)


@pytest.fixture
def base(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(ta, "_db", lambda: db)
    ta._CACHE.clear()
    yield db
    ta._CACHE.clear()


def _fx(source, mid, kickoff, league, home, away, odds, ids=()):
    return Fixture(source=source, match_id=mid, kickoff=kickoff, league=league,
                   home=home, away=away, odds=odds, team_ids=ids)


CN = [
    _fx("odds500", "1418972", "2026-08-22T09:00:00Z", "日职",
        "鹿岛鹿角", "福冈黄蜂", PINNACLE, ("1029", "808")),
    _fx("odds500", "1420317", "2026-08-22T19:30:00Z", "英超",
        "赫尔城", "曼联", BETFAIR, ("872", "1075")),
]
EN = [
    _fx("sevenm", "51001", "2026-08-22T09:00:00Z", "J1 League",
        "Kashima Antlers", "Avispa Fukuoka", PINNACLE),
    _fx("sevenm", "51002", "2026-08-22T19:30:00Z", "Premier League",
        "Hull City", "Manchester United", BETFAIR),
]


class TestApprentissageMultilingue:
    def test_un_nom_chinois_devient_un_nom_canonique(self, base):
        """LE cas du cahier des charges : nom chinois ↔ canonique, appris
        SANS aucun appel IA et sans comparer un seul libellé."""
        pairs = sa.pair_fixtures(CN, EN)
        assert len(pairs) == 2
        compte = ta.apply_pairing("odds500", pairs)
        assert compte["appris"] == 4 and compte["contredits"] == 0

        assert ta.canonical("odds500", "鹿岛鹿角", "1029", "日职") == "Kashima Antlers"
        assert ta.canonical("odds500", "曼联", "1075", "英超") == "Manchester United"

    def test_lidentifiant_numerique_retrouve_lalias_meme_si_le_libelle_change(self, base):
        ta.apply_pairing("odds500", sa.pair_fixtures(CN, EN))
        # 500.com réécrit « 曼联 » en « 曼彻斯特联 » : l'identifiant, lui, ne
        # bouge pas — c'est tout l'intérêt de le stocker.
        assert ta.canonical("odds500", "曼彻斯特联", "1075", "英超") == "Manchester United"

    def test_la_langue_est_detectee_et_stockee(self, base):
        ta.apply_pairing("odds500", sa.pair_fixtures(CN, EN))
        row = ta.lookup("odds500", "鹿岛鹿角", "1029", "日职")
        assert row["lang"] == "zh"
        assert row["resolved_by"] == "sevenm"


class TestConfianceEtInvalidation:
    def test_un_second_appariement_monte_la_confiance(self, base):
        ta.apply_pairing("odds500", sa.pair_fixtures(CN, EN))
        avant = ta.lookup("odds500", "鹿岛鹿角", "1029", "日职")["confidence"]
        ta._CACHE.clear()
        compte = ta.apply_pairing("odds500", sa.pair_fixtures(CN, EN))
        assert compte["confirmés"] == 4 and compte["appris"] == 0
        apres = ta.lookup("odds500", "鹿岛鹿角", "1029", "日职")["confidence"]
        assert apres > avant

    def test_une_contradiction_invalide_immediatement(self, base):
        """Asymétrie voulue : plusieurs confirmations pour monter, UNE seule
        contradiction pour tomber. Un alias faux produit un edge crédible et
        entièrement imaginaire ; un alias écarté à tort ne coûte qu'un match."""
        ta.apply_pairing("odds500", sa.pair_fixtures(CN, EN))
        ta._CACHE.clear()
        faux = [_fx("sevenm", "51003", "2026-08-22T09:00:00Z", "J1 League",
                    "Kawasaki Frontale", "Cerezo Osaka", PINNACLE)]
        compte = ta.apply_pairing("odds500", sa.pair_fixtures([CN[0]], faux))
        assert compte["contredits"] == 2
        assert ta.lookup("odds500", "鹿岛鹿角", "1029", "日职")["confidence"] == 0.0

    def test_un_alias_invalide_ne_porte_plus_aucun_signal(self, base):
        ta.apply_pairing("odds500", sa.pair_fixtures(CN, EN))
        ta._CACHE.clear()
        ta.invalidate("odds500", "鹿岛鹿角", "1029", "日职", reason="test")
        ta._CACHE.clear()
        assert ta.canonical("odds500", "鹿岛鹿角", "1029", "日职") is None

    def test_les_variantes_de_suffixe_ne_sont_pas_des_contradictions(self, base):
        """« Manchester United FC » et « Manchester Utd » désignent la même
        équipe. Sans cette normalisation, chaque source contredirait l'autre
        sur des suffixes et le dictionnaire s'auto-détruirait."""
        ta.apply_pairing("odds500", sa.pair_fixtures(CN, EN))
        ta._CACHE.clear()
        variante = [_fx("sevenm", "51004", "2026-08-22T19:30:00Z", "Premier League",
                        "Hull City AFC", "Manchester Utd", BETFAIR)]
        compte = ta.apply_pairing("odds500", sa.pair_fixtures([CN[1]], variante))
        assert compte["contredits"] == 0 and compte["confirmés"] == 2


class TestSeuilDeConfiance:
    def test_un_alias_IA_non_confirme_ne_passe_pas_le_seuil(self, base):
        # 0,4 au départ pour l'IA : il faut deux confirmations par appariement
        # indépendant avant qu'un signal puisse s'appuyer dessus.
        ta.remember("odds500", "某某队", "Some Team", "999", "英超", resolved_by="ai")
        ta._CACHE.clear()
        assert ta.canonical("odds500", "某某队", "999", "英超") is None
        ta.confirm("odds500", "某某队", "999", "英超")
        ta._CACHE.clear()
        ta.confirm("odds500", "某某队", "999", "英超")
        ta._CACHE.clear()
        assert ta.canonical("odds500", "某某队", "999", "英超") == "Some Team"

    def test_un_alias_7M_passe_des_le_premier_appariement(self, base):
        ta.remember("odds500", "鹿岛鹿角", "Kashima Antlers", "1029", "日职",
                    resolved_by="sevenm")
        ta._CACHE.clear()
        assert ta.canonical("odds500", "鹿岛鹿角", "1029", "日职") == "Kashima Antlers"


class TestDegradation:
    def test_sans_base_le_dictionnaire_ne_leve_jamais(self, monkeypatch):
        monkeypatch.setattr(ta, "_db", lambda: None)
        ta._CACHE.clear()
        assert ta.lookup("odds500", "鹿岛鹿角") is None
        assert ta.canonical("odds500", "鹿岛鹿角") is None
        assert ta.remember("odds500", "鹿岛鹿角", "Kashima Antlers") is not None
        ta.invalidate("odds500", "鹿岛鹿角")          # ne lève pas
        assert ta.stats()["base"] is False
        ta._CACHE.clear()

    def test_le_budget_IA_est_borne(self, base, monkeypatch):
        from core import daily_quota
        monkeypatch.setattr(daily_quota, "spent", lambda b: ta.AI_DAILY_BUDGET)
        appels = []
        monkeypatch.setattr("core.ai_search.ai_complete",
                            lambda *a, **k: appels.append(1))
        assert ta.resolve_with_ai("odds500", "未知队", "英超") is None
        assert appels == []            # aucun appel IA une fois le budget atteint

    def test_un_nom_deja_connu_ne_repasse_jamais_par_lIA(self, base, monkeypatch):
        ta.remember("odds500", "鹿岛鹿角", "Kashima Antlers", "1029", "日职",
                    resolved_by="sevenm")
        ta._CACHE.clear()
        appels = []
        monkeypatch.setattr("core.ai_search.ai_complete",
                            lambda *a, **k: appels.append(1))
        assert ta.resolve_with_ai("odds500", "鹿岛鹿角", "日职",
                                  team_id="1029") == "Kashima Antlers"
        assert appels == []            # c'est un dictionnaire, pas un traducteur
