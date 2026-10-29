"""
tests/test_seuils_sans_cliquet.py — les seuils appris sans cliquet (2026-10-29).

INCIDENTS.md, « Le tennis gagnait 70 % et son seuil montait : le cliquet des
seuils et la dérive biaisée (2026-10-29) ». Mesuré ce jour-là sur le ledger
post-époque : le moteur annonçait 58,4 % et réalisait 58,7 % (z +0,14, n=436),
mais `threshold_tennis` était passé de 2,0 à 6,0 pendant que le tennis
gagnait 70 % (z +1,45), et les handicaps foot à 6,0. Deux causes :

1. le pas de +0,4 dès que la réussite cumulée passait sous le point mort,
   rejoué à CHAQUE ligne neuve — simulé, un sport à +5 % d'edge réel finit
   au plafond dans 51 % des cas en 60 paris ;
2. la dérive sharp brute, biaisée de −0,64 % (t −2,7) alors que les
   résultats collaient au prix d'entrée, qui décidait seule d'une montée.
"""
import core.learning_layer as ll
from test_incident_2026_09_15 import _SB


def _r(outcome, p=0.55, odds=1.90, minutes=600, close=None, created="2026-10-01T12:00:00+00:00"):
    return {"signal_id": None, "outcome": outcome, "kelly_pct": 1.0, "odds": odds,
            "market_type": "totals_over", "initial_edge": 3.0, "sharp_prob": p,
            "clv_pct_real": None, "closing_pinnacle_price": close,
            "closing_source": "exchange" if close else None,
            "time_to_match_minutes": minutes, "created_at": created}


def _serie(gagnes, n, **kw):
    return [_r("WIN", **kw) for _ in range(gagnes)] + [_r("LOSS", **kw) for _ in range(n - gagnes)]


class TestPasDeCliquet:
    def test_un_sport_calibre_ne_quitte_jamais_son_defaut(self):
        """Début à 9/20 (45 %, sous le point mort de 52,6 %) puis réussite
        conforme à l'annonce : l'ancienne règle relevait de +0,4 à chaque
        ligne tant que le cumul restait sous le point mort. Aucune perte
        n'est prouvée : rien ne doit être posé, à aucun moment."""
        rows = _serie(9, 20)
        store: dict = {}
        sb = _SB({"tennis": rows}, store)
        motif = ["WIN", "LOSS", "WIN", "WIN", "LOSS", "WIN", "LOSS", "WIN", "LOSS", "WIN"]
        for i in range(60):
            ll.compute_and_save(sb)
            assert "threshold_tennis" not in store, f"seuil posé après {len(rows)} lignes"
            rows.append(_r(motif[i % len(motif)]))

    def test_meme_preuve_meme_seuil_quel_que_soit_le_point_de_depart(self):
        """Sans mémoire : partir d'un seuil hérité à 6,0 ou de rien mène au
        même endroit dès que le mouvement est permis."""
        rows = _serie(10, 40)                    # perte prouvée
        a, b = {}, {"threshold_soccer": "3.4"}
        ll.compute_and_save(_SB({"soccer": list(rows)}, a))
        ll.compute_and_save(_SB({"soccer": list(rows)}, b))
        assert a["threshold_soccer"] == b["threshold_soccer"] == str(ll._THRESHOLD_MAX)


class TestSeuilsHerites:
    def test_le_cas_tennis_est_reevalue_au_premier_audit(self):
        """`threshold_tennis=6.0` et `threshold_seg_tennis_totals=6.0`, posés
        par l'ancienne règle (base dans `learning_bases`, aucune en v2) :
        rien ne les prouve, ils sont retirés tout de suite."""
        store = {"threshold_tennis": "6.0", "threshold_seg_tennis_totals": "6.0",
                 "learning_bases": '{"threshold_tennis": "34:62b8842d2812f9c8"}'}
        ll.compute_and_save(_SB({"tennis": _serie(26, 37, p=0.59, odds=1.82)}, store))
        assert "threshold_tennis" not in store
        assert "threshold_seg_tennis_totals" not in store
        assert ll._BASES_KEY == "learning_bases_v2" and ll._BASES_KEY in store

    def test_le_mouvement_attend_les_lignes_neuves(self):
        bases = {"threshold_x": "30:abc"}
        assert ll._mouvement_permis({}, "threshold_x", 30) is True
        assert ll._mouvement_permis(bases, "threshold_x", 30 + ll._LIGNES_NEUVES_MIN - 1) is False
        assert ll._mouvement_permis(bases, "threshold_x", 30 + ll._LIGNES_NEUVES_MIN) is True
        assert ll._mouvement_permis(bases, "threshold_x", 25) is True    # échantillon rétréci
        assert ll._mouvement_permis({"threshold_x": "illisible"}, "threshold_x", 30) is True


