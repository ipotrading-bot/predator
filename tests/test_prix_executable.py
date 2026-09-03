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
  · le repricing de dernière minute relisant une cote 1X2 brute (la marge du
    book passe alors pour un mouvement de ligne favorable) ;
  · `advice` taisant qu'un DNB synthétique engage DEUX jambes (l'opérateur
    mise tout sur l'équipe et détient une exposition au nul non modélisée).
"""
import logging
from datetime import datetime, timedelta, timezone

import pytest

import run_engine
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



def _bk(name, o1, ox, o2):
    values = [{"value": "Home", "odd": str(o1)}, {"value": "Draw", "odd": str(ox)},
              {"value": "Away", "odd": str(o2)}]
    return {"name": name, "bets": [{"name": "Match Winner", "values": values}]}


# ── Le chemin d'émission ─────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _emit_one(**kw):
    signals = []
    # Le pari doit être RÉELLEMENT jouable : EV positive APRÈS la taxe de 20 %
    # (rétablie en A2), sous SUSPECT_EDGE, et avec une mise Kelly non nulle.
    # cote 1.30 / p 0.83 → +7.90 % brut, +2.92 % net, f* = 0.1217.
    # ⚠️ Une EV brute suffit de moins en moins à mesure que la cote monte : à
    # 1.90 il faut +10.47 % brut pour seulement atteindre le point mort après
    # taxe, soit AU-DESSUS de SUSPECT_EDGE (10 %). Ce n'est pas un détail de
    # fixture, c'est la contradiction que A6 devra trancher.
    params = dict(executable_odd=1.30, pin_odd=1.25, sharp_prob=0.83)
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
        assert sig["executable_odd"] == 1.30
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
        assert vus["xbet_odd"] == 1.30
        assert "executable_odd" not in vus

    def test_ledge_se_calcule_sur_le_prix_executable(self):
        (sig,) = _emit_one(executable_odd=1.30, sharp_prob=0.83)
        assert sig["edge_pct"] == pytest.approx((0.83 * 1.30 - 1) * 100, abs=0.01)


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


# ── La ligne comparée doit être LA MÊME ligne ────────────────────────────

class TestMemeLigne:
    """Le pendant de A1 pour les handicaps et les totaux. A1 avait corrigé le
    PRIX du h2h ; ici c'est le PARI lui-même qui n'était pas le même des deux
    côtés.

    L'ancienne garde s'écrivait
    `if xs and ps and abs(abs(xs) - abs(ps)) > 0.5` et portait trois défauts,
    chacun fabriquant exactement l'objet qu'elle prétendait écarter :

      1. `if xs and ps` — 0.0 est FAUX en Python, donc une ligne AH 0.0
         désactivait la garde entièrement ;
      2. tolérance de 0,5 — AH −1,0 contre AH −1,5 passait ;
      3. `abs(abs(x) - abs(p))` — le double `abs` détruit le SIGNE : −0,5
         contre +0,5 passait, c'est-à-dire les handicaps OPPOSÉS.

    MESURÉ le 2026-08-27 sur le slate réel : 21 paires de spreads sur 24
    avaient des lignes différentes, dont 20 passaient l'ancienne garde. Les
    edges tombent de « 29 lignes au-dessus de +1,5 %, max +13,88 % » à
    « aucune, max −2,30 % ». Toute la queue positive était l'écart de prix
    entre deux paris différents.
    """

    @staticmethod
    def _ligne(soft_pt, sharp_pt):
        return run_engine._meme_ligne({"point": soft_pt}, {"point": sharp_pt},
                                      "spreads", "A vs B", "⚽", log)

    def test_deux_lignes_identiques_passent(self):
        assert self._ligne(-1.0, -1.0) == -1.0
        assert self._ligne(2.5, 2.5) == 2.5

    def test_zero_et_moins_zero_sont_le_meme_handicap(self):
        assert self._ligne(0.0, -0.0) == 0.0

    def test_une_ligne_a_zero_ne_desactive_plus_la_garde(self):
        """Défaut n°1, et le plus coûteux : `if 0.0` est faux. Une AH 0.0
        soft pouvait être comparée à un handicap −1,5 sharp sans un mot."""
        assert self._ligne(0.0, -1.5) is None
        assert self._ligne(-1.5, 0.0) is None

    def test_une_demi_unite_decart_est_refusee(self):
        """Défaut n°2. Sur un handicap, une demi-unité change le pari : l'un
        rembourse une victoire d'un but exact, l'autre la perd."""
        assert self._ligne(-1.0, -1.5) is None
        assert self._ligne(-1.0, -0.5) is None

    def test_deux_handicaps_OPPOSES_sont_refuses(self):
        """Défaut n°3, le pire : le double `abs` faisait comparer le prix du
        FAVORI chez un book à celui de l'OUTSIDER chez l'autre."""
        assert self._ligne(-0.5, 0.5) is None
        assert self._ligne(-1.0, 1.0) is None

    def test_une_ligne_absente_fait_REFUSER_pas_passer(self):
        """On ne peut pas vérifier qu'on compare le même pari sans voir la
        ligne. Même contrat que le football sans prix de nul : refus
        silencieux plutôt qu'un prix posé au hasard."""
        assert run_engine._meme_ligne({}, {"point": -1.0}, "spreads",
                                      "A vs B", "⚽", log) is None
        assert run_engine._meme_ligne({"point": -1.0}, {}, "spreads",
                                      "A vs B", "⚽", log) is None

    def test_une_ligne_illisible_ne_leve_pas(self):
        assert run_engine._meme_ligne({"point": "n/a"}, {"point": -1.0},
                                      "spreads", "A vs B", "⚽", log) is None

    def test_les_deux_marches_partagent_la_MEME_garde(self):
        """Totals et spreads avaient chacun leur copie, avec des défauts
        différents — le double `abs` n'était que côté spreads. Deux copies
        d'une même règle finissent toujours par diverger."""
        import inspect
        for fonction in (run_engine._process_totals, run_engine._process_spreads):
            src = inspect.getsource(fonction)
            assert "_meme_ligne(" in src, fonction.__name__
            assert "abs(abs(" not in src, f"{fonction.__name__} recompare à la main"


