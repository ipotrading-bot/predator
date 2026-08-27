"""
tests/test_prix_executable.py — PHASE A1.

Garde LE contrat économique du moteur : le prix comparé à la référence sharp
est celui qu'on peut RÉELLEMENT jouer, jamais un prix dévigorisé.

Pourquoi ces tests existent : la panne d'origine ne levait aucune erreur. Le
moteur tournait vert, publiait des edges à deux chiffres, et mesurait en fait
une divergence d'opinion entre books dont le coût de transaction n'avait jamais
été soustrait. Rien dans la suite ne s'en serait aperçu. Chaque test ci-dessous
correspond donc à une façon précise dont le défaut pourrait revenir :

  · `to_binary` re-dévigorisant le côté soft (la panne elle-même) ;
  · `extract_prices` réassemblant un 1X2 issue par issue (edge fabriqué qui
    s'ajoute au précédent) ;
  · le repricing de dernière minute relisant une cote 1X2 brute (la marge du
    book passe alors pour un mouvement de ligne favorable) ;
  · `advice` taisant qu'un DNB synthétique engage DEUX jambes (l'opérateur
    mise tout sur l'équipe et détient une exposition au nul non modélisée).
"""
import logging
from datetime import datetime, timedelta, timezone

import pytest

import run_engine
from core.api_sports import _favourite_side, extract_prices
from core.math_engine import (calc_dnb, dnb_leg_split, executable_price,
                              synthetic_dnb, to_binary)

log = logging.getLogger("test")


# ── Le DNB synthétique ───────────────────────────────────────────────────

class TestSyntheticDnb:
    def test_la_formule_est_celle_des_deux_jambes(self):
        # Mise 1 : 1/oX sur le nul (qui rend 1), le reste sur l'équipe.
        o_team, o_draw = 1.80, 3.60
        attendu = o_team * (1 - 1 / o_draw)
        assert synthetic_dnb(o_team, o_draw) == pytest.approx(attendu, abs=1e-4)

    def test_le_prix_executable_est_toujours_sous_le_prix_devigorise(self):
        """C'est LA propriété qui manquait : dévigoriser rend un prix meilleur
        que celui du book, puisque sa marge en a été retirée."""
        for o1, ox, o2 in [(1.80, 3.60, 4.50), (2.10, 3.30, 3.40),
                           (1.20, 6.00, 15.0), (2.50, 3.10, 2.80)]:
            assert synthetic_dnb(o1, ox) < calc_dnb(o1, o2, ox), (o1, ox, o2)

    def test_un_carnet_sans_marge_fait_coincider_les_deux(self):
        # 1/2 + 1/4 + 1/4 = 1 : plus rien à retirer, donc plus rien à perdre.
        assert synthetic_dnb(2.0, 4.0) == pytest.approx(calc_dnb(2.0, 4.0, 4.0),
                                                        abs=0.01)

    @pytest.mark.parametrize("team,draw", [(0, 3.5), (1.8, 0), (1.0, 3.5),
                                           (1.8, 1.0), (None, None)])
    def test_une_entree_invalide_rend_zero_et_ne_leve_pas(self, team, draw):
        assert synthetic_dnb(team, draw) == 0.0

    def test_un_nul_trop_court_ne_produit_pas_de_cote_sous_1(self):
        # oX = 1.05 → prix < 1 : une « cote » inférieure à 1 est un non-sens
        # qui passerait tous les gates en aval sous forme de perte garantie.
        assert synthetic_dnb(1.02, 1.05) == 0.0


class TestRepartitionDesJambes:
    def test_les_deux_parts_somment_a_un(self):
        for draw in (2.5, 3.4, 6.0, 12.0):
            nul, equipe = dnb_leg_split(draw)
            assert nul + equipe == pytest.approx(1.0, abs=1e-4)

    def test_la_jambe_nul_rembourse_exactement_la_mise(self):
        draw = 3.40
        nul, _ = dnb_leg_split(draw)
        assert nul * draw == pytest.approx(1.0, abs=1e-4)

    def test_une_cote_de_nul_invalide_ne_produit_pas_de_repartition(self):
        assert dnb_leg_split(0) == (0.0, 0.0)
        assert dnb_leg_split(1.0) == (0.0, 0.0)


# ── to_binary : le prix d'entrée ─────────────────────────────────────────

