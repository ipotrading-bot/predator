"""
tests/test_correction_domicile.py — biais domicile corrigé sur les marchés de
côté (décision opérateur du 2026-11-02, INCIDENTS.md « Le marché Euroleague
sous-estime le domicile »).

Gardiens :
  1. la fonction pure : +δ logit au domicile, −δ à l'extérieur, somme à 1,
     rien hors de la table ni sur un marché sans domicile ;
  2. le moteur : un handicap extérieur Euroleague « +2,8 % » brut n'est plus
     émis, le même prix en NBA l'est toujours, et chaque correction est loggée.
"""
import logging
from datetime import datetime, timedelta, timezone

import pytest

import run_engine as eng
from core.constants import CORRECTION_DOMICILE_LOGIT, EXECUTION_BOOKS
from core.execution_books import fusionner_lignes
from core.math_engine import corriger_domicile

log = logging.getLogger("test")
REF = EXECUTION_BOOKS[0]
EL = "euroleague_basketball"


class TestFonctionPure:
    def test_euroleague_plus_trois_points_a_cinquante_pour_cent(self):
        assert corriger_domicile(0.50, EL, "home") == pytest.approx(0.53, abs=0.001)
        assert corriger_domicile(0.50, EL, "away") == pytest.approx(0.47, abs=0.001)

    @pytest.mark.parametrize("p", [0.15, 0.35, 0.5, 0.62, 0.9])
    def test_les_deux_cotes_corriges_somment_a_un(self, p):
        assert corriger_domicile(p, EL, "home") + corriger_domicile(1 - p, EL, "away") \
            == pytest.approx(1.0, abs=2e-4)

    @pytest.mark.parametrize("sport", ["basketball", "soccer", "tennis", "americanfootball"])
    def test_un_sport_hors_table_n_est_pas_touche(self, sport):
        assert sport not in CORRECTION_DOMICILE_LOGIT
        assert corriger_domicile(0.55, sport, "home") == 0.55

    @pytest.mark.parametrize("cote", ["over", "under", "", None])
    def test_un_marche_sans_domicile_n_est_pas_touche(self, cote):
        assert corriger_domicile(0.55, EL, cote) == 0.55

    @pytest.mark.parametrize("p", [0.0, 1.0, -0.1, 1.2])
    def test_une_probabilite_degeneree_est_rendue_telle_quelle(self, p):
        assert corriger_domicile(p, EL, "home") == p

    def test_la_table_reste_bornee(self):
        """Un décalage de plus de 0,25 logit (≈ 6 pts) contredirait la mesure
        multi-saisons qui le fonde : il faudrait une nouvelle étude."""
        assert all(0 < d <= 0.25 for d in CORRECTION_DOMICILE_LOGIT.values())


def _dans(heures=4):
    return (datetime.now(timezone.utc) + timedelta(hours=heures)).isoformat()


def _spreads(sport):
    """Away +6.5 : 1xbet 1.93, Pinnacle 2.05 / 1.80 → extérieur 53,2 % brut,
    EV +2,8 % ; corrigé Euroleague → 50,3 %, EV négatif."""
    m = {"id": "el1", "commence_time": _dans(),
         "spreads_1xbet": fusionner_lignes({REF: {"point": -6.5, "home": 1.93, "away": 1.93}}, "spreads"),
         "spreads_pinnacle": {"point": -6.5, "home": 2.05, "away": 1.80}}
    out: list = []
    eng._process_spreads(m, "Real Madrid vs Fenerbahce", sport, "L", "Real Madrid", "Fenerbahce",
                         "🏀", out, None, datetime.now(timezone.utc), log, min_edge=2.0)
    return out


class TestMoteur:
    def test_le_handicap_exterieur_euroleague_n_est_plus_emis(self, caplog):
        with caplog.at_level(logging.INFO):
            out = _spreads(EL)
        assert not [s for s in out if s.get("market_key") == "spreads_away"]
        assert any("DOMICILE" in r.getMessage() and "away" in r.getMessage() for r in caplog.records)

    def test_sans_la_correction_le_meme_handicap_euroleague_serait_emis(self, monkeypatch):
        """Contre-épreuve : c'est bien la correction, pas un autre gate, qui
        écarte le pari ci-dessus."""
        import core.constants as c
        monkeypatch.setattr(c, "CORRECTION_DOMICILE_LOGIT", {})
        out = _spreads(EL)
        assert [s for s in out if s.get("market_key") == "spreads_away"]

    def test_le_meme_prix_en_nba_passe_toujours_sans_correction(self, caplog):
        with caplog.at_level(logging.INFO):
            out = _spreads("basketball")
        assert not any("DOMICILE" in r.getMessage() for r in caplog.records)
        assert [s for s in out if s.get("market_key") == "spreads_away"], \
            "le témoin NBA doit émettre : sinon le test Euroleague ne prouve rien"


