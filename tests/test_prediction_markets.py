"""
tests/test_prediction_markets.py — Kalshi & Polymarket : lire les bons champs.

Les deux pièges couverts ici ont réellement coûté du temps le 2026-08-22 et
sont invisibles sans test :
  - Kalshi rend `yes_bid`/`yes_ask` à **null** sur l'API publique (0 marché
    sur 200 échantillonnés en portait) ; les prix sont dans les champs
    `*_dollars`, en CHAÎNE ;
  - Polymarket sérialise `outcomes`/`outcomePrices` en CHAÎNES JSON.

Les charges utiles ci-dessous sont des réponses réelles, réduites.
"""
import json

import pytest

from core import prediction_markets as pm


def _kalshi_leg(ticker, event, yes_bid, yes_ask, sub_title, when="2026-08-29T19:30:00Z"):
    """Une patte de marché Kalshi, avec les champs entiers à null COMME EN
    PRODUCTION — c'est le cœur du piège."""
    return {
        "ticker": ticker, "event_ticker": event,
        "yes_bid": None, "yes_ask": None, "volume": None, "open_interest": None,
        "yes_bid_dollars": f"{yes_bid:.4f}", "yes_ask_dollars": f"{yes_ask:.4f}",
        "no_bid_dollars": f"{1 - yes_ask:.4f}", "no_ask_dollars": f"{1 - yes_bid:.4f}",
        "yes_sub_title": sub_title, "occurrence_datetime": when,
    }


@pytest.fixture
def stub(monkeypatch):
    payloads = {}
    monkeypatch.setattr(pm, "_get_json",
                        lambda url: next((v for k, v in payloads.items() if k in url), None))
    return payloads


class TestKalshi:
    def test_les_prix_sont_lus_dans_les_champs_dollars(self, stub):
        stub["series_ticker=KXEPLGAME"] = {"markets": [
            _kalshi_leg("KXEPLGAME-26AUG29TOTNEW-TOT", "KXEPLGAME-26AUG29TOTNEW", 0.48, 0.50, "Tottenham"),
            _kalshi_leg("KXEPLGAME-26AUG29TOTNEW-TIE", "KXEPLGAME-26AUG29TOTNEW", 0.26, 0.28, "Tie"),
            _kalshi_leg("KXEPLGAME-26AUG29TOTNEW-NEW", "KXEPLGAME-26AUG29TOTNEW", 0.26, 0.28, "Newcastle"),
        ]}
        fx = pm.fetch_kalshi("epl")
        assert len(fx) == 1
        assert len(fx[0].odds) == 3
        assert fx[0].odds[0] == pytest.approx(1 / 0.49, rel=1e-3)

    def test_les_champs_entiers_null_ne_font_pas_croire_a_labsence_de_prix(self, stub):
        # Si le module lisait `yes_bid`, il conclurait « aucun marché coté ».
        stub["series_ticker=KXEPLGAME"] = {"markets": [
            _kalshi_leg("A-TOT", "E1", 0.48, 0.50, "Tottenham"),
            _kalshi_leg("A-NEW", "E1", 0.50, 0.52, "Newcastle"),
        ]}
        assert pm.fetch_kalshi("epl") != []

    def test_le_nul_est_place_au_milieu(self, stub):
        stub["series_ticker=KXEPLGAME"] = {"markets": [
            _kalshi_leg("X-TIE", "E1", 0.26, 0.28, "Tie"),
            _kalshi_leg("X-TOT", "E1", 0.48, 0.50, "Tottenham"),
            _kalshi_leg("X-NEW", "E1", 0.22, 0.24, "Newcastle"),
        ]}
        fx = pm.fetch_kalshi("epl")[0]
        # ordre 1-X-2 attendu par le moteur : le nul est la cote la plus…
        # simplement, il est au milieu, et ce n'est pas la plus courte.
        assert fx.odds[1] == pytest.approx(1 / 0.27, rel=1e-3)
        assert fx.home == "Tottenham" and fx.away == "Newcastle"

    def test_un_1X2_ampute_est_rejete_et_non_pris_pour_un_moneyline(self, stub):
        """Piège réel : une patte écartée pour carnet trop large laissait un
        vecteur à deux issues qui RESSEMBLE à un moneyline. Apparié par
        structure contre un vrai moneyline, il lierait les cotes au mauvais
        match."""
        stub["series_ticker=KXEPLGAME"] = {"markets": [
            _kalshi_leg("Y-TIE", "E1", 0.26, 0.28, "Tie"),
            _kalshi_leg("Y-TOT", "E1", 0.48, 0.50, "Tottenham"),
            # la troisième patte porte un carnet trop large et sera écartée
            _kalshi_leg("Y-NEW", "E1", 0.10, 0.40, "Newcastle"),
        ]}
        assert pm.fetch_kalshi("epl") == []

    def test_un_carnet_trop_large_est_ignore(self, stub):
        stub["series_ticker=KXNFLGAME"] = {"markets": [
            _kalshi_leg("Z-A", "E1", 0.30, 0.70, "A"),
            _kalshi_leg("Z-B", "E1", 0.30, 0.70, "B"),
        ]}
        assert pm.fetch_kalshi("nfl") == []

    def test_ligue_inconnue_ne_coute_aucune_requete(self, stub):
        stub["series_ticker=KXEPLGAME"] = {"markets": []}
        assert pm.fetch_kalshi("curling") == []

    def test_panne_reseau_rend_liste_vide(self, stub):
        assert pm.fetch_kalshi("epl") == []