class TestAlignerSurMemeLigne:
    """L'alignement rend comparables des paires que la garde refusait — sans
    jamais relâcher la garde elle-même.

    Chaque source ne retenait qu'UNE ligne, « la plus équilibrée », calculée
    sur son propre carnet. Rien n'oblige un book soft à équilibrer sa cote sur
    le même handicap qu'un exchange : les deux choix tombaient à côté l'un de
    l'autre et `_meme_ligne` refusait — à raison, mais sur une divergence
    fabriquée en amont, alors que la ligne du sharp était cotée chez le soft
    aussi. Mesuré le 2026-08-27 : 1 total sur 2 et 0 spread sur 2 passaient.
    """

    @staticmethod
    def _aligner(soft, sharp, marche="spreads"):
        return run_engine._aligner_sur_meme_ligne(soft, sharp, marche,
                                                  "A vs B", "⚽", log)

    def test_la_ligne_du_sharp_est_reprise_chez_le_soft(self):
        soft = {"home": 2.05, "away": 1.85, "point": -0.75,
                "ladder": [{"home": 2.05, "away": 1.85, "point": -0.75, "away_point": 0.75},
                           {"home": 2.30, "away": 1.68, "point": -1.0,  "away_point": 1.0}]}
        sharp = {"home": 1.90, "away": 1.95, "point": -1.0,
                 "ladder": [{"home": 1.90, "away": 1.95, "point": -1.0, "away_point": 1.0}]}
        s2, p2 = self._aligner(soft, sharp)
        assert run_engine._meme_ligne(s2, p2, "spreads", "A vs B", "⚽", log) == -1.0
        assert (s2["home"], s2["away"]) == (2.30, 1.68)   # le PRIX suit la ligne
        assert s2["away_point"] == 1.0

    def test_sans_ligne_commune_la_garde_refuse_toujours(self):
        soft = {"point": -0.75, "ladder": [{"home": 2.05, "away": 1.85, "point": -0.75}]}
        sharp = {"point": -1.5, "ladder": [{"home": 1.90, "away": 1.95, "point": -1.5}]}
        s2, p2 = self._aligner(soft, sharp)
        assert (s2["point"], p2["point"]) == (-0.75, -1.5)
        assert run_engine._meme_ligne(s2, p2, "spreads", "A vs B", "⚽", log) is None

    def test_sans_echelle_rien_ne_change(self):
        """Une source qui ne publie pas son carnet n'est pas pénalisée — elle
        est simplement dans l'état d'avant."""
        soft, sharp = {"point": 0.0}, {"point": -1.0}
        s2, p2 = self._aligner(soft, sharp)
        assert (s2, p2) == (soft, sharp)

    def test_on_ne_choisit_JAMAIS_la_ligne_la_mieux_payee(self):
        """Parcourir une échelle en retenant la ligne au plus gros edge, c'est
        retenir la plus grosse erreur de cote — la queue positive qu'A6 a
        identifiée comme un artefact. On vise la ligne du SHARP."""
        soft = {"home": 2.00, "away": 1.90, "point": -0.5,
                "ladder": [{"home": 2.00, "away": 1.90, "point": -0.5},
                           {"home": 1.95, "away": 1.95, "point": -1.0},
                           {"home": 9.00, "away": 1.05, "point": -3.0}]}
        sharp = {"home": 1.90, "away": 1.95, "point": -1.0,
                 "ladder": [{"home": 1.90, "away": 1.95, "point": -1.0},
                            {"home": 5.20, "away": 1.15, "point": -3.0}]}
        s2, p2 = self._aligner(soft, sharp)
        assert s2["point"] == -1.0 and p2["point"] == -1.0   # pas la -3.0, mieux payée

    def test_le_signe_n_est_pas_perdu_dans_l_echelle(self):
        """+0.5 et -0.5 sont les handicaps OPPOSÉS. Une échelle qui les
        confond réintroduit le troisième défaut d'A6, un cran plus bas."""
        soft = {"point": 0.5, "ladder": [{"home": 1.70, "away": 2.25, "point": 0.5}]}
        sharp = {"point": -0.5, "ladder": [{"home": 2.20, "away": 1.72, "point": -0.5}]}
        s2, p2 = self._aligner(soft, sharp)
        assert run_engine._meme_ligne(s2, p2, "spreads", "A vs B", "⚽", log) is None

    def test_les_deux_marches_passent_par_l_alignement(self):
        import inspect
        for fonction in (run_engine._process_totals, run_engine._process_spreads):
            src = inspect.getsource(fonction)
            assert "_aligner_sur_meme_ligne(" in src, fonction.__name__


