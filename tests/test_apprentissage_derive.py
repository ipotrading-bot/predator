"""
tests/test_apprentissage_derive.py — l'apprentissage jugeait l'edge par
lui-même, sur la moitié des preuves, avec des étiquettes encore faillibles
(2026-09-27).

1. `clv_pct_real` vaut cote prise / clôture sharp − 1, donc ≈ l'edge d'ENTRÉE
   tant que le sharp ne bouge pas (mesuré le 2026-09-24 : dérive −0,10 %,
   106 positives contre 105 négatives). « CLV > +1 % » était vrai par
   construction : les DÉCISIONS de seuil lisent désormais la dérive sharp
   (`_derive_stats`), le CLV reste affiché.
2. `compute_and_save` lisait les 120 dernières lignes par sport, fantômes
   compris : le football était jugé sur 65 paris quand 130 existaient.
3. Deux défauts de règlement de l'audit du 2026-09-24 : ESPN réglait un match
   après prolongation ou tirs au but sur le score final, et les lignes se
   lisaient sur le PREMIER nombre du libellé (« FC Iberia 1999 +0.5 »).
"""
import inspect

import core.learning_layer as ll
from core.learning_layer import _decide_threshold, _derive_stats
from core.score_sources import _espn_candidat
from core.settlement import determine_outcome


def _l(sharp_prob, close, **extra):
    d = {"sharp_prob": sharp_prob, "closing_pinnacle_price": close}
    d.update(extra)
    return d


class TestDerive:
    def test_nulle_quand_le_sharp_ne_bouge_pas(self):
        """Le cas du 2026-09-24 : CLV à +5 %, dérive nulle."""
        etat = _derive_stats([_l(0.5, 2.0, clv_pct_real=5.0)] * 20)
        assert etat["n"] == 20 and abs(etat["avg_clv"]) < 1e-9
        assert etat["positive_rate"] == 0

    def test_positive_quand_le_marche_vient_vers_nous(self):
        """Entrée à 2,00 (p = 0,5), clôture à 1,90 : +5,26 %."""
        etat = _derive_stats([_l(0.5, 1.90)])
        assert round(etat["avg_clv"], 2) == 5.26 and etat["positive_rate"] == 1

    def test_ignore_les_lignes_sans_cloture(self):
        """Règle 14 : sans prix postérieur observé, rien — jamais un zéro."""
        etat = _derive_stats([_l(0.5, None), _l(None, 1.9), _l(0.5, "n/a"), _l(0.5, 1.0)])
        assert etat == {"n": 0, "avg_clv": None, "positive_rate": None}

    def test_ignore_une_cloture_oracle(self):
        """2026-09-29 : un prix de clôture DEMANDÉ à l'ancien LLM n'est pas une
        observation (règle 14) — il ne pèse pas dans la dérive qui décide des
        seuils, même quand la ligne porte encore le prix."""
        from core.constants import CLOSING_SRC_EXCHANGE, CLOSING_SRC_ORACLE
        etat = _derive_stats([_l(0.5, 1.50, closing_source=CLOSING_SRC_ORACLE),
                              _l(0.5, 2.00, closing_source=CLOSING_SRC_EXCHANGE)])
        assert etat["n"] == 1 and abs(etat["avg_clv"]) < 1e-9

    def test_la_source_de_cloture_est_lue(self):
        """Sans la colonne dans la sélection, le filtre ci-dessus ne voit
        jamais rien et laisse tout passer."""
        assert "closing_source" in ll._LEDGER_SELECT

    def test_meme_forme_que_le_clv(self):
        """`_decide_threshold` la lit sans changer : mêmes clés, même unité."""
        assert set(_derive_stats([_l(0.5, 1.9)])) == set(ll._clv_stats([{"clv_pct_real": 1.0}]))