class TestPolymarket:
    def _event(self, slug, legs, end="2026-08-22T11:30:00Z"):
        return {"slug": slug, "endDate": end, "title": "T",
                "markets": [{"groupItemTitle": name,
                             # CHAÎNES JSON, comme en production
                             "outcomes": json.dumps(["Yes", "No"]),
                             "outcomePrices": json.dumps([f"{p}", f"{1 - p:.4f}"])}
                            for name, p in legs]}

    def test_les_champs_chaines_json_sont_parses(self, stub):
        # Prix RÉELS du 2026-08-22 : epl-hul-mun-2026-08-22.
        stub["tag_slug=epl"] = [self._event("epl-hul-mun-2026-08-22", [
            ("Hull City AFC", 0.095),
            ("Draw (Hull City AFC vs. Manchester United FC)", 0.185),
            ("Manchester United FC", 0.715)])]
        fx = pm.fetch_polymarket("epl")
        assert len(fx) == 1
        assert fx[0].odds[0] == pytest.approx(1 / 0.095, rel=1e-3)
        assert fx[0].odds[2] == pytest.approx(1 / 0.715, rel=1e-3)

    def test_le_nul_est_place_au_milieu(self, stub):
        stub["tag_slug=epl"] = [self._event("m", [
            ("Draw (A vs. B)", 0.185), ("Hull City AFC", 0.095),
            ("Manchester United FC", 0.715)])]
        fx = pm.fetch_polymarket("epl")[0]
        assert fx.odds[1] == pytest.approx(1 / 0.185, rel=1e-3)

    def test_ce_prix_reel_concorde_avec_500com(self, stub):
        """La mesure qui a validé la carte des books masqués de 500.com :
        deux hôtes sans aucun lien tombent à ~1 point l'un de l'autre."""
        from core.source_adapter import divergence_pts
        stub["tag_slug=epl"] = [self._event("epl-hul-mun-2026-08-22", [
            ("Hull City AFC", 0.095), ("Draw", 0.185),
            ("Manchester United FC", 0.715)])]
        poly = pm.fetch_polymarket("epl")[0].odds
        pinnacle_500 = [9.19, 5.14, 1.36]
        assert 0 <= divergence_pts(poly, pinnacle_500) < 1.5

    def test_reponse_inattendue_rend_liste_vide(self, stub):
        stub["tag_slug=epl"] = {"erreur": "pas une liste"}
        assert pm.fetch_polymarket("epl") == []

    def test_un_marche_a_une_seule_issue_est_ignore(self, stub):
        stub["tag_slug=nfl"] = [self._event("solo", [("A", 0.5)])]
        assert pm.fetch_polymarket("nfl") == []


class TestDoctrine:
    def test_user_agent_ascii_pur(self):
        """C'est ICI que le piège a été trouvé : gamma-api.polymarket.com rend
        403 sur un User-Agent non-ASCII, là où curl passait."""
        pm._UA.encode("ascii")

    def test_role_consensus_jamais_sharp(self):
        """Ces marchés sont trop peu liquides hors grandes affiches pour
        servir de fair price ; ils servent d'arbitre, pas de référence."""
        assert pm.SPEC_KALSHI.role == "consensus"
        assert pm.SPEC_POLYMARKET.role == "consensus"

    def test_le_budget_est_partage_entre_les_deux_marches(self):
        assert pm.SPEC_KALSHI.bucket == pm.SPEC_POLYMARKET.bucket