class TestUnSpreadNemetPlusSurUneLigneDifferente:
    """Bout en bout : le marché entier est refusé, pas seulement un côté."""

    @staticmethod
    def _match(soft_pt, sharp_pt):
        return {
            "id": "m1", "commence_time": (_now() + timedelta(hours=4)).isoformat(),
            "spreads_1xbet":    {"home": 2.05, "away": 1.85, "point": soft_pt},
            "spreads_pinnacle": {"home": 1.90, "away": 1.95, "point": sharp_pt},
        }

    def _lancer(self, m):
        out = []
        run_engine._process_spreads(m, "A vs B", "soccer", "L1", "A", "B", "⚽",
                                    out, None, _now(), log, min_edge=1.0)
        return out

    def test_lignes_differentes_nemettent_rien(self):
        assert self._lancer(self._match(0.0, -1.5)) == []
        assert self._lancer(self._match(-1.0, 1.0)) == []

    def test_le_temoin_avec_la_meme_ligne_traverse_la_garde(self):
        """Sans lui, le test ci-dessus passerait même si `_process_spreads`
        refusait TOUT."""
        m = self._match(-1.0, -1.0)
        assert run_engine._meme_ligne(m["spreads_1xbet"], m["spreads_pinnacle"],
                                      "spreads", "A vs B", "⚽", log) == -1.0
