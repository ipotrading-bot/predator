"""
tests/test_migration_cloture_oracle.py — règle 14 : un prix de clôture
demandé à l'ancien LLM (closing_source='oracle') n'est pas une observation.

La migration v10_15 avait retiré le CLV de ces lignes et gardé le prix ; la
dérive sharp de l'apprentissage (2026-09-27) lit le PRIX. v10_18 le retire à
son tour, après l'avoir archivé (règle 9). Gardiens : l'archive précède
l'effacement, rien n'est supprimé, la provenance reste, et aucun module de
production n'écrit plus la source `oracle`.
"""
import ast
import pathlib

RACINE = pathlib.Path(__file__).resolve().parent.parent
SQL = (RACINE / "sql" / "migrate_v10_18_cloture_oracle.sql").read_text(encoding="utf-8")
# Le SQL exécutable seul : les commentaires citent volontairement des requêtes.
CODE = "\n".join(l for l in SQL.splitlines() if not l.lstrip().startswith("--"))


class TestMigration:
    def test_archive_avant_effacement(self):
        assert CODE.index("closing_oracle_archive_v10_18") < CODE.index("UPDATE")
        assert "ON CONFLICT (key) DO NOTHING" in CODE

    def test_rien_nest_supprime(self):
        assert "DELETE" not in CODE.upper() and "DROP" not in CODE.upper()

    def test_seuls_le_prix_et_sa_date_sont_effaces(self):
        for update in CODE.split("UPDATE")[1:]:
            clause_set = update.split("WHERE")[0]
            assert "closing_pinnacle_price = NULL" in clause_set
            assert "closing_source" not in clause_set     # la provenance reste
            assert "outcome" not in clause_set

    def test_les_deux_tables(self):
        assert "UPDATE signals" in CODE and "UPDATE ai_learning_ledger" in CODE

    def test_idempotente(self):
        assert CODE.count("closing_pinnacle_price IS NOT NULL") >= 4


class TestPlusAucunEcrivainOracle:
    def test_la_source_oracle_nest_plus_ecrite(self):
        """CLOSING_SRC_ORACLE ne subsiste que comme constante (lecture des
        lignes historiques) et comme libellé du rapport hebdo."""
        autorises = {"core/constants.py", "scripts/weekly_report.py"}
        fautifs = []
        for dossier in ("core", "scripts", "api"):
            for f in (RACINE / dossier).rglob("*.py"):
                rel = f.relative_to(RACINE).as_posix()
                if rel in autorises:
                    continue
                arbre = ast.parse(f.read_text(encoding="utf-8"))
                for n in ast.walk(arbre):
                    if isinstance(n, ast.Name) and n.id == "CLOSING_SRC_ORACLE":
                        fautifs.append(rel)
                    elif isinstance(n, ast.Constant) and n.value == "oracle":
                        fautifs.append(rel)
        for f in RACINE.glob("run_*.py"):
            src = f.read_text(encoding="utf-8")
            if "CLOSING_SRC_ORACLE" in src:
                fautifs.append(f.name)
        # learning_layer LIT la constante pour l'écarter : seul usage admis.
        assert set(fautifs) <= {"core/learning_layer.py"}, fautifs
