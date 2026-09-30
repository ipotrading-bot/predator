"""Gardien — le même pari annoncé deux fois par deux sources (2026-09-30).

Toronto–Montréal Over 6.5 est sorti recommandé deux fois le 29/09 : une fois
par OddsAPI (« Montréal », 23:10, uuid), une fois par odds-api.io
(« Montreal », 23:00, `oai_…`). Toutes les gardes d'émission comparaient le
match_id EXACT. INCIDENTS.md, « Le même pari annoncé deux fois ».

L'appariement flou avait été rejeté le 2026-09-02 pour ses faux positifs :
ils sont rejoués ici un par un, et doivent rester refusés.
"""
import logging

import pytest

from run_engine import _meme_match_reel, _sans_contradiction, _actifs_des_matchs

log = logging.getLogger("test")


def _s(match, mt, mid, sport="soccer", mk="totals_over", sel="Over 2.5"):
    return {"match": match, "match_time": mt, "match_id": mid, "sport": sport,
            "market_key": mk, "selection_name": sel}


# Les 18 paires trouvées en rejouant `_meme_match_reel` sur les 677 signaux
# en base le 2026-09-30 : toutes sont le même match réel (même heure, mêmes
# issues quand le pari est le même). Aucune autre paire n'est sortie.
_PAIRES_REELLES = [
    ("Tepatitlan de Morelos vs Tlaxcala", "Tepatitlan FC vs Tlaxcala FC", "2026-08-21T01:00:00+00:00", None),
    ("Acassuso vs San Telmo", "CA Acassuso vs CA San Telmo", "2026-08-26T18:00:00+00:00", None),
    ("Lyon vs AJ Auxerre", "Olympique Lyon vs AJ Auxerre", "2026-09-04T17:00:00+00:00", None),
    ("Fulham vs Crystal Palace", "Fulham FC vs Crystal Palace", "2026-09-05T14:00:00+00:00", None),
    ("Brentford vs Sunderland A.F.C", "Brentford FC vs Sunderland AFC", "2026-09-05T14:00:00+00:00", None),
    ("Newcastle United vs AFC Bournemouth", "Newcastle United vs Bournemouth", "2026-09-05T11:30:00+00:00", None),
    ("Cagliari vs Lecce", "Cagliari Calcio vs US Lecce", "2026-09-07T16:30:00+00:00", None),
    ("Elche CF vs Real Sociedad", "Elche CF vs Real Sociedad San Sebastian", "2026-09-07T19:30:00+00:00", None),
    ("Como vs RB Leipzig", "Como 1907 vs RB Leipzig", "2026-09-10T19:00:00+00:00", None),
    ("Le Havre vs Angers", "Le Havre AC vs Angers SCO", "2026-09-12T18:45:00+00:00", None),
    ("Como 1907 vs Parma Calcio", "Como vs Parma", "2026-09-14T16:30:00+00:00", None),
    ("Olympique Marseille vs Paris Saint-Germain", "Marseille vs Paris Saint Germain", "2026-09-20T18:45:00+00:00", None),
    ("Tigres UANL vs Club Puebla", "Tigres vs Puebla", "2026-09-27T03:10:00+00:00", None),
    ("Toronto Maple Leafs vs Montréal Canadiens", "Toronto Maple Leafs vs Montreal Canadiens",
     "2026-09-29T23:10:00+00:00", "2026-09-29T23:00:00+00:00"),
]


class TestLeMemeMatchReel:
    @pytest.mark.parametrize("a,b,mt,mt_b", _PAIRES_REELLES)
    def test_les_jumeaux_reels_sont_apparies(self, a, b, mt, mt_b):
        assert _meme_match_reel(_s(a, mt, "id1"), _s(b, mt_b or mt, "id2"))

    def test_meme_match_id_suffit(self):
        assert _meme_match_reel(_s("A vs B", None, "x"), _s("C vs D", None, "x"))

    def test_domicile_et_exterieur_inverses(self):
        assert _meme_match_reel(_s("Mexico vs Peru", "2026-09-30T01:00:00+00:00", "a"),
                                _s("Peru vs Mexico", "2026-09-30T01:00:00+00:00", "b"))

    # ── Les faux positifs qui avaient fait rejeter le flou le 2026-09-02 ──
    @pytest.mark.parametrize("a,b", [
        ("Green Gully U23 vs Altona Magic U23", "Green Gully vs Altona Magic"),
        ("Kocaelispor U19 vs Sakaryaspor U19", "Kocaelispor vs Sakaryaspor"),
        ("Atletico Junior Barranquilla vs Deportiva Once Caldas",
         "Atletico Nacional vs Deportivo Cali"),
        ("Real Valladolid Juvenil vs Real Sociedad Juvenil", "Real Valladolid vs Real Oviedo"),
    ])
    def test_les_faux_positifs_de_septembre_restent_refuses(self, a, b):
        mt = "2026-09-02T18:00:00+00:00"
        assert not _meme_match_reel(_s(a, mt, "a"), _s(b, mt, "b"))

    def test_coup_d_envoi_trop_eloigne(self):
        """Un doubleheader est à des heures d'écart : deux matchs."""
        assert not _meme_match_reel(
            _s("Yankees vs Red Sox", "2026-09-02T17:00:00+00:00", "a", "baseball"),
            _s("Yankees vs Red Sox", "2026-09-02T17:31:00+00:00", "b", "baseball"))

    def test_coup_d_envoi_inconnu_fait_refuser(self):
        assert not _meme_match_reel(_s("Lyon vs Auxerre", None, "a"),
                                    _s("Lyon vs Auxerre", "2026-09-04T17:00:00+00:00", "b"))

    def test_sports_differents(self):
        mt = "2026-09-04T17:00:00+00:00"
        assert not _meme_match_reel(_s("Lyon vs Auxerre", mt, "a", "soccer"),
                                    _s("Lyon vs Auxerre", mt, "b", "basketball"))

    def test_nom_vide_ou_sans_vs_fait_refuser(self):
        mt = "2026-09-04T17:00:00+00:00"
        assert not _meme_match_reel(_s(" vs Auxerre", mt, "a"), _s("Lyon vs Auxerre", mt, "b"))
        assert not _meme_match_reel(_s("Lyon", mt, "a"), _s("Lyon vs Auxerre", mt, "b"))