def _h2h(sport):
    """Favori extérieur : 1xbet 1.70, Pinnacle 2.45 / 1.58 → 59,6 % brut."""
    m = {"id": "el2", "commence_time": _dans(),
         "odds_1xbet": {"1": 2.20, "X": 0.0, "2": 1.70},
         "odds_pinnacle": {"1": 2.45, "X": 0.0, "2": 1.58}}
    out: list = []
    eng._process_h2h(m, "Real Madrid vs Fenerbahce", sport, "L", "Real Madrid", "Fenerbahce",
                     "🏀", out, None, datetime.now(timezone.utc), log, min_edge=2.0)
    return out


class TestMoteurVainqueur:
    def test_le_favori_exterieur_euroleague_est_corrige_et_logge(self, caplog):
        with caplog.at_level(logging.INFO):
            _h2h(EL)
        assert any("DOMICILE" in r.getMessage() and "h2h away" in r.getMessage() for r in caplog.records)

    def test_le_favori_exterieur_a_plus_quatre_pour_cent_brut_n_est_plus_emis(self, monkeypatch):
        """61,2 % brut (EV +4,1 %) → ≈ 58,3 % corrigé, EV négatif à 1,70."""
        import core.constants as c
        assert _h2h(EL) == []
        monkeypatch.setattr(c, "CORRECTION_DOMICILE_LOGIT", {})
        sans = _h2h(EL)
        assert [s["market_key"] for s in sans] == ["h2h"], "témoin : émis sans correction"
        assert sans[0]["sharp_prob"] == pytest.approx(0.6124, abs=1e-3)


# ── 3. La dérive sharp compare deux prix de MARCHÉ ─────────────────────────

from core.constants import CORRECTION_DOMICILE_DEPUIS  # noqa: E402
from core.learning_layer import _derive_valeurs, _instant, _prob_marche  # noqa: E402
from core.math_engine import retirer_correction_domicile  # noqa: E402

APRES, AVANT = "2026-11-03 18:00:00.12+00", "2026-11-01T18:00:00+00:00"


def _ligne(**kw):
    base = {"sport": EL, "market_type": "spreads_away", "sharp_prob": 0.5025,
            "closing_pinnacle_price": 1.90, "closing_source": "exchange",
            "match": "Real Madrid vs Fenerbahce", "selection": "Fenerbahce +6.5",
            "signal_created_at": APRES}
    return {**base, **kw}


class TestDerive:
    @pytest.mark.parametrize("p", [0.2, 0.5025, 0.71])
    def test_l_inverse_est_exact(self, p):
        for cote in ("home", "away"):
            assert retirer_correction_domicile(corriger_domicile(p, EL, cote), EL, cote) \
                == pytest.approx(p, abs=1e-4)

    def test_apres_la_mise_en_service_la_correction_est_retiree(self):
        assert _prob_marche(_ligne()) == pytest.approx(0.5325, abs=1e-3)

    def test_avant_la_mise_en_service_rien_ne_bouge(self):
        assert _prob_marche(_ligne(signal_created_at=AVANT)) == 0.5025

    def test_un_total_ou_un_autre_sport_ne_bouge_pas(self):
        assert _prob_marche(_ligne(market_type="totals_over")) == 0.5025
        assert _prob_marche(_ligne(sport="basketball")) == 0.5025

    def test_vainqueur_le_cote_se_lit_au_nom_exact(self):
        dom = _ligne(market_type="h2h", selection="Real Madrid", sharp_prob=0.583)
        ext = _ligne(market_type="h2h", selection="Fenerbahce", sharp_prob=0.583)
        assert _prob_marche(dom) == pytest.approx(retirer_correction_domicile(0.583, EL, "home"))
        assert _prob_marche(ext) == pytest.approx(retirer_correction_domicile(0.583, EL, "away"))

    def test_un_cote_introuvable_ecarte_la_ligne_au_lieu_de_la_fausser(self):
        ligne = _ligne(market_type="h2h", selection="Fener")      # pas le nom exact
        assert _prob_marche(ligne) is None
        assert _derive_valeurs([ligne]) == []

    def test_la_derive_d_un_pari_sans_mouvement_de_marche_reste_nulle(self):
        """Marché : extérieur 53,25 % à l'entrée ET à la clôture (1/0,5325).
        Sans compensation, la correction se lirait comme +6 % de dérive."""
        ligne = _ligne(closing_pinnacle_price=round(1 / 0.5325, 4))
        assert _derive_valeurs([ligne])[0] == pytest.approx(0.0, abs=0.1)

    @pytest.mark.parametrize("txt", ["2026-11-02T14:50:00+00:00", "2026-11-02 14:50:00+00",
                                     "2026-11-02T14:50:00Z", "2026-11-02 14:50:00.000+00:00"])
    def test_les_formats_de_date_de_la_base_se_lisent(self, txt):
        assert _instant(txt) == _instant(CORRECTION_DOMICILE_DEPUIS)