class TestToBinaryRendLePrixExecutable:
    ODDS = {"1": 1.80, "X": 3.60, "2": 4.50}

    def test_le_football_rend_le_dnb_synthetique_pas_le_devigorise(self):
        price, label, fav = to_binary(self.ODDS, "soccer", "Lille", "Reims")
        assert price == pytest.approx(synthetic_dnb(1.80, 3.60), abs=1e-4)
        assert price != pytest.approx(calc_dnb(1.80, 4.50, 3.60), abs=1e-4)
        assert fav == "Lille"

    def test_le_libelle_annonce_les_deux_jambes(self):
        assert to_binary(self.ODDS, "soccer", "Lille", "Reims")[1] == "AH 0.0 (2 jambes)"

    def test_un_ah0_reellement_cote_est_pris_brut_et_prime(self):
        odds = {**self.ODDS, "ah0_1": 1.42}
        price, label, _ = to_binary(odds, "soccer", "Lille", "Reims")
        assert price == 1.42          # brut, aucune reconstruction
        assert label == "AH 0.0"      # une seule jambe

    def test_lah0_du_mauvais_cote_est_ignore(self):
        # Le favori est le domicile : `ah0_2` ne le concerne pas.
        odds = {**self.ODDS, "ah0_2": 5.00}
        price, label, _ = to_binary(odds, "soccer", "Lille", "Reims")
        assert price == pytest.approx(synthetic_dnb(1.80, 3.60), abs=1e-4)
        assert label == "AH 0.0 (2 jambes)"

    def test_le_favori_se_lit_sur_le_1x2_brut(self):
        away_fav = {"1": 4.50, "X": 3.60, "2": 1.80}
        price, _, fav = to_binary(away_fav, "soccer", "Lille", "Reims")
        assert fav == "Reims"
        assert price == pytest.approx(synthetic_dnb(1.80, 3.60), abs=1e-4)

    def test_football_sans_nul_est_refuse_jamais_rabattu_sur_le_moneyline(self):
        # Rabattre sur le ML comparerait une entrée ML à une référence DNB :
        # edge faux, et silencieux.
        assert to_binary({"1": 1.80, "2": 4.50}, "soccer", "A", "B") == (0.0, None, "")
        assert to_binary({"1": 1.80, "X": 1.0, "2": 4.50}, "soccer", "A", "B") == (0.0, None, "")

    @pytest.mark.parametrize("sport", ["basketball", "tennis", "mma", "baseball"])
    def test_hors_football_la_cote_brute_est_deja_executable(self, sport):
        price, label, fav = to_binary({"1": 1.80, "2": 2.10}, sport, "A", "B")
        assert (price, label, fav) == (1.80, "Moneyline", "A")


class TestExecutablePriceEstLePointUnique:
    """`to_binary` et le repricing doivent lire la MÊME règle — sinon le
    last-look compare une grandeur à une autre."""

    def test_to_binary_delegue_bien_a_executable_price(self):
        odds = {"1": 1.75, "X": 3.80, "2": 4.60}
        assert to_binary(odds, "soccer", "A", "B")[0] == executable_price(odds, "soccer", "1")

    def test_le_cote_demande_est_respecte(self):
        odds = {"1": 1.75, "X": 3.80, "2": 4.60}
        assert executable_price(odds, "soccer", "2") == pytest.approx(
            synthetic_dnb(4.60, 3.80), abs=1e-4)

    def test_un_cote_inconnu_rend_zero(self):
        assert executable_price({"1": 1.8, "X": 3.4, "2": 4.0}, "soccer", "X") == 0.0


# ── extract_prices : un seul book ────────────────────────────────────────

def _bk(name, o1, ox, o2):
    values = [{"value": "Home", "odd": str(o1)}, {"value": "Draw", "odd": str(ox)},
              {"value": "Away", "odd": str(o2)}]
    return {"name": name, "bets": [{"name": "Match Winner", "values": values}]}


class TestLineShoppingSurLePrixFinal:
    def test_le_bloc_rendu_appartient_a_un_book_reel(self):
        a = _bk("Bwin", 2.30, 3.40, 2.80)
        b = _bk("Unibet", 2.20, 3.50, 3.00)
        soft, _ = extract_prices([a, b], draw=True)
        assert soft in ({"1": 2.30, "X": 3.40, "2": 2.80},
                        {"1": 2.20, "X": 3.50, "2": 3.00})
        # L'assemblage que produisait l'ancien max par issue :
        assert soft != {"1": 2.30, "X": 3.50, "2": 3.00}

    def test_cest_le_prix_executable_qui_departage_pas_la_cote_nue(self):
        # B a un favori MOINS cher mais un nul bien plus généreux : son DNB
        # exécutable est meilleur. Trier sur la cote nue choisirait A.
        a = _bk("A", 1.80, 3.20, 4.50)
        b = _bk("B", 1.78, 8.00, 4.40)
        assert synthetic_dnb(1.78, 8.00) > synthetic_dnb(1.80, 3.20)
        soft, _ = extract_prices([a, b], draw=True)
        assert soft == {"1": 1.78, "X": 8.00, "2": 4.40}

    def test_le_book_sharp_ne_gonfle_jamais_le_soft(self):
        soft, sharp = extract_prices(
            [_bk("Pinnacle", 2.50, 3.20, 2.90), _bk("Bwin", 2.30, 3.40, 2.80)],
            draw=True)
        assert sharp == {"1": 2.50, "X": 3.20, "2": 2.90}
        assert soft == {"1": 2.30, "X": 3.40, "2": 2.80}

    def test_sans_book_soft_il_ny_a_pas_de_prix_soft(self):
        soft, sharp = extract_prices([_bk("Pinnacle", 2.50, 3.20, 2.90)], draw=True)
        assert soft is None and sharp is not None

    def test_le_favori_se_decide_au_consensus_pas_sur_un_seul_book(self):
        # Deux books font le domicile favori, un seul l'extérieur : le
        # consensus doit tenir, sinon l'ordre de la réponse HTTP déciderait.
        books = [{"1": 4.00, "X": 3.5, "2": 1.85},
                 {"1": 1.80, "X": 3.5, "2": 4.20},
                 {"1": 1.82, "X": 3.5, "2": 4.10}]
        assert _favourite_side(books) == "1"

    def test_un_book_sans_nul_ne_peut_pas_gagner_le_shopping(self):
        sans_nul = _bk("SansNul", 1.50, 0.0, 4.00)
        avec_nul = _bk("AvecNul", 1.45, 3.60, 4.10)
        soft, _ = extract_prices([sans_nul, avec_nul], draw=True)
        assert soft == {"1": 1.45, "X": 3.60, "2": 4.10}