_TOR = "Toronto Maple Leafs vs Montréal Canadiens"
_TOR2 = "Toronto Maple Leafs vs Montreal Canadiens"


class TestLaGardeRefuseLeJumeau:
    def test_le_cas_toronto_d_un_scan_a_l_autre(self):
        actif = _s(_TOR, "2026-09-29T23:10:00+00:00", "485b2953", "hockey", "totals_over", "Over 6.5")
        jumeau = _s(_TOR2, "2026-09-29T23:00:00+00:00", "oai_72886404", "hockey", "totals_over", "Over 6.5")
        assert _sans_contradiction([jumeau], [actif], log) == []

    def test_le_jumeau_dans_le_meme_scan(self):
        a = _s(_TOR, "2026-09-29T23:10:00+00:00", "485b2953", "hockey", "totals_over", "Over 6.5")
        b = _s(_TOR2, "2026-09-29T23:00:00+00:00", "oai_72886404", "hockey", "totals_over", "Over 6.5")
        assert _sans_contradiction([a, b], [], log) == [a]

    def test_mexique_perou_handicap_puis_h2h_du_meme_camp(self):
        mt = "2026-09-30T01:00:00+00:00"
        actif = _s("Mexico vs Peru", mt, "oai_73685558", mk="spreads_home", sel="Mexico -1.0")
        jumeau = _s("Mexico vs Peru", mt, "t7_3094999", mk="h2h", sel="Mexico")
        assert _sans_contradiction([jumeau], [actif], log) == []

    def test_total_et_cote_d_un_jumeau_coexistent(self):
        """Real Sociedad–Celta, 03/09 : AH 0.0 d'une source, Over 2.5 de
        l'autre. Deux familles, deux paris : la garde ne supprime rien."""
        mt = "2026-09-03T19:00:00+00:00"
        actif = _s("Real Sociedad vs Celta Vigo", mt, "a", mk="h2h", sel="Real Sociedad")
        total = _s("Real Sociedad vs Celta Vigo", mt, "b", mk="totals_over", sel="Over 2.5")
        assert _sans_contradiction([total], [actif], log) == [total]

    def test_le_rafraichissement_de_la_meme_source_passe_toujours(self):
        actif = _s(_TOR, "2026-09-29T23:10:00+00:00", "485b2953", "hockey", "totals_over", "Over 6.5")
        revu = dict(actif)
        assert _sans_contradiction([revu], [actif], log) == [revu]


class _Requete:
    def __init__(self, journal, lignes):
        self.journal, self.lignes, self.filtres = journal, lignes, []

    def select(self, *_a):
        return self

    def eq(self, *a):
        self.filtres.append(("eq",) + a)
        return self

    def in_(self, *a):
        self.filtres.append(("in",) + a)
        return self

    def gte(self, *a):
        self.filtres.append(("gte",) + a)
        return self

    def lte(self, *a):
        self.filtres.append(("lte",) + a)
        return self

    def execute(self):
        self.journal.append(self.filtres)
        par_id = any(f[0] == "in" and f[1] == "match_id" for f in self.filtres)
        return type("R", (), {"data": self.lignes["id" if par_id else "fenetre"]})()


class _SB:
    def __init__(self, lignes):
        self.journal, self.lignes = [], lignes

    def table(self, _nom):
        return _Requete(self.journal, self.lignes)


class TestLaLectureDesActifs:
    def test_le_jumeau_d_un_autre_match_id_est_lu_par_la_fenetre(self):
        jumeau_actif = {"id": 1, "match_id": "485b2953", "match": _TOR, "sport": "hockey",
                        "match_time": "2026-09-29T23:10:00+00:00",
                        "market_key": "totals_over", "selection_name": "Over 6.5"}
        sb = _SB({"id": [], "fenetre": [jumeau_actif]})
        cand = _s(_TOR2, "2026-09-29T23:00:00+00:00", "oai_72886404", "hockey")
        assert _actifs_des_matchs(sb, [cand], log) == [jumeau_actif]
        fenetre = sb.journal[1]
        assert ("in", "sport", ["hockey"]) in fenetre
        assert ("gte", "match_time", "2026-09-29T22:30:00+00:00") in fenetre
        assert ("lte", "match_time", "2026-09-29T23:30:00+00:00") in fenetre

    def test_une_ligne_lue_deux_fois_n_est_comptee_qu_une(self):
        ligne = {"id": 7, "match_id": "x"}
        sb = _SB({"id": [ligne], "fenetre": [ligne]})
        cand = _s(_TOR2, "2026-09-29T23:00:00+00:00", "x", "hockey")
        assert _actifs_des_matchs(sb, [cand], log) == [ligne]
