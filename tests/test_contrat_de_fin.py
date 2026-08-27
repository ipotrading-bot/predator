"""
tests/test_contrat_de_fin.py — PHASE B5.

Ce dépôt échoue en silence, et toujours de la même façon : le travail ne se
fait pas, rien ne lève, GitHub Actions affiche une coche verte. ~17 h de
« 0/N signals persisted » le 2026-07-07 ; deux jours d'audit rendant
« 0 settled | 52 skipped » EN VERT du 24 au 26 août ; un disjoncteur incapable
de se déclencher jusqu'à B4. Aucun n'était un plantage.

Ce que ces tests gardent :

  · les trois conditions sont des CONJONCTIONS, jamais des seuils — « 0 match »
    ou « 0 signal » restent parfaitement légitimes, et doivent rester VERTS ;
  · le verdict est une fonction PURE, donc éprouvable sans rejouer un scan ;
  · les trois points de sortie sont réellement CÂBLÉS — un contrat que
    personne n'appelle est une décoration ;
  · la troisième condition vit dans `audit_engine`, seul module qui règle
    quoi que ce soit.
"""
import pytest

from core.run_contract import terminer, verdict_de_fin
# Le harnais REPRICE est le seul chemin qui produit un signal sans toucher une
# source payante — on le réutilise plutôt que d'en écrire un second, qui
# divergerait de lui au premier changement.
from test_reprice_mode import _meta_row, _slate_match, cabler_reprice


class TestZeroNestPasUnEchec:
    """Le point le plus important. Depuis A1 le moteur n'émet quasiment plus
    rien, et A6 a établi qu'aucun seuil n'est justifié : un run qui ne produit
    aucun signal est le résultat ATTENDU. Le rendre rouge noierait le vrai
    signal d'alarme sous une alerte quotidienne."""

    def test_un_run_qui_ne_trouve_rien_reste_vert(self):
        assert verdict_de_fin() is None

    def test_zero_match_sans_source_joignable_reste_vert(self):
        assert verdict_de_fin(sources_joignables=False, matches_vus=0) is None

    def test_zero_signal_emis_reste_vert(self):
        # 40 matchs analysés, aucun edge : fonctionnement normal depuis A1.
        assert verdict_de_fin(sources_joignables=True, matches_vus=40,
                              signaux_emis=0, signaux_persistes=0) is None

    def test_aucun_reglement_eligible_reste_vert(self):
        # « Nothing to audit » n'est pas un audit stérile.
        assert verdict_de_fin(settlement_eligible=0, settlement_regles=0) is None

    def test_un_run_pleinement_productif_reste_vert(self):
        assert verdict_de_fin(sources_joignables=True, matches_vus=40,
                              signaux_emis=3, signaux_persistes=3,
                              settlement_eligible=10, settlement_regles=7) is None


class TestLesTroisContradictions:
    """Chacune décrit un pipeline cassé AU MILIEU — jamais un marché calme."""

    def test_des_sources_ont_repondu_mais_aucun_match_nen_est_sorti(self):
        motif = verdict_de_fin(sources_joignables=True, matches_vus=0)
        assert motif is not None and "AUCUN match" in motif

    def test_des_signaux_emis_mais_aucun_persiste(self):
        motif = verdict_de_fin(signaux_emis=5, signaux_persistes=0)
        assert motif is not None and "AUCUN persisté" in motif
        assert "5" in motif, "le motif doit dire COMBIEN ont été perdus"

    def test_des_reglements_eligibles_mais_aucun_abouti(self):
        motif = verdict_de_fin(settlement_eligible=52, settlement_regles=0)
        assert motif is not None and "AUCUN abouti" in motif
        assert "52" in motif

    def test_un_seul_signal_persiste_sur_cinq_ne_declenche_pas(self):
        """La condition est « AUCUN », pas « pas assez ». Une écriture qui
        échoue sur cinq est un incident isolé ; cinq sur cinq est une panne
        systématique — seule la seconde se distingue d'un aléa."""
        assert verdict_de_fin(signaux_emis=5, signaux_persistes=1) is None

    def test_un_seul_reglement_abouti_ne_declenche_pas(self):
        assert verdict_de_fin(settlement_eligible=52, settlement_regles=1) is None


class TestTerminer:
    def test_un_motif_fait_sortir_en_echec(self):
        with pytest.raises(SystemExit) as e:
            terminer("des signaux perdus", contexte="scan")
        assert e.value.code == 1

    def test_labsence_de_motif_ne_fait_rien(self):
        assert terminer(None) is None

    def test_le_motif_est_journalise_en_critical_avec_son_contexte(self, caplog):
        import logging
        with caplog.at_level(logging.CRITICAL, logger="PREDATOR.run_contract"):
            with pytest.raises(SystemExit):
                terminer("52 règlements éligibles, AUCUN abouti", contexte="audit")
        rec = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
        assert rec and "audit" in rec[0].getMessage() and "52" in rec[0].getMessage()


