"""
tests/test_bande_cote.py — l'hypothèse PRÉ-ENREGISTRÉE « cote 1,50-1,75 »
(2026-09-27).

POURQUOI CE TEST EXISTE.

L'opérateur demandait comment passer de 60 % à 70 % de réussite. Mesuré : 60,2 %
réalisé pour 59,6 % annoncé par le sharp — le moteur est calibré, et le taux se
règle par la cote. Découpés après coup, les segments « gagnants » s'inversaient
tous sur les fantômes, sauf la bande 1,50-1,75 (zone 37-13 pour 62,6 %
annoncé ; fantômes 21-9). Lue parmi cinq bandes, elle n'est qu'une hypothèse.

Ces tests gardent la discipline de la pré-enregistration :
  1. bornes, population, taille et échéance sont IMPORTÉES, jamais recopiées ;
  2. les lignes qui ont fait naître l'hypothèse (émises avant BANDE_COTE_DEPUIS)
     ne comptent pas — datation par SIGNAL, pas par règlement ;
  3. la mesure se fait contre la sharp_prob ANNONCÉE, jamais contre une autre
     bande : le taux de réussite dépend de la cote par construction ;
  4. sous la taille requise, rien n'est conclu, quel que soit le z.
"""
import inspect
from datetime import datetime, timezone

from core.learning_layer import (BANDE_COTE_DECISION_LE, BANDE_COTE_DEPUIS,
                                 BANDE_COTE_MAX, BANDE_COTE_MIN, BANDE_COTE_N_REQUIS,
                                 _PLAYABLE_MAX_MINUTES, _PLAYABLE_MIN_MINUTES)
from scripts import weekly_report as wr

_APRES = "2026-10-05T12:00:00+00:00"


def _ligne(outcome, odds=1.65, prob=0.626, ttm=600, shadow=False,
           emis=_APRES, regle=_APRES, sport="soccer"):
    return {"outcome": outcome, "odds": odds, "sharp_prob": prob,
            "time_to_match_minutes": ttm, "is_shadow": shadow, "sport": sport,
            "signal_created_at": emis, "created_at": regle}


def _jeu(w, l, **kw):
    return [_ligne("WIN", **kw)] * w + [_ligne("LOSS", **kw)] * l


class TestPreEnregistrement:
    def test_les_constantes_sont_importees_pas_recopiees(self):
        """Règle n°6 : la pré-enregistration vit dans core.learning_layer."""
        src = inspect.getsource(wr.bande_cote).split('"""')[2]
        for nom in ("BANDE_COTE_MIN", "BANDE_COTE_MAX", "BANDE_COTE_DEPUIS",
                    "_PLAYABLE_MIN_MINUTES", "_PLAYABLE_MAX_MINUTES"):
            assert nom in src
        for litteral in ("1.5", "1.75", "2026-09-28", "120", "1440"):
            assert litteral not in src

    def test_les_bornes_sont_celles_qui_ont_ete_vues(self):
        """Retoucher la bande après coup rouvrirait le chemin bifurquant."""
        assert (BANDE_COTE_MIN, BANDE_COTE_MAX) == (1.50, 1.75)

    def test_la_fenetre_commence_apres_la_mesure(self):
        """Les données du 2026-09-27 ont fait naître l'hypothèse : elles ne
        peuvent pas la confirmer."""
        assert BANDE_COTE_DEPUIS > "2026-09-27"

    def test_le_n_requis_est_celui_de_la_puissance_calculee(self):
        """≈ 183 pour +10 points à 80 % de puissance, arrondi à 200."""
        assert BANDE_COTE_N_REQUIS >= 200

    def test_lecheance_est_datee(self):
        assert len(BANDE_COTE_DECISION_LE) == 10 and BANDE_COTE_DECISION_LE > BANDE_COTE_DEPUIS


class TestPopulation:
    def test_les_lignes_de_la_decouverte_ne_comptent_pas(self):
        """Émise avant la fenêtre, réglée après : exclue (datation par SIGNAL)."""
        decouverte = _jeu(37, 13, emis="2026-09-20T00:00:00+00:00")
        assert wr.bande_cote(decouverte + _jeu(3, 2))["n"] == 5

    def test_une_ligne_non_datee_est_ecartee(self):
        sans = [dict(_ligne("WIN"), signal_created_at=None)] * 10
        assert wr.bande_cote(sans)["n"] == 0

    def test_shadow_et_hors_zone_sont_ecartes(self):
        pollution = (_jeu(10, 0, shadow=True)
                     + _jeu(10, 0, ttm=_PLAYABLE_MIN_MINUTES - 1)
                     + _jeu(10, 0, ttm=_PLAYABLE_MAX_MINUTES + 1))
        assert wr.bande_cote(pollution + _jeu(3, 2))["n"] == 5

    def test_les_bornes_de_cote(self):
        """Borne basse incluse, borne haute exclue."""
        lignes = (_jeu(1, 0, odds=BANDE_COTE_MIN) + _jeu(1, 0, odds=BANDE_COTE_MAX)
                  + _jeu(1, 0, odds=1.49) + _jeu(1, 0, odds=1.74))
        assert wr.bande_cote(lignes)["n"] == 2

    def test_tous_sports(self):
        assert wr.bande_cote(_jeu(2, 1) + _jeu(2, 1, sport="baseball"))["n"] == 6

    def test_sans_sharp_prob_ecartee_et_comptee(self):
        etat = wr.bande_cote(_jeu(3, 2) + _jeu(4, 0, prob=None))
        assert etat["n"] == 5 and etat["sans_prob"] == 4
        assert "sans sharp" in "\n".join(wr.format_bande_cote(etat))


