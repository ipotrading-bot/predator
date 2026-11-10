"""Gardiens : le plafond du Tier 2 se pose APRÈS le périmètre (2026-11-10).

Mesuré sur les quatre scans standard du 2026-11-10 : 123 à 141 matchs du
Tier 2 avec prix sharp, 50 retenus par la coupe, puis 22 à 28 seulement
réglables. Challengers, AHL, Eurocup et FIBA prenaient la moitié des places
avant d'être écartés « NON RÉGLABLE » — et des matchs réglables plus tardifs
(Musetti–Samuel, Kostyuk–Pegula au scan de 13:10) n'étaient jamais regardés.
"""
import inspect
import logging

import run_engine as eng

LOG = logging.getLogger("test")


def _m(sport: str, i: int, **kw) -> dict:
    return {"sport": sport, "match": f"{sport} {i}", **kw}


class TestPlafonnerTier2:
    def test_le_tier_1_n_est_jamais_coupe(self):
        tier1 = [_m("soccer", i) for i in range(70)]
        tier2 = [_m("tennis", i) for i in range(60)]
        out = eng._plafonner_tier2(tier1 + tier2, tier2, 50, LOG)
        assert [m for m in out if m in tier1] == tier1
        assert sum(1 for m in out if m["sport"] == "tennis") == 50

    def test_sous_le_plafond_rien_ne_bouge(self):
        tier1 = [_m("basketball", i) for i in range(6)]
        tier2 = [_m("hockey", i) for i in range(7)] + [_m("tennis", i) for i in range(5)]
        matches = tier1 + tier2
        assert eng._plafonner_tier2(matches, tier2, 50, LOG) == matches

    def test_la_coupe_tourne_entre_les_sports_et_garde_l_ordre(self):
        tier2 = ([_m("soccer", i) for i in range(80)] + [_m("hockey", i) for i in range(7)]
                 + [_m("tennis", i) for i in range(5)])
        out = eng._plafonner_tier2(list(tier2), tier2, 50, LOG)
        assert len(out) == 50
        assert sum(1 for m in out if m["sport"] == "hockey") == 7
        assert sum(1 for m in out if m["sport"] == "tennis") == 5
        assert [m["match"] for m in out if m["sport"] == "soccer"] == [f"soccer {i}" for i in range(38)]

    def test_un_match_ecarte_par_le_perimetre_ne_prend_pas_de_place(self):
        """Le cas du 2026-11-10 : 13 tennis à prix sharp dont 10 Challengers
        non réglables en TÊTE de file (ils jouent plus tôt). Le filtre passe
        d'abord ; les 3 + 4 matchs réglables sont tous gardés."""
        challengers = [_m("tennis", i, league="Challenger") for i in range(10)]
        principal = [_m("tennis", 100 + i, league="ATP") for i in range(7)]
        foot = [_m("soccer", i) for i in range(60)]
        tier2 = challengers + principal + foot
        apres_perimetre = principal + foot            # ce que rend _filtrer_perimetre
        out = eng._plafonner_tier2(apres_perimetre, tier2, 50, LOG)
        assert [m for m in out if m["sport"] == "tennis"] == principal
        assert len(out) == 50

    def test_sans_tier_2_la_liste_est_rendue_telle_quelle(self):
        slate = [_m("soccer", i) for i in range(80)]          # REPRICE : slate du cache
        assert eng._plafonner_tier2(slate, [], 50, LOG) == slate

    def test_le_bilan_est_logge(self, caplog):
        tier2 = [_m("tennis", i) for i in range(4)]
        with caplog.at_level(logging.INFO):
            eng._plafonner_tier2(tier2[:2], tier2, 50, LOG)
        assert "4 match(s) avec prix sharp, 2 au périmètre, 2 retenu(s)" in caplog.text


class TestOrdreDansLeMoteur:
    def test_le_perimetre_precede_la_coupe(self):
        src = inspect.getsource(eng.run)
        assert src.index("_filtrer_perimetre(matches") < src.index("_plafonner_tier2(matches")

    def test_la_coupe_precede_la_photographie_du_slate(self):
        # Le slate de REPRICE reste borné : il est photographié APRÈS la coupe.
        src = inspect.getsource(eng.run)
        assert src.index("_plafonner_tier2(matches") < src.index('_set_cached(sb, "cache_soft_slate"')

    def test_plus_aucune_coupe_avant_le_perimetre(self):
        src = inspect.getsource(eng.run)
        avant = src[:src.index("_filtrer_perimetre(matches")]
        assert "_repartir_par_sport(" not in avant
        assert "matches.extend(avec_sharp)" in avant