class TestDeriveExcedentaire:
    def test_un_biais_commun_n_est_pas_un_signal(self):
        """Toutes les dérives à −1,45 % (le tennis), référence commune
        −1,45 % : l'excédent est nul."""
        lignes = _serie(26, 37, p=0.5, close=2.0295)
        brute = ll._derive_stats(lignes)["avg_clv"]
        assert round(brute, 2) == -1.45
        dx = ll._derive_excedent(lignes, brute)
        assert abs(dx["excedent"]) < 1e-9

    def test_la_derive_seule_ne_monte_jamais_un_seuil(self):
        """Le cas du 26/10 : tennis à 70 %, dérive −1,45 % sur 35 lignes,
        référence −0,62 %. L'ancienne règle relevait « sans attendre le
        win-rate »."""
        lignes = _serie(26, 37, p=0.59, odds=1.82, close=1.72)
        verdict, _ = ll._verdict_seuil(lignes, [], -0.62)
        assert verdict != "perte"

    def test_un_excedent_significatif_freine_un_abaissement(self):
        gagnant = _serie(30, 40, p=0.5, odds=2.2)         # gain prouvé
        assert ll._verdict_seuil(gagnant, [], None)[0] == "gain"
        # Même résultat, mais chaque clôture s'éloigne de 5 % de plus que
        # le reste du ledger, avec dispersion : le marché ne confirme pas.
        fuyant = [dict(r, closing_pinnacle_price=2.0 * (1.05 + 0.01 * (i % 3)),
                       closing_source="exchange") for i, r in enumerate(gagnant)]
        verdict, raison = ll._verdict_seuil(fuyant, [], 0.0)
        assert verdict == "neutre" and "dérive excédentaire" in raison

    def test_la_reference_est_commune_a_tous_les_sports(self):
        import inspect
        src = inspect.getsource(ll.compute_and_save)
        assert "derive_ref" in src and "_derive_valeurs(toutes)" in src


class TestContreEpreuveFantomes:
    def test_des_fantomes_contraires_annulent_le_motif(self):
        zone = _serie(10, 30, p=0.6)                       # z ≈ −2,8
        assert ll._verdict_seuil(zone, [], None)[0] == "perte"
        fantomes = _serie(12, 15, p=0.6, minutes=30)       # z > 0
        verdict, raison = ll._verdict_seuil(zone, fantomes, None)
        assert verdict == "neutre" and "fantômes contredisent" in raison

    def test_des_fantomes_concordants_s_ajoutent_a_la_preuve(self):
        # Zone seule : z ≈ −1,7, rien de prouvé. Avec des fantômes dans le
        # même sens, la preuve commune passe −1,96.
        zone = _serie(13, 30, p=0.58)
        assert ll._verdict_seuil(zone, [], None)[0] == "neutre"
        fantomes = _serie(5, 15, p=0.58, minutes=30)
        assert ll._verdict_seuil(zone, fantomes, None)[0] == "perte"

    def test_trop_peu_de_fantomes_ne_contredisent_rien(self):
        zone = _serie(10, 30, p=0.6)
        fantomes = _serie(5, 5, p=0.6, minutes=30)
        assert ll._verdict_seuil(zone, fantomes, None)[0] == "perte"

    def test_compute_and_save_passe_les_fantomes_du_sport(self):
        """Bout en bout : la même zone perdante, des fantômes gagnants du même
        sport dans le ledger → aucun seuil posé."""
        rows = _serie(10, 30, p=0.6) + _serie(14, 15, p=0.6, minutes=30)
        store: dict = {}
        ll.compute_and_save(_SB({"soccer": rows}, store))
        assert "threshold_soccer" not in store