class TestMesure:
    def test_z_contre_la_probabilite_annoncee(self):
        """Calibré à l'annonce : z nul, même avec un taux de 74 %."""
        etat = wr.bande_cote(_jeu(74, 26, prob=0.74))
        assert abs(etat["z"]) < 1e-9 and round(etat["hit_rate"], 2) == 0.74

    def test_les_chiffres_du_27_09_ne_concluent_rien(self):
        """37-13 pour 62,6 % annoncé : z ≈ +1,6 et n = 50 — aucune conclusion."""
        etat = wr.bande_cote(_jeu(37, 13))
        assert etat["decidable"] is False and etat["z"] > 1.5
        rendu = "\n".join(wr.format_bande_cote(etat))
        assert "AUCUNE conclusion" in rendu and "150 lignes à venir" in rendu
        assert "CONFIRMÉE" not in rendu and BANDE_COTE_DECISION_LE in rendu

    def test_n_atteint_et_ecart_confirme(self):
        etat = wr.bande_cote(_jeu(148, 52))
        assert etat["decidable"] is True and etat["z"] >= 1.96
        assert "CONFIRMÉE" in "\n".join(wr.format_bande_cote(etat))

    def test_n_atteint_sans_ecart_clot(self):
        etat = wr.bande_cote(_jeu(125, 75))
        assert "NON démontré" in "\n".join(wr.format_bande_cote(etat))

    def test_la_regle_7_est_tenue(self):
        """Jamais un taux nu : Wilson et point mort taxé à côté."""
        from core.constants import TAX_RATE
        from core.stats_utils import p_breakeven
        etat = wr.bande_cote(_jeu(37, 13))
        assert etat["p_breakeven"] == p_breakeven(etat["avg_odds"], TAX_RATE)
        rendu = "\n".join(wr.format_bande_cote(etat))
        assert "IC95" in rendu and "point mort" in rendu

    def test_vide_dit_zero(self):
        rendu = "\n".join(wr.format_bande_cote(wr.bande_cote([])))
        assert f"0/{BANDE_COTE_N_REQUIS}" in rendu and "AUCUNE conclusion" in rendu


class TestContrat:
    def test_lecture_tronquee_signalee(self):
        """Si la plus vieille ligne lue est réglée après le début de la fenêtre,
        n n'est qu'un plancher — la section doit le dire."""
        etat = wr.bande_cote(_jeu(3, 2))
        assert etat["tronque"] is True
        assert "plancher" in "\n".join(wr.format_bande_cote(etat))
        vieille = [_ligne("WIN", odds=2.5, regle="2026-09-01T00:00:00+00:00")]
        assert wr.bande_cote(_jeu(3, 2) + vieille)["tronque"] is False

    def test_main_mesure_sur_les_lignes_datees(self):
        src = inspect.getsource(wr.main)
        assert src.index("_dater_par_signal(sb, ledger_rows)") < src.index("bande_cote(ledger_rows)")
        assert "sharp_prob" in wr._LEAGUE_SELECT

    def test_la_section_sintegre_au_rapport(self):
        texte = wr.format_report({}, {}, (0, 0), datetime(2026, 10, 5, tzinfo=timezone.utc),
                                 None, None, None, None,
                                 wr.format_bande_cote(wr.bande_cote(_jeu(3, 2))))
        assert "Bande de cote" in texte

    def test_le_markdown_telegram_est_ferme(self):
        """Un `*` ou un `_` non fermé fait refuser le rapport ENTIER (HTTP 400,
        2026-09-14). Le premier jet de cette section ouvrait son titre sans le
        fermer."""
        for etat in (wr.bande_cote([]), wr.bande_cote(_jeu(37, 13)),
                     wr.bande_cote(_jeu(3, 2) + _jeu(1, 0, prob=None))):
            for ligne in wr.format_bande_cote(etat):
                assert ligne.count("*") % 2 == 0
                assert ligne.replace("\\_", "").count("_") % 2 == 0

    def test_la_section_ne_touche_pas_lemission(self):
        """Règles 10 et 11 : l'hypothèse ne filtre rien d'elle-même."""
        import run_engine
        assert "BANDE_COTE" not in inspect.getsource(run_engine)
