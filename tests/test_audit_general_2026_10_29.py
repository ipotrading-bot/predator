"""
tests/test_audit_general_2026_10_29.py — défauts trouvés par l'audit général.

INCIDENTS.md, « Audit général du 2026-10-29 ». Un test par défaut corrigé :

1. `flip_exchange_prices` levait KeyError sur une ligne d'exchange sans
   moneyline (scans standard PAYÉS tombés les 09/10 et 13/10) et perdait
   `commence_time` (clôture tennis et contrôle « même match » neutralisés).
2. Une exception dans la récolte d'UN sport faisait tomber tout le scan.
3. Un nul en moneyline à deux issues était réglé LOSS (signal 10963,
   Commanders–Giants 27-27, recommandé) ; un total sans côté lisible LOSS.
(Le coup d'envoi dans la clé de jumeau du ledger : tests/test_ledger_jumeaux.py.)
"""
import inspect

import pytest

import core.harvester as harvester
import run_engine as eng
from core.exchange_match import flip_exchange_prices, lookup_exchange
from core.settlement import determine_outcome


class TestRetournementDExchange:
    def test_une_ligne_sans_moneyline_ne_leve_plus(self):
        rangee = {"home": "Sinner", "away": "Alcaraz", "_source": "smarkets",
                  "totals": {"point": 22.5, "over": 1.9, "under": 1.9}}
        out = flip_exchange_prices(rangee)
        assert out["1"] == 0.0 and out["2"] == 0.0     # refusé en aval (> 1.01 exigé)
        assert out["totals"] == rangee["totals"]

    def test_le_coup_d_envoi_et_les_noms_suivent(self):
        rangee = {"match": "Alcaraz vs Sinner", "home": "Alcaraz", "away": "Sinner",
                  "1": 1.80, "X": 0.0, "2": 2.05, "_source": "matchbook",
                  "commence_time": "2026-10-30T14:10:00Z"}
        out = flip_exchange_prices(rangee)
        assert out["commence_time"] == "2026-10-30T14:10:00Z"
        assert (out["home"], out["away"], out["1"], out["2"]) == ("Sinner", "Alcaraz", 2.05, 1.80)

    def test_bout_en_bout_par_lookup(self):
        prix = {"alcaraz_sinner": {"home": "Alcaraz", "away": "Sinner", "1": 1.8, "2": 2.05,
                                   "commence_time": "2026-10-30T14:10:00Z"}}
        hit = lookup_exchange({"home": "Sinner", "away": "Alcaraz"}, prix)
        assert hit["1"] == 2.05 and hit["commence_time"] == "2026-10-30T14:10:00Z"


class TestUnSportNeFaitPlusTomberLeScan:
    def test_le_sport_fautif_est_saute_et_consigne(self, monkeypatch):
        def recolte(sport_id):
            if sport_id == min(harvester._ODDS_API_IO_BY_ID):
                raise KeyError("2")
            return [{"id": f"m{sport_id}"}]
        monkeypatch.setattr(harvester, "_fetch_multi_book", recolte)
        matchs = harvester.fetch_matches()
        assert matchs, "les autres sports doivent aller au bout"
        echecs = harvester.echecs_recolte()
        assert len(echecs) == 1 and "KeyError" in echecs[0]

    def test_une_recolte_saine_remet_les_echecs_a_zero(self, monkeypatch):
        harvester._ECHECS_RECOLTE.append("vieux")
        monkeypatch.setattr(harvester, "_fetch_multi_book", lambda _s: [])
        harvester.fetch_matches()
        assert harvester.echecs_recolte() == []

    def test_le_run_sort_quand_meme_en_echec(self, monkeypatch):
        monkeypatch.setattr(eng, "echecs_recolte", lambda: ["nba: KeyError: '2'"])
        monkeypatch.setattr(eng, "REPRICE", False)
        with pytest.raises(RuntimeError, match="récolte partielle"):
            eng._echouer_si_recolte_partielle()

    def test_toutes_les_sorties_du_scan_verifient(self):
        """Un sport en panne peut laisser zéro match : les sorties anticipées
        vérifient aussi, pas seulement la fin."""
        assert inspect.getsource(eng.run).count("_echouer_si_recolte_partielle()") >= 4


class TestReglement:
    def test_un_nul_en_moneyline_a_deux_issues_rend_la_mise(self):
        # Signal 10963 : Commanders–Giants 27-27 (NFL), réglé LOSS à tort.
        assert determine_outcome("americanfootball", "h2h", "Washington Commanders",
                                 "Washington Commanders", "New York Giants", 27, 27) == "PUSH"

    def test_le_football_garde_son_nul_dnb(self):
        assert determine_outcome("soccer", "h2h", "Lyon", "Lyon", "Nice", 1, 1) == "PUSH"

    def test_le_cote_d_un_total_vient_du_marche(self):
        assert determine_outcome("soccer", "totals_over", "2.5", "Lyon", "Nice", 2, 1) == "WIN"
        assert determine_outcome("soccer", "totals_under", "Under 2.5", "Lyon", "Nice", 2, 1) == "LOSS"

    def test_sans_cote_lisible_rien_n_est_invente(self):
        assert determine_outcome("soccer", "totals", "Total 2.5", "Lyon", "Nice", 2, 1) == "UNKNOWN"


class TestRetentionMeta:
    """`meta` n'était jamais purgée (quota_*, mesure_books_* à 20-80 Ko/jour)."""

    class _Q:
        def __init__(self, log, leve=False):
            self.log, self.leve, self.f = log, leve, {}
        def delete(self): return self
        def like(self, c, v): self.f["like"] = v; return self
        def lt(self, c, v): self.f["lt"] = v; return self
        def execute(self):
            if self.leve:
                raise RuntimeError("réseau")
            self.log.append(dict(self.f))
            return type("R", (), {"data": [{"key": "x"}]})()

    def _sb(self, log, leve=False):
        test = self
        class _SB:
            def table(self, nom):
                assert nom == "meta", "seule meta est purgée — jamais le ledger"
                return test._Q(log, leve)
        return _SB()

    def test_seules_les_familles_datees_partent_apres_60_jours(self):
        from datetime import datetime, timezone
        from core import meta_retention as mr
        log = []
        n = mr.purger(self._sb(log), now=datetime(2026, 10, 29, tzinfo=timezone.utc))
        assert [x["like"] for x in log] == [p + "%" for p in mr.PREFIXES_DATES]
        assert all(x["lt"].startswith("2026-08-30") for x in log)
        assert n == len(mr.PREFIXES_DATES)
        assert not any(p.startswith(("threshold", "edge_ceiling", "sport_verdict", "learning"))
                       for p in mr.PREFIXES_DATES)

    def test_une_panne_ne_fait_pas_tomber_l_audit(self):
        from core import meta_retention as mr
        assert mr.purger(self._sb([], leve=True)) == 0
        assert mr.purger(None) == 0

    def test_l_audit_purge_apres_l_apprentissage(self):
        import core.audit_engine as ae
        src = inspect.getsource(ae._apprendre_et_entretenir)
        assert src.index("_learn(sb)") < src.index("_purger_meta(sb)")

    def test_un_audit_vide_apprend_aussi(self):
        """13:40 le 2026-10-29 : 0 pari en attente → sortie AVANT `_learn` ;
        les seuils corrigés à midi n'étaient pas appliqués."""
        import core.audit_engine as ae
        src = inspect.getsource(ae.run)
        vide = src[src.index("if not pending:"):src.index("return", src.index("if not pending:"))]
        assert "_apprendre_et_entretenir(sb)" in vide
        assert src.count("_apprendre_et_entretenir(sb)") == 2
