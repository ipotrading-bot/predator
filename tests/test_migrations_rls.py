"""
tests/test_migrations_rls.py — toute table d'archive créée en SQL est refermée.

`ai_learning_ledger_archive` (v10_5) puis `signals_archive` (v10_5) ont
toutes deux vécu SANS RLS jusqu'à ce qu'un audit les trouve nues (refermées
par v10_10 et v10_11). Plutôt qu'une liste de tables à tenir à la main
(règle n°6), ce gardien DÉRIVE la liste des tables `_archive` des CREATE
TABLE du dossier sql/ et exige pour chacune un ENABLE ROW LEVEL SECURITY
quelque part dans le même dossier.
"""
import re
from pathlib import Path

_SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def _tout_le_sql() -> str:
    return "\n".join(p.read_text(encoding="utf-8")
                     for p in sorted(_SQL_DIR.glob("*.sql")))


def test_toute_table_archive_creee_a_sa_rls():
    sql = _tout_le_sql()
    creees = set(re.findall(
        r"CREATE TABLE (?:IF NOT EXISTS )?(\w+_archive)\b", sql, re.IGNORECASE))
    assert creees, "Aucune table _archive trouvée dans sql/ — le motif de ce gardien a changé ?"
    sans_rls = {t for t in creees
                if not re.search(
                    rf"ALTER TABLE (?:public\.)?{t}\s+ENABLE ROW LEVEL SECURITY",
                    sql, re.IGNORECASE)}
    assert not sans_rls, (
        f"Table(s) d'archive sans RLS dans sql/ : {sorted(sans_rls)}. "
        "Une archive de résultats est la seule trace empirique du passé — "
        "la laisser ouverte à anon a déjà été trouvé deux fois à l'audit "
        "(v10_10, v10_11) : poser ENABLE ROW LEVEL SECURITY + REVOKE dans "
        "une nouvelle migration.")


def test_toute_table_archive_creee_revoque_anon():
    sql = _tout_le_sql()
    creees = set(re.findall(
        r"CREATE TABLE (?:IF NOT EXISTS )?(\w+_archive)\b", sql, re.IGNORECASE))
    sans_revoke = {t for t in creees
                   if not re.search(
                       rf"REVOKE ALL ON (?:public\.)?{t}\s+FROM\s+anon",
                       sql, re.IGNORECASE)}
    assert not sans_revoke, (
        f"Table(s) d'archive sans REVOKE anon dans sql/ : {sorted(sans_revoke)}. "
        "La RLS seule ne suffit pas : un futur CREATE POLICY … TO PUBLIC "
        "rouvrirait tout (motif v10_9).")