# ── Le chemin d'émission ─────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _emit_one(**kw):
    signals = []
    # EV visée ~+4.5 % : au-dessus du plancher, sous SUSPECT_EDGE.
    params = dict(executable_odd=1.90, pin_odd=1.80, sharp_prob=0.55)
    params.update(kw)
    run_engine._emit(signals, None, _now(), log, "Lille vs Reims", "soccer",
                     "L1", "h2h", "AH 0.0 (2 jambes)",
                     params["executable_odd"], params["pin_odd"],
                     params["sharp_prob"], "⚽",
                     selection_name="Lille", min_edge=1.5,
                     match_time=(_now() + timedelta(hours=4)).isoformat(),
                     dnb_draw_odd=kw.get("dnb_draw_odd", 0.0))
    return signals


class TestSignalEnMemoire:
    def test_le_moteur_nomme_le_prix_executable_odd(self):
        (sig,) = _emit_one()
        assert sig["executable_odd"] == 1.90
        assert "xbet_odd" not in sig, \
            "le moteur ne doit plus porter l'ancien nom : c'est lui qui " \
            "entretenait la confusion entre prix dévigorisé et prix jouable"

    def test_la_persistance_traduit_vers_la_colonne_historique(self):
        """La colonne DB garde `xbet_odd` — dashboard, closing_line,
        settlement et audit la relisent. La traduction se fait au point
        unique de persistance, pas en laissant les deux noms cohabiter."""
        (sig,) = _emit_one()
        vus = {}

        class _Table:
            def select(self, *_a, **_k): return self
            def eq(self, *_a, **_k): return self
            def order(self, *_a, **_k): return self
            def limit(self, *_a, **_k): return self
            def execute(self): return type("R", (), {"data": []})()
            def insert(self, payload):
                vus.update(payload)
                return self

        class _SB:
            def table(self, _name): return _Table()

        assert run_engine._save(_SB(), sig) is True
        assert vus["xbet_odd"] == 1.90
        assert "executable_odd" not in vus

    def test_ledge_se_calcule_sur_le_prix_executable(self):
        (sig,) = _emit_one(executable_odd=1.90, sharp_prob=0.55)
        assert sig["edge_pct"] == pytest.approx((0.55 * 1.90 - 1) * 100, abs=0.01)


class TestAdviceAnnonceLesDeuxJambes:
    def test_un_dnb_synthetique_dit_la_repartition_et_le_meme_book(self):
        (sig,) = _emit_one(dnb_draw_odd=3.40)
        advice = sig["advice"]
        nul, equipe = dnb_leg_split(3.40)
        assert "MÊME book" in advice
        assert f"{equipe * 100:.1f}%" in advice
        assert f"{nul * 100:.1f}%" in advice
        assert "nul" in advice

    def test_la_mise_annoncee_est_lexposition_TOTALE_a_repartir(self):
        (sig,) = _emit_one(dnb_draw_odd=3.40)
        nul, equipe = dnb_leg_split(3.40)
        total = sig["kelly_pct"]
        assert f"{total * equipe:.2f}%" in sig["advice"]
        assert f"{total * nul:.2f}%" in sig["advice"]

    def test_un_pari_a_une_seule_jambe_nannonce_aucune_repartition(self):
        # AH 0.0 réellement coté, ou moneyline : rien à répartir. Annoncer une
        # répartition ferait poser un pari sur le nul qui n'a pas lieu d'être.
        (sig,) = _emit_one(dnb_draw_odd=0.0)
        assert "MÊME book" not in sig["advice"]
        assert "répartir" not in sig["advice"]
