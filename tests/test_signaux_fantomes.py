"""
tests/test_signaux_fantomes.py — un fantôme ne s'affiche NULLE PART (2026-09-03).

Le mode fantôme (SHADOW_GOLDEN_HOUR) taisait Telegram mais persistait le
signal en `status = 'active'` sans marqueur : le dashboard et /api/signals le
montraient comme un pari à poser. Mesuré : 17 des 27 actifs de septembre, 19
des 22 réglés. Correctif en quatre pièces, chacune gardée ici :
  1. run_engine._shadow_partition MARQUE (`is_shadow`, `shadow_reason`) et
     tourne AVANT la persistance ; la règle T-2h vaut par SIGNAL, pas
     seulement par mode de run ;
  2. run_engine._save ne réécrit jamais le drapeau au rafraîchissement ;
  3. core.db.log_to_ledger recopie le drapeau et mesure la distance au coup
     d'envoi depuis created_at ;
  4. api/index.py filtre `is_shadow = false` partout où il liste des actifs.
"""
import inspect
import pathlib

import run_engine as eng
from core import db as core_db
from core import perf_view
from core.learning_layer import _PLAYABLE_MIN_MINUTES

RACINE = pathlib.Path(__file__).resolve().parent.parent
SCAN = "2026-09-03T14:00:00+00:00"


def _sig(sport="soccer", minutes=600, **extra):
    from datetime import datetime, timedelta
    mt = (datetime.fromisoformat(SCAN) + timedelta(minutes=minutes)).isoformat() if minutes is not None else ""
    d = {"match": "A vs B", "sport": sport, "edge_pct": 5.0, "scanned_at": SCAN, "match_time": mt,
         "match_id": "m1", "market_key": "h2h", "status": "active"}
    d.update(extra)
    return d


class TestRaisonDuFantome:
    def test_zone_recommandee_pas_de_raison(self):
        assert eng._shadow_reason(_sig(minutes=600), golden_hour=False) is None

    def test_t_moins_2h_est_fantome_quel_que_soit_le_mode(self):
        """La règle qui manquait : un scan STANDARD à T-66 min émettait un
        signal recommandé, envoyé, affiché — alors que la mesure du fantôme
        portait sur la tranche horaire, pas sur le mode du run."""
        assert eng._shadow_reason(_sig(minutes=66), golden_hour=False) == "t_minus_2h"
        assert eng._shadow_reason(_sig(minutes=_PLAYABLE_MIN_MINUTES - 1), golden_hour=False) == "t_minus_2h"
        assert eng._shadow_reason(_sig(minutes=_PLAYABLE_MIN_MINUTES), golden_hour=False) is None

    def test_la_borne_est_importee_pas_recopiee(self):
        src = inspect.getsource(eng._shadow_reason)
        assert "_PLAYABLE_MIN_MINUTES" in src and "120" not in src

    def test_mode_golden_couvre_tout_le_run(self):
        assert eng._shadow_reason(_sig(minutes=600), golden_hour=True) == "golden_hour"

    def test_sport_fantome_prime(self, monkeypatch):
        monkeypatch.setattr(eng, "SHADOW_SPORTS", {"baseball"})
        assert eng._shadow_reason(_sig("baseball", minutes=600), golden_hour=False) == "shadow_sport"

    def test_linconnu_nest_pas_classe_fantome(self):
        assert eng._shadow_reason(_sig(minutes=None), golden_hour=False) is None
        assert eng._shadow_reason(_sig(match_time="n'importe quoi"), golden_hour=False) is None
        assert eng._minutes_avant_coup_denvoi({"match_time": "x", "scanned_at": SCAN}) is None


class TestPartitionMarque:
    def test_chaque_signal_porte_son_drapeau(self):
        sigs = [_sig(minutes=600), _sig(minutes=30)]
        kept, shadowed = eng._shadow_partition(sigs, golden_hour=False)
        assert [s["is_shadow"] for s in sigs] == [False, True]
        assert kept[0]["shadow_reason"] is None and shadowed[0]["shadow_reason"] == "t_minus_2h"

    def test_la_partition_precede_la_persistance(self):
        """Le drapeau doit partir en base AVEC la ligne : dans run(), la
        partition vient avant la boucle _save, et Telegram part des
        recommandés."""
        src = inspect.getsource(eng.run)
        assert src.index("_shadow_partition(signals, GOLDEN_HOUR)") < src.index("if _save(sb, s):")
        assert "emit_signals = recommandes" in src
        assert "_heartbeat(sb, now, len(matches), len(recommandes))" in src

    def test_les_colonnes_sont_optionnelles_pour_le_repli_de_schema(self):
        assert {"is_shadow", "shadow_reason"} <= eng._OPTIONAL_COLS