class TestDecisionSansCirculariteDuClv:
    _stats = {"hit_rate": 0.65, "n": 40, "wilson_lower": 0.50, "p_breakeven": 0.55, "roi": None}

    def test_un_clv_eleve_a_derive_nulle_ne_descend_plus(self):
        """Avant : CLV +5 % sur 20 lignes → « le marché confirme → ↓ ». La
        dérive, nulle, ne confirme rien : hold."""
        lignes = [_l(0.5, 2.0, clv_pct_real=5.0)] * 20
        new_t, raison = _decide_threshold(2.0, self._stats, _derive_stats(lignes), False)
        assert new_t is None and "hold" in raison
        # Le même échantillon lu par l'ancien critère descendait :
        ancien, _ = _decide_threshold(2.0, self._stats, ll._clv_stats(lignes), False)
        assert ancien is not None and ancien < 2.0

    def test_compute_and_save_decide_sur_la_derive(self):
        src = inspect.getsource(ll.compute_and_save)
        assert src.count("_derive_stats(") == 2          # sport et segments
        assert "clv = _clv_stats" not in src and "fam_clv = _clv_stats" not in src
        assert "clv_by_sport[sport] = _clv_stats(" in src   # l'AFFICHAGE garde le CLV

    def test_la_cloture_est_lue(self):
        assert "closing_pinnacle_price" in ll._LEDGER_SELECT


class TestToutelEpoque:
    def test_plus_de_fenetre_des_120_dernieres(self):
        src = inspect.getsource(ll.compute_and_save)
        assert ".limit(120)" not in src
        assert '.gte("created_at", CALIBRATION_EPOCH)' in src

    def test_le_garde_fou_de_volume_nest_pas_une_fenetre(self):
        """Au débit mesuré (~11 lignes/jour, fantômes compris), 5 000 lignes
        couvrent plus d'un an : la borne ne tronque pas l'époque."""
        assert ll._LECTURE_MAX >= 5000


def _ev(nom_statut, hs="2", as_="1"):
    return {"competitions": [{
        "id": "1",
        "status": {"type": {"completed": True, "state": "post", "name": nom_statut}},
        "competitors": [
            {"homeAway": "home", "team": {"displayName": "Lyon"}, "score": hs},
            {"homeAway": "away", "team": {"displayName": "Nice"}, "score": as_}]}]}


class TestEspnProlongation:
    def test_final_normal_regle(self):
        for nom in ("STATUS_FULL_TIME", "STATUS_FINAL"):
            assert _espn_candidat(_ev(nom), "Lyon", "Nice")[:2] == (2, 1)

    def test_apres_prolongation_ou_tirs_au_but_refuse(self):
        """Le score final d'un match à prolongation n'est pas celui du pari :
        l'étage suivant (LiveScore, score à 90 min) prend la main."""
        for nom in ("STATUS_FINAL_AET", "STATUS_FINAL_PEN"):
            assert _espn_candidat(_ev(nom), "Lyon", "Nice") is None

    def test_prolongation_us_non_concernee(self):
        """Au basket ou au hockey US, la prolongation compte pour le pari."""
        assert _espn_candidat(_ev("STATUS_FINAL_OT"), "Lyon", "Nice")[:2] == (2, 1)


class TestLigneDuLibelle:
    def test_un_nombre_dans_le_nom_ne_fait_plus_la_ligne(self):
        """Signal 9790 (28/08) : « FC Iberia 1999 +0.5 » réglé sur 1999 →
        WIN à tous les coups. Sur +0.5, une défaite 0-2 est perdue."""
        assert determine_outcome("soccer", "spreads_home", "FC Iberia 1999 +0.5",
                                 "FC Iberia 1999", "Dinamo", 0, 2) == "LOSS"
        assert determine_outcome("soccer", "spreads_home", "FC Iberia 1999 +0.5",
                                 "FC Iberia 1999", "Dinamo", 1, 1) == "WIN"

    def test_handicap_negatif(self):
        assert determine_outcome("soccer", "spreads_away", "Nice -1.5",
                                 "Lyon", "Nice", 0, 1) == "LOSS"
        assert determine_outcome("soccer", "spreads_away", "Nice -1.5",
                                 "Lyon", "Nice", 0, 2) == "WIN"

    def test_les_totaux_ordinaires_ne_changent_pas(self):
        assert determine_outcome("soccer", "totals_over", "Over 2.5",
                                 "Lyon", "Nice", 2, 1) == "WIN"
        assert determine_outcome("soccer", "totals_under", "Under 2.5",
                                 "Lyon", "Nice", 2, 1) == "LOSS"

    def test_libelle_sans_nombre_final_ne_devine_pas(self):
        assert determine_outcome("soccer", "totals_over", "Over",
                                 "Lyon", "Nice", 2, 1) == "UNKNOWN"

    def test_une_seule_regle_avec_la_cloture(self):
        """Règle 6 : la capture de clôture et le règlement lisent la ligne au
        même endroit."""
        import core.settlement as st
        assert "_TRAILING_NUMBER" in inspect.getsource(st._ligne_du_libelle)