class TestLeContratEstReellementCable:
    """Un contrat que personne n'appelle est une décoration. Ces tests
    vérifient le CÂBLAGE, pas la logique — elle est éprouvée plus haut."""

    def test_le_moteur_de_scan_appelle_le_contrat(self):
        import inspect
        import run_engine
        src = inspect.getsource(run_engine.run)
        assert src.count("_terminer_run(verdict_de_fin(") >= 2, \
            "les deux sorties du scan (0 match, 0 persisté) doivent être gardées"

    def test_la_sortie_zero_persiste_lit_les_vrais_compteurs(self):
        import inspect
        import run_engine
        src = inspect.getsource(run_engine.run)
        assert "signaux_emis=len(signals)" in src
        assert "signaux_persistes=saved_count" in src

    def test_laudit_appelle_le_contrat_sur_ses_propres_compteurs(self):
        import inspect
        from core import audit_engine
        src = inspect.getsource(audit_engine.run)
        assert "settlement_eligible=len(pending)" in src
        assert 'settlement_regles=counts["settled"]' in src

    def test_le_verdict_de_laudit_est_pose_APRES_lapprentissage(self):
        """Un audit stérile doit quand même avoir tenté d'apprendre de ce
        qu'il a : sortir avant le priverait de ce tour-là."""
        import inspect
        from core import audit_engine
        src = inspect.getsource(audit_engine.run)
        assert src.index("_learn(sb)") < src.index("_terminer_run(")


class TestLauditSterileSortEnEchec:
    """Bout en bout sur `audit_engine.run()` — le cas resté vert deux jours
    entiers, du 24 au 26 août 2026."""

    @staticmethod
    def _cabler(monkeypatch, pending, resultats):
        from core import audit_engine as ae

        class _SB:
            def table(self, _n):
                return self

            def select(self, *_a, **_k):
                return self

            def eq(self, *_a, **_k):
                return self

            def limit(self, *_a, **_k):
                return self

            def upsert(self, *_a, **_k):
                return self

            def delete(self):
                return self

            def execute(self):
                return type("R", (), {"data": []})()

        monkeypatch.setattr(ae, "get_db", lambda write=False: _SB())
        monkeypatch.setattr(ae, "ai_available", lambda: True)
        monkeypatch.setattr(ae, "gemini_quota_dead", lambda: False)
        monkeypatch.setattr(ae, "fetch_pending", lambda _sb: pending)
        monkeypatch.setattr(ae, "_learn", lambda _sb: None)
        monkeypatch.setattr(ae, "_signaler_audit_sterile", lambda *_a, **_k: None)
        monkeypatch.setattr(ae, "_effacer_marqueur_sterile", lambda *_a, **_k: None)
        sequence = iter(resultats)
        monkeypatch.setattr(ae, "audit_one", lambda *_a, **_k: next(sequence))
        return ae

    def test_cinquante_deux_en_attente_zero_regle_sort_rouge(self, monkeypatch):
        pending = [{"id": i, "match": f"A{i} vs B{i}"} for i in range(52)]
        ae = self._cabler(monkeypatch, pending, ["skipped"] * 52)
        with pytest.raises(SystemExit) as e:
            ae.run()
        assert e.value.code == 1

    def test_un_seul_reglement_abouti_sort_vert(self, monkeypatch):
        pending = [{"id": i, "match": f"A{i} vs B{i}"} for i in range(52)]
        ae = self._cabler(monkeypatch, pending, ["settled"] + ["skipped"] * 51)
        ae.run()      # ne doit pas lever

    def test_rien_a_auditer_sort_vert(self, monkeypatch):
        ae = self._cabler(monkeypatch, [], [])
        ae.run()      # « Nothing to audit » n'est pas un audit stérile


class TestLeScanQuiNePersisteRienSortEnEchec:
    """Bout en bout sur `run_engine.run()` — l'incident du 2026-07-07 :
    ~17 h de « 0/N signals persisted », chaque run VERT, chaque écriture
    refusée une par une par une RLS 42501.

    Le harnais du mode REPRICE est réutilisé parce qu'il est le seul chemin
    qui produit un signal sans toucher une source payante."""

    def test_des_signaux_emis_et_aucun_persiste_sort_rouge(self, monkeypatch):
        import run_engine as eng
        sb, _telegrams, _mb = cabler_reprice(monkeypatch)
        sb.meta["cache_soft_slate"] = _meta_row([_slate_match()])

        # Chaque écriture échoue — exactement le mode de panne de juillet.
        monkeypatch.setattr(eng, "_save", lambda _sb, _s: False)
        with pytest.raises(SystemExit) as e:
            eng.run()
        assert e.value.code == 1

    def test_le_meme_run_qui_persiste_sort_vert(self, monkeypatch):
        """Témoin : sans lui, le test ci-dessus passerait même si `run()`
        échouait pour une tout autre raison."""
        import run_engine as eng
        sb, _telegrams, _mb = cabler_reprice(monkeypatch)
        sb.meta["cache_soft_slate"] = _meta_row([_slate_match()])
        eng.run()      # ne doit pas lever
        assert [r for r in sb.signals if r.get("status") == "active"]
