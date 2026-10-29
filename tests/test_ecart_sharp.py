"""
tests/test_ecart_sharp.py — l'accord des deux avis sharp est gardé (2026-10-29).

`run_engine._enrich_from_exchange` mesurait l'écart Pinnacle–exchange en
points de probabilité, refusait le match au-delà d'EXCHANGE_DIVERGENCE_PTS, et
JETAIT la mesure en deçà. Le ledger prévoyait `sharp_divergence_std` pour ce
rôle : 0 valeur sur 1 357 lignes, écrite en dur à None par core/db.py et
backfill_ledger.py. `sharp_divergence_pts` (sql/migrate_v10_22) la remplace ;
v10_23 retire les quatre colonnes jamais écrites.
"""
import inspect
import logging
from pathlib import Path

import core.db as core_db
import run_engine as eng
from test_contre_expertise_exchange import _match, _mb

log = logging.getLogger("test")
RACINE = Path(__file__).resolve().parent.parent


class TestLaMesureEstGardee:
    def test_un_accord_pose_l_ecart_sur_le_match(self):
        pin = {"1": 2.05, "X": 3.50, "2": 3.55}
        m = _match(pin=dict(pin))
        eng._enrich_from_exchange([m], _mb(2.10, 3.60, 3.40), log)
        attendu = eng._sharp_divergence_pts(pin, {"1": 2.10, "X": 3.60, "2": 3.40})
        assert m["_sharp_divergence_pts"] == round(attendu, 3)

    def test_une_seule_source_sharp_ne_pose_rien(self):
        """Bouche-trou : l'exchange EST le prix sharp, il n'y a rien à
        comparer — NULL, jamais 0 (un 0 dirait « accord parfait »)."""
        m = _match()
        eng._enrich_from_exchange([m], _mb(2.10, 3.60, 3.40), log)
        assert "_sharp_divergence_pts" not in m

    def test_le_signal_la_porte_sur_les_trois_marches(self):
        assert '"sharp_divergence_pts": sharp_divergence_pts' in inspect.getsource(eng._emit)
        src = inspect.getsource(eng)
        assert src.count('sharp_divergence_pts=m.get("_sharp_divergence_pts")') == 3

    def test_colonne_optionnelle_tant_que_la_migration_manque(self):
        assert "sharp_divergence_pts" in eng._OPTIONAL_COLS


class TestLedger:
    def test_l_ecart_est_recopie_et_la_colonne_morte_n_est_plus_ecrite(self):
        cap = {}

        class _Q:
            def insert(self, payload): cap.update(payload); return self
            def execute(self): return type("R", (), {"data": []})()
            def select(self, *a): return self
            def eq(self, *a): return self
            def gte(self, *a): return self
            def in_(self, *a): return self
            def limit(self, *a): return self
            def order(self, *a, **k): return self

        class _FSB:
            def table(self, t): return _Q()

        sig = {"id": 7, "match": "A vs B", "sport": "soccer", "market_key": "h2h",
               "xbet_odd": 1.9, "edge_pct": 3.0, "sharp_divergence_pts": 0.42}
        core_db.log_to_ledger(_FSB(), sig, clv=None, outcome="WIN")
        assert cap["sharp_divergence_pts"] == 0.42
        assert "sharp_divergence_std" not in cap

    def test_plus_aucun_ecrivain_des_colonnes_mortes(self):
        """v10_23 les retire : un écrivain restant ferait échouer chaque
        règlement après la migration."""
        mortes = ("sharp_divergence_std", "ai_confidence_score",
                  "news_sentiment_score", "bookmaker_adjustment_speed_seconds")
        for f in [*RACINE.glob("*.py"), *RACINE.glob("core/*.py"),
                  *RACINE.glob("scripts/*.py"), *RACINE.glob("api/*.py")]:
            texte = f.read_text(encoding="utf-8")
            for col in mortes:
                assert f'"{col}"' not in texte, f"{f.name} écrit encore {col}"

    def test_la_migration_de_retrait_se_garde_elle_meme(self):
        sql = (RACINE / "sql" / "migrate_v10_23_colonnes_mortes.sql").read_text(encoding="utf-8")
        assert "RAISE EXCEPTION" in sql and sql.index("RAISE EXCEPTION") < sql.index("DROP COLUMN")
        assert "DELETE" not in sql.upper().replace("DROP COLUMN", "")
