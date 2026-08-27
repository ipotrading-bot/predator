"""
tests/test_oracle_inerte.py — PHASE A4.

`core/oracle.py` demande à un LLM de chercher « la cote Pinnacle » d'un match,
et le moteur traite la valeur rendue comme sa RÉFÉRENCE SHARP : elle fixe
`sharp_prob`, donc l'edge, donc la mise. Rien ne garantit qu'un tel prix ait
jamais été affiché. Le budget de repêchage passe donc à ZÉRO par défaut.

Ce qui est gardé ici, et pourquoi :

  · le défaut vaut 0 — sans quoi tout le reste est décoratif ;
  · `run_engine` le DÉRIVE de `core.oracle` au lieu de le réécrire — les deux
    valeurs figées séparément, c'est la panne signature de ce dépôt ;
  · aucun mode de scan ne le rétablit — `guerrilla` posait `MAX_ORACLE=3`, ce
    qui aurait annulé le passage à zéro pour le mode qui en abuse le plus ;
  · l'appel à l'oracle reste GARDÉ par le budget — vérifié sur l'AST, parce
    que la boucle vit en ligne dans `run()` et qu'un refactor pourrait libérer
    l'appel sans qu'aucun test fonctionnel ne s'en aperçoive ;
  · le code reste VIVANT — `MAX_ORACLE=3` le rétablit entièrement. Il n'est
    pas supprimé, il est débranché.
"""
import ast
import importlib
import os
import pathlib

import pytest

import run_engine
from core import oracle
from scripts.ci_scan_mode import MODE_ENV

_RACINE = pathlib.Path(__file__).resolve().parent.parent


class TestLeDefautEstZero:
    def test_le_budget_de_repechage_est_nul_par_defaut(self):
        assert oracle.MAX_ORACLE_DEFAULT == 0

    def test_le_moteur_derive_la_valeur_au_lieu_de_la_reecrire(self):
        assert run_engine._MAX_ORACLE == oracle.MAX_ORACLE_DEFAULT

    def test_aucun_nombre_de_repechages_nest_ecrit_en_dur_dans_le_moteur(self):
        src = (_RACINE / "run_engine.py").read_text(encoding="utf-8")
        ligne = next(l for l in src.splitlines() if l.startswith("_MAX_ORACLE ="))
        assert "MAX_ORACLE_DEFAULT" in ligne, ligne
        assert '"3"' not in ligne and "'3'" not in ligne, ligne


class TestAucunModeNeLeRetablit:
    """`guerrilla` posait `MAX_ORACLE=3`. Une constante mise à zéro d'un côté
    et rétablie de l'autre ne protège de rien."""

    @pytest.mark.parametrize("mode", sorted(MODE_ENV))
    def test_aucun_mode_de_scan_ne_pose_max_oracle(self, mode):
        assert "MAX_ORACLE" not in MODE_ENV[mode], \
            f"le mode {mode} rétablit l'oracle que A4 débranche"

    def test_le_reste_du_renforcement_guerrilla_est_conserve(self):
        # A4 ne débranche QUE le repêchage par match. La recherche groupée
        # (`fetch_pinnacle_prices`) n'est pas dans son périmètre, et les
        # budgets qui la servent doivent rester intacts.
        g = MODE_ENV["guerrilla"]
        for cle in ("TAVILY_RUN_BUDGET", "PINNACLE_TAVILY_QUERIES",
                    "PINNACLE_BATCH", "SEARCH_MAX_TOKENS"):
            assert cle in g, cle


class TestLappelResteGardeParLeBudget:
    """La boucle d'oracle vit EN LIGNE dans `run()` : aucun test fonctionnel
    ne peut l'isoler sans rejouer un scan complet. On vérifie donc sur l'AST
    que l'appel n'est pas atteignable hors du garde de budget."""

    def _appels_get_pinnacle_price(self, arbre):
        for noeud in ast.walk(arbre):
            if isinstance(noeud, ast.Call):
                f = noeud.func
                nom = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
                if nom == "get_pinnacle_price":
                    yield noeud

    def test_le_moteur_nappelle_loracle_que_sous_le_garde_de_budget(self):
        arbre = ast.parse((_RACINE / "run_engine.py").read_text(encoding="utf-8"))
        # Chaque appel doit avoir un `if`/`elif` ancêtre dont le TEST nomme
        # MAX_ORACLE. On remonte la parenté en marquant les enfants.
        parents = {}
        for noeud in ast.walk(arbre):
            for enfant in ast.iter_child_nodes(noeud):
                parents[enfant] = noeud

        appels = list(self._appels_get_pinnacle_price(arbre))
        assert appels, "l'appel a disparu : ce test ne garde plus rien"

        for appel in appels:
            garde = False
            courant = appel
            while courant in parents:
                courant = parents[courant]
                if isinstance(courant, ast.If):
                    noms = {n.id for n in ast.walk(courant.test)
                            if isinstance(n, ast.Name)}
                    if any("MAX_ORACLE" in n for n in noms):
                        garde = True
                        break
            assert garde, (
                "un appel à get_pinnacle_price n'est plus sous le garde "
                f"MAX_ORACLE (ligne {appel.lineno})")

    def test_un_budget_nul_ne_laisse_passer_aucun_repechage(self):
        # Le garde est `oracle_used < MAX_ORACLE`. À zéro, il est faux dès le
        # premier match, quel que soit le nombre de matchs sans prix sharp.
        assert not (0 < run_engine._MAX_ORACLE)


class TestLeCodeResteVivant:
    """« Conservé, inerte » — pas supprimé. Le jour où la sortie de l'oracle
    serait recoupée avec un prix réellement observé, il doit suffire de
    reposer la variable."""

    def test_la_fonction_existe_toujours_et_est_appelable(self):
        assert callable(oracle.get_pinnacle_price)
        assert oracle._SHARP_BOOKS, "la chaîne de repli a été vidée"

    def test_la_variable_denvironnement_le_retablit(self, monkeypatch):
        monkeypatch.setenv("MAX_ORACLE", "3")
        recharge = importlib.reload(run_engine)
        try:
            assert recharge._MAX_ORACLE == 3
        finally:
            monkeypatch.delenv("MAX_ORACLE", raising=False)
            importlib.reload(run_engine)

    def test_sans_variable_on_revient_a_zero(self):
        assert os.environ.get("MAX_ORACLE") is None
        assert run_engine._MAX_ORACLE == 0


class TestCeQueA4NeCouvrePas:
    """Le réglage ne ferme pas toutes les portes, et le dire fait partie du
    contrat : deux autres chemins font encore prixer un « sharp » par un LLM.
    Ces tests échouent le jour où l'un d'eux disparaît — ce qui est le signal
    qu'il faut mettre à jour la documentation, pas un problème."""

    def test_la_recherche_groupee_reste_active_et_hors_perimetre(self):
        from core import harvester
        assert callable(harvester.fetch_pinnacle_prices)
        # Elle n'est gouvernée par aucun budget d'oracle.
        src = (_RACINE / "run_engine.py").read_text(encoding="utf-8")
        appel = next(l for l in src.splitlines() if "fetch_pinnacle_prices(" in l
                     and "def " not in l)
        assert "MAX_ORACLE" not in appel

    def test_la_ligne_de_cloture_reste_estimee_par_le_meme_oracle(self):
        from core import audit_engine
        assert audit_engine.get_pinnacle_price is oracle.get_pinnacle_price
        # Sous son PROPRE budget, indépendant de MAX_ORACLE.
        from core.constants import CLOSING_LINE_BUDGET
        assert CLOSING_LINE_BUDGET > 0
