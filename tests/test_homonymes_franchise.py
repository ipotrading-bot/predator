"""Gardien — un SURNOM ou une VILLE seuls ne désignent pas une franchise.

Incident du 2026-11-11 : « South Florida Bulls vs Saint Josephs Hawks »
(basket universitaire) émis en recommandé parce que le scoreboard NBA
listait « Atlanta Hawks at Chicago Bulls » et qu'ESPN publie le surnom nu
de chaque franchise (`name` = « Bulls »). Signal 12558 jamais réglable,
quatre audits de suite en échec. Voir INCIDENTS.md et `core.score_sources._meme_equipe`.

Les compétiteurs de ce fichier ont la FORME RÉELLE de la réponse ESPN
(displayName, shortDisplayName, name, location) : les faux événements à
`displayName` seul des autres tests ne pouvaient pas voir cette panne.
"""
import run_engine as eng
from core import score_sources as s


def _franchise(ville, surnom):
    return {"team": {"displayName": f"{ville} {surnom}", "shortDisplayName": surnom,
                     "name": surnom, "location": ville}}


def _club(nom, court=None):
    """Un club de football chez ESPN : le nom entier répété partout."""
    return {"team": {"displayName": nom, "shortDisplayName": court or nom,
                     "name": nom, "location": nom}}


def _ev(dom, ext):
    return {"competitions": [{"competitors": [dict(dom, homeAway="home"),
                                              dict(ext, homeAway="away")],
                              "status": {"type": {"state": "pre", "completed": False}}}]}


def _m(match, sport, league="L"):
    home, away = match.split(" vs ")
    return {"match": match, "home": home, "away": away, "sport": sport, "league": league}


NBA = {"basketball": [_ev(_franchise("Chicago", "Bulls"), _franchise("Atlanta", "Hawks")),
                      _ev(_franchise("LA", "Clippers"), _franchise("Los Angeles", "Lakers"))]}


class TestLHomonymeNEstPasUneCouverture:
    def test_le_match_universitaire_du_11_novembre_est_refuse(self):
        m = _m("South Florida Bulls vs Saint Josephs Hawks", "basketball", "USA - NCAA")
        assert eng._reglable(m, NBA) is False

    def test_il_ne_se_regle_pas_non_plus_sur_le_score_de_la_nba(self):
        comp = NBA["basketball"][0]["competitions"][0]
        assert s._espn_paire(comp, "South Florida Bulls", "Saint Josephs Hawks") is None

    def test_l_ahl_ne_se_regle_pas_sur_la_nhl(self):
        """AHL admise via LiveScore le 2026-11-10 : mêmes surnoms que la NHL."""
        comp = _ev(_franchise("Dallas", "Stars"), _franchise("Minnesota", "Wild"))["competitions"][0]
        assert s._espn_paire(comp, "Texas Stars", "Iowa Wild") is None
        assert s._espn_paire(comp, "Dallas Stars", "Minnesota Wild") is not None

    def test_deux_franchises_de_la_meme_ville(self):
        jets = _franchise("New York", "Jets")
        assert s._meme_equipe("New York Giants", jets) is False
        assert s._meme_equipe("New York Jets", jets) is True

    def test_une_ville_sigle_n_attrape_plus_un_nom_qui_la_contient(self):
        """« la » est une sous-chaîne d'« atlanta » et de « lakers »."""
        clippers = _franchise("LA", "Clippers")
        assert s._meme_equipe("Atlanta Hawks", clippers) is False
        assert s._meme_equipe("Los Angeles Lakers", clippers) is False


class TestLesVraisMatchsPassent:
    def test_le_vrai_match_nba(self):
        assert eng._reglable(_m("Chicago Bulls vs Atlanta Hawks", "basketball", "NBA"), NBA) is True

    def test_les_ecritures_d_une_meme_franchise(self):
        cas = [("Los Angeles Clippers", _franchise("LA", "Clippers")),
               ("LA Clippers", _franchise("LA", "Clippers")),
               ("NY Knicks", _franchise("New York", "Knicks")),
               ("St Louis Blues", _franchise("St. Louis", "Blues")),
               ("Montreal Canadiens", _franchise("Montréal", "Canadiens")),
               ("Las Vegas Golden Knights", _franchise("Vegas", "Golden Knights")),
               ("Bulls", _franchise("Chicago", "Bulls"))]
        for nom, competiteur in cas:
            for restes in (True, False):
                assert s._meme_equipe(nom, competiteur, restes) is True, (nom, restes)


class TestLeFootballEtLesAthletesNeChangentPas:
    def test_un_club_n_est_pas_une_franchise(self):
        assert s._franchise(_club("FC Cologne", "Cologne")) is None
        assert s._franchise({"athlete": {"fullName": "Jannik Sinner"}}) is None

    def test_le_football_reste_tolerant_sur_un_camp(self):
        fx = {"soccer": [_ev(_club("VfB Stuttgart"), _club("FC Cologne", "Cologne"))]}
        assert eng._reglable(_m("VfB Stuttgart vs 1. FC Köln", "soccer"), fx) is True

    def test_un_athlete_s_apparie_comme_avant(self):
        sinner = {"athlete": {"fullName": "Jannik Sinner", "displayName": "Jannik Sinner",
                              "shortName": "J. Sinner"}}
        assert s._meme_equipe("Jannik Sinner", sinner) is True


class TestContredit:
    def test_un_reste_plein_different_contredit(self):
        assert s._contredit("South Florida Bulls", "Bulls", "Chicago Bulls") is True
        assert s._contredit("Texas Stars", "Stars", "Dallas Stars") is True

    def test_un_sigle_ou_rien_ne_contredit_pas(self):
        assert s._contredit("LA Clippers", "Clippers", "Los Angeles Clippers") is False
        assert s._contredit("Bulls", "Bulls", "Chicago Bulls") is False

    def test_la_meme_ville_ecrite_autrement_ne_contredit_pas(self):
        assert s._contredit("St Louis Blues", "Blues", "St. Louis Blues") is False
        assert s._contredit("Montreal Canadiens", "Canadiens", "Montréal Canadiens") is False