class TestRafraichissementFige:
    def test_un_rescan_ne_transforme_pas_une_recommandation_en_fantome(self):
        """Émise à T-6h (recommandée, envoyée), revue à T-1h par le tick
        golden : la ligne active garde is_shadow = false. Sinon le dashboard
        cacherait un pari que l'opérateur a peut-être déjà posé."""
        captured = {}

        class _Q:
            def __init__(self, t): self.t = t
            def insert(self, payload): return self
            def update(self, champs):
                captured.update(champs); return self
            def eq(self, *a): return self
            def select(self, *a): return self
            def order(self, *a, **k): return self
            def limit(self, *a): return self
            def execute(self):
                if not captured:
                    raise RuntimeError('duplicate key value violates unique constraint "signals_active_uniq"')
                return type("R", (), {"data": [{"id": 1}]})()

        class _SB:
            def table(self, t): return _Q(t)

        payload = _sig(minutes=60)
        payload["is_shadow"], payload["shadow_reason"] = True, "t_minus_2h"
        assert eng._save(_SB(), payload) is True
        assert "is_shadow" not in captured and "shadow_reason" not in captured
        assert captured["scanned_at"] == SCAN     # les champs de scan, eux, se rafraîchissent


class TestLedger:
    def _capture(self):
        cap = {}

        class _Q:
            def insert(self, payload): cap.update(payload); return self
            def execute(self): return type("R", (), {"data": []})()
            def select(self, *a): return self
            def eq(self, *a): return self
            def in_(self, *a): return self
            def neq(self, *a): return self
            def limit(self, *a): return self
            def order(self, *a, **k): return self

        class _SB:
            def table(self, t): return _Q()
        return _SB(), cap

    def test_le_drapeau_est_recopie(self):
        sb, cap = self._capture()
        core_db.log_to_ledger(sb, {**_sig(minutes=30), "id": 7, "is_shadow": True}, clv=0.0, outcome="LOSS")
        assert cap["is_shadow"] is True
        sb, cap = self._capture()
        core_db.log_to_ledger(sb, {**_sig(minutes=600), "id": 8}, clv=0.0, outcome="WIN")
        assert cap["is_shadow"] is False

    def test_la_distance_se_mesure_depuis_created_at(self):
        """Signal créé à T-6h, rafraîchi à T-1h (scanned_at écrasé) : le
        ledger doit dire 360 minutes, pas 60."""
        sb, cap = self._capture()
        sig = _sig(minutes=60, created_at="2026-09-03T09:00:00+00:00", id=9)
        core_db.log_to_ledger(sb, sig, clv=0.0, outcome="WIN")
        assert cap["time_to_match_minutes"] == 360
        sb, cap = self._capture()
        core_db.log_to_ledger(sb, _sig(minutes=60, id=10), clv=0.0, outcome="WIN")   # sans created_at : repli
        assert cap["time_to_match_minutes"] == 60


class TestDashboard:
    def test_les_listes_dactifs_filtrent_le_fantome(self):
        import api.index as dash
        for fn in (dash.dashboard, dash.api_signals):
            assert '.eq("is_shadow", False)' in inspect.getsource(fn), fn.__name__
        src = inspect.getsource(dash)
        # trois lectures d'actifs (accueil, API, audit) → trois filtres
        assert src.count('.eq("is_shadow", False)') == 3

    def test_perf_view_prefere_le_drapeau(self):
        assert perf_view.is_phantom({"is_shadow": True, "time_to_match_minutes": 600}) is True
        assert perf_view.is_phantom({"is_shadow": False, "time_to_match_minutes": 30}) is False
        # sans drapeau : approximation par la zone horaire
        assert perf_view.is_phantom({"time_to_match_minutes": 30}) is True
        assert perf_view.is_phantom({"time_to_match_minutes": 600}) is False
        assert perf_view.is_phantom({}) is False


class TestMigration:
    def test_la_migration_marque_sans_supprimer(self):
        sql = (RACINE / "sql" / "migrate_v10_12_signals_shadow.sql").read_text(encoding="utf-8")
        code = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
        assert "DELETE" not in code.upper() and "DROP" not in code.upper()
        for table in ("signals", "ai_learning_ledger", "signals_archive", "ai_learning_ledger_archive"):
            assert f"ALTER TABLE {table}" in code, table
        assert "is_shadow" in code and "shadow_reason" in code
        # le backfill de signals part de created_at, pas de scanned_at (réécrit)
        assert "match_time::timestamptz - created_at" in code
        assert "time_to_match_minutes < 120" in code


class TestAutresLecteurs:
    def test_le_digest_telegram_ne_reliste_pas_les_fantomes(self):
        """Le rapport bi-horaire lit `signals` directement : sans ce filtre,
        la tranche que le moteur venait de taire repartait sur Telegram."""
        import run_rapport
        assert '.eq("is_shadow", False)' in inspect.getsource(run_rapport.main if hasattr(run_rapport, "main") else run_rapport)

    def test_lexposition_ignore_les_fantomes(self):
        from core import risk_manager
        assert '.eq("is_shadow", False)' in inspect.getsource(risk_manager.get_current_exposure)
