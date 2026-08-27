"""`expired` n'est plus un état TERMINAL — et les gardes qui rendent ça sûr.

Le défaut corrigé : `audit_engine.fetch_pending` ne sélectionne que
`status='active'`, donc une ligne passée en `expired` faute d'avoir PU
chercher (quota mort, historique api-sports fermé au plan gratuit) ne
repassait plus jamais devant un moteur de recherche. Mesuré le 2026-08-27 :
57 % du portefeuille absent de /performance, biais de survie assuré.

Ces tests verrouillent les quatre propriétés qui rendent la relance sûre :
elle ne devine pas, elle passe APRÈS le settlement frais, elle tourne (curseur)
et elle ne peut jamais faire échouer un audit.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import pytest

from core import relance_expires

ROOT = Path(__file__).resolve().parent.parent


class _Res:
    def __init__(self, data):
        self.data = data


class FakeTable:
    """Table Supabase minimale : mémorise les updates, rend des lignes."""

    def __init__(self, nom, base):
        self.nom, self.base = nom, base
        self._filtres = {}

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._filtres[col] = val
        return self

    def order(self, *a, **k):
        return self

    def limit(self, n):
        self._n = n
        return self

    def range(self, a, b):
        self._range = (a, b)
        return self

    def update(self, patch):
        self.base.updates.append((self.nom, patch))
        return self

    def upsert(self, payload, **k):
        self.base.upserts.append((self.nom, payload))
        return self

    def delete(self):
        return self

    def execute(self):
        if self.nom == "signals":
            return _Res(list(self.base.signaux))
        if self.nom == "ai_learning_ledger":
            a, b = getattr(self, "_range", (0, 99))
            return _Res(self.base.ledger[a:b + 1])
        if self.nom == "meta":
            return _Res([{"value": str(self.base.curseur)}])
        return _Res([])


class FakeDB:
    def __init__(self, signaux=(), ledger=(), curseur=0):
        self.signaux, self.ledger, self.curseur = list(signaux), list(ledger), curseur
        self.updates, self.upserts = [], []

    def table(self, nom):
        return FakeTable(nom, self)


LIGNE = {"id": "abc", "match": "Alpha FC vs Beta FC", "sport": "soccer",
         "market_type": "h2h", "selection": "Alpha FC"}


class TestElleNeDevinePas:
    """Le cœur de la sûreté : sans score sûr, la ligne RESTE expirée."""

    def test_score_introuvable_laisse_la_ligne_expiree(self):
        db = FakeDB(ledger=[dict(LIGNE)])
        with patch.object(relance_expires, "ai_available", return_value=True), \
             patch.object(relance_expires, "fetch_match_result", return_value=None):
            faits = relance_expires.relancer(db)
        assert faits["ledger"] == 0
        assert faits["sans_score"] == 1
        assert db.updates == [], "aucune écriture ne doit partir sans score"

    def test_marche_indecidable_laisse_la_ligne_expiree(self):
        # Un marché que `determine_outcome` ne sait pas trancher rend UNKNOWN ;
        # écrire UNKNOWN au ledger serait pire que d'attendre.
        db = FakeDB(ledger=[dict(LIGNE, market_type="martingale_exotique",
                                 selection="???")])
        with patch.object(relance_expires, "ai_available", return_value=True), \
             patch.object(relance_expires, "fetch_match_result",
                          return_value={"home_score": 2, "away_score": 1, "completed": True}):
            faits = relance_expires.relancer(db)
        assert faits["indecidable"] == 1
        assert db.updates == []

    def test_un_score_trouve_ecrit_la_vraie_issue(self):
        db = FakeDB(ledger=[dict(LIGNE)])
        with patch.object(relance_expires, "ai_available", return_value=True), \
             patch.object(relance_expires, "fetch_match_result",
                          return_value={"home_score": 2, "away_score": 1, "completed": True}):
            faits = relance_expires.relancer(db)
        assert faits["ledger"] == 1
        assert ("ai_learning_ledger", {"outcome": "WIN"}) in db.updates

    def test_sans_fournisseur_ia_rien_nest_ecrit(self):
        db = FakeDB(ledger=[dict(LIGNE)])
        with patch.object(relance_expires, "ai_available", return_value=False):
            faits = relance_expires.relancer(db)
        assert faits == {"signaux": 0, "ledger": 0, "sans_score": 0, "indecidable": 0}
        assert db.updates == []


class TestLeBudgetEtLeCurseur:
    """Sans curseur, le lot repasserait éternellement sur les mêmes lignes."""

    def test_le_budget_borne_le_nombre_de_recherches(self):
        db = FakeDB(ledger=[dict(LIGNE, id=f"l{i}") for i in range(50)])
        appels = []
        with patch.object(relance_expires, "ai_available", return_value=True), \
             patch.object(relance_expires, "fetch_match_result",
                          side_effect=lambda *a, **k: appels.append(1) or None):
            relance_expires.relancer(db, budget=5)
        assert len(appels) == 5

    def test_le_curseur_avance_pour_couvrir_toutes_les_lignes(self):
        db = FakeDB(ledger=[dict(LIGNE, id=f"l{i}") for i in range(50)], curseur=10)
        with patch.object(relance_expires, "ai_available", return_value=True), \
             patch.object(relance_expires, "fetch_match_result", return_value=None):
            relance_expires.relancer(db, budget=4)
        curseurs = [p for (t, p) in db.upserts if t == "meta"]
        assert curseurs and curseurs[-1]["value"] == "14"

    def test_le_curseur_revient_au_debut_en_fin_de_liste(self):
        db = FakeDB(ledger=[], curseur=99)
        with patch.object(relance_expires, "ai_available", return_value=True):
            relance_expires.relancer(db, budget=4)
        curseurs = [p for (t, p) in db.upserts if t == "meta"]
        assert curseurs and curseurs[-1]["value"] == "0"

    def test_un_budget_nul_ne_fait_rien(self):
        db = FakeDB(ledger=[dict(LIGNE)])
        with patch.object(relance_expires, "ai_available", return_value=True):
            assert relance_expires.relancer(db, budget=0)["ledger"] == 0


class TestLaPlaceDansLaudit:
    """Deux invariants lus sur l'AST : l'ordre, et l'échec avalé."""

    @staticmethod
    def _source():
        return (ROOT / "core" / "audit_engine.py").read_text(encoding="utf-8")

    def test_la_relance_passe_apres_le_settlement_frais(self):
        src = self._source()
        arbre = ast.parse(src)
        run = next(n for n in arbre.body
                   if isinstance(n, ast.FunctionDef) and n.name == "run")
        lignes_relance, lignes_boucle = [], []
        for n in ast.walk(run):
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_relancer_expires":
                lignes_relance.append(n.lineno)
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "audit_one":
                lignes_boucle.append(n.lineno)
        assert lignes_relance, "la relance doit être appelée dans run()"
        assert lignes_boucle, "audit_one doit rester la passe principale"
        assert max(lignes_relance) > max(lignes_boucle), (
            "la relance des expirés doit passer APRÈS le settlement des signaux "
            "frais : la réserve IA est tenue en négatif, un signal du jour vaut "
            "plus qu'un match d'il y a deux semaines")

    def test_un_audit_a_vide_relance_quand_meme(self):
        # Le meilleur moment pour reprendre les expirés est justement l'audit
        # qui n'a rien de frais à régler : tout le budget est disponible.
        src = self._source()
        i_vide = src.index("Nothing to audit.")
        i_fin = src.index("--- Learning Layer ---")
        assert "_relancer_expires" in src[i_vide:i_fin]

    def test_une_panne_de_relance_ne_fait_pas_echouer_laudit(self):
        arbre = ast.parse(self._source())
        fn = next(n for n in arbre.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_relancer_expires")
        assert any(isinstance(n, ast.Try) for n in ast.walk(fn)), (
            "la relance améliore un état déjà écrit : elle ne doit jamais "
            "pouvoir faire échouer un audit qui a réglé des signaux")


class TestLaRelanceEstCablee:
    def test_le_module_est_importe_par_laudit(self):
        assert "relance_expires" in (ROOT / "core" / "audit_engine.py").read_text(
            encoding="utf-8")

    @pytest.mark.parametrize("nom", ["relancer", "RELANCE_BUDGET", "CURSEUR_KEY"])
    def test_lapi_publique_reste_stable(self, nom):
        assert hasattr(relance_expires, nom)
