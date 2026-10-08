"""Deux exchanges, un match : le comblement ne doit ni le rendre introuvable,
ni cacher la moneyline derrière un 1X2 de temps réglementaire.

Mesuré le 2026-10-08 (`ops.py chaine soccer 30`, `chaine hockey 30`) : sur 62
matchs de foot d'odds-api.io, 26 trouvaient un prix sharp, 35 après correctif ;
au hockey 12 → 14 sur 23. Voir core/exchange_match, « Comblement »."""
import pytest

from core.exchange_match import combler, lookup_exchange
from core.paim_engine import strict_team_match


def _ligne(home, away, un, deux, nul=0.0, heure="2030-01-01T18:00:00Z", source="matchbook"):
    return {"home": home, "away": away, "match": f"{home} vs {away}", "1": un, "X": nul,
            "2": deux, "commence_time": heure, "_source": source}


def _prix(*lignes):
    return {f"{r['home'].lower()}_{r['away'].lower()}": r for r in lignes}


class TestUnMatchDeuxOrthographes:
    def test_le_meme_match_chez_deux_exchanges_reste_trouvable(self):
        prix = _prix(_ligne("Santos FC", "Flamengo", 4.65, 1.85, 4.05))
        combler(prix, _prix(_ligne("Santos", "Flamengo", 4.70, 1.84, 4.05, source="smarkets")))
        hit = lookup_exchange({"home": "Santos FC SP", "away": "CR Flamengo RJ"}, prix)
        assert hit and hit["_source"] == "matchbook", "le premier exchange garde la main"

    def test_les_deux_orthographes_sont_gardees(self):
        """« EC Bahia BA » ne s'apparie qu'à l'écriture de Smarkets : retirer
        le doublon perdait Palmeiras–Bahia."""
        prix = _prix(_ligne("Palmeiras", "Esporte Clube Bahia", 1.62, 5.95, 4.55))
        combler(prix, _prix(_ligne("Palmeiras", "Bahia", 1.63, 5.90, 4.55, source="smarkets")))
        assert len(prix) == 2
        assert lookup_exchange({"home": "SE Palmeiras SP", "away": "EC Bahia BA"}, prix)

    def test_un_doublon_n_est_pas_un_match_nouveau(self):
        prix = _prix(_ligne("RC Lens", "Olympique Lyonnais", 2.51, 2.89, 3.92))
        assert combler(prix, _prix(_ligne("RC Lens", "Lyon", 2.50, 2.88, 3.90))) == 0
        assert combler(prix, _prix(_ligne("Moreirense FC", "Gil Vicente", 3.5, 2.4, 3.4))) == 1

    def test_deux_matchs_differents_restent_une_ambiguite(self):
        prix = _prix(_ligne("Atletico Nacional", "Junior", 1.9, 4.0, 3.4),
                     _ligne("Atletico Tucuman", "Junior FC", 2.1, 3.5, 3.2))
        assert lookup_exchange({"home": "Atletico", "away": "Junior"}, prix) is None

    def test_la_meme_affiche_un_autre_jour_reste_une_ambiguite(self):
        """Série de baseball : même affiche, deux soirs — on ne choisit pas."""
        prix = _prix(_ligne("NY Yankees", "Boston Red Sox", 1.8, 2.1))
        prix["b"] = _ligne("New York Yankees", "Boston Red Sox", 1.9, 2.0,
                           heure="2030-01-02T18:00:00Z", source="smarkets")
        assert lookup_exchange({"home": "Yankees", "away": "Red Sox"}, prix) is None

    def test_deux_lignes_du_meme_exchange_restent_une_ambiguite(self):
        prix = _prix(_ligne("Club Juventud Italiana", "Delfin SC", 5.5, 1.65, 4.1),
                     _ligne("Juventud Italiana FC", "Delfin SC", 5.9, 1.60, 4.0))
        assert lookup_exchange({"home": "Cde Juventud Italiana", "away": "Delfin SC"}, prix) is None


class TestLaMoneylineDerriereLe1X2:
    def test_a_cle_egale_la_moneyline_remplace_le_trois_issues(self):
        """Islanders–Blackhawks, 2026-10-08 : Matchbook 2.00 / 4.60 / 3.67
        (temps réglementaire), Smarkets 1.59 / 2.68 (moneyline)."""
        prix = _prix(_ligne("New York Islanders", "Chicago Blackhawks", 2.0, 3.675, 4.6))
        combler(prix, _prix(_ligne("New York Islanders", "Chicago Blackhawks", 1.59, 2.68,
                                   source="smarkets")))
        hit = lookup_exchange({"home": "New York Islanders", "away": "Chicago Blackhawks"}, prix)
        assert (hit["1"], hit["X"], hit["_source"]) == (1.59, 0.0, "smarkets")

    def test_a_cles_differentes_la_moneyline_est_preferee(self):
        prix = _prix(_ligne("NY Islanders", "Chicago Blackhawks", 2.0, 3.675, 4.6))
        combler(prix, _prix(_ligne("New York Islanders", "Chicago Blackhawks", 1.59, 2.68,
                                   source="smarkets")))
        hit = lookup_exchange({"home": "Islanders", "away": "Blackhawks"}, prix)
        assert hit["_source"] == "smarkets"

    def test_au_football_le_premier_exchange_reste(self):
        prix = _prix(_ligne("RC Lens", "Lyon", 2.51, 2.89, 3.92))
        combler(prix, _prix(_ligne("RC Lens", "Lyon", 2.40, 3.00, 3.90, source="smarkets")))
        assert prix["rc lens_lyon"]["_source"] == "matchbook"


class TestEcrituresVuesLe2026_10_08:
    @pytest.mark.parametrize("book,exchange", [
        ("Racing Club De Lens", "RC Lens"),
        ("Nancy-Lorraine", "AS Nancy"),
        ("CA Paranaense PR", "Athlético Paranaense"),
        ("Al Ahli Saudi FC", "Al Ahli Jeddah"),
    ])
    def test_le_nom_du_book_rejoint_celui_de_l_exchange(self, book, exchange):
        assert strict_team_match(book, exchange)

    @pytest.mark.parametrize("a,b", [
        ("Racing Club De Lens", "Racing Club de Strasbourg"),
        ("Racing Club", "RC Lens"),
        ("Al Ahli Saudi FC", "Al Ahli Doha"),
        ("CA Paranaense PR", "Paraná Clube"),
        ("Nancy-Lorraine", "FC Lorient"),
    ])
    def test_le_nom_entier_seulement(self, a, b):
        assert not strict_team_match(a, b)
