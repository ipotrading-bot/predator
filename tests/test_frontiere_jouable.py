"""
tests/test_frontiere_jouable.py — l'expérience PRÉ-ENREGISTRÉE sur la borne
basse de la zone jouable (2026-09-21).

POURQUOI CE TEST EXISTE, ET QUELLE ERREUR IL ENCODE.

La borne basse vaut 120 min, mais `run_engine._shadow_reason` admet lui-même que
la mesure fondatrice du 2026-08-04 « portait sur la TRANCHE HORAIRE » — l'heure
de la journée, pas le délai avant le coup d'envoi. La règle a donc été posée sur
une grandeur et justifiée par une autre.

Mesuré le 2026-09-21 par bande de délai (soccer, post-A6) : la bande 60-120 min
est la MEILLEURE du ledger (+19,8 % d'EV/pari, +5,42 u) et elle est jetée, tandis
que 0-30 min fait −5,7 % et 30-60 min −21,6 %. Agrégé à 60 min : z = 2,08.

Puis le scan de TOUTES les coupures de 20 à 300 min a montré que z > 1,96
n'apparaît qu'à 40, 60 et 290 (290 = bruit) : avec 29 coupures testées,
Bonferroni exige z ≈ 2,99. Le « meilleur » z était donc un MAXIMUM SÉLECTIONNÉ,
pas une preuve — exactement le chemin bifurquant. Rien n'a bougé.

Ces tests gardent la discipline qui remplace l'intuition :
  1. la coupure et la taille requise sont PRÉ-ENREGISTRÉES et importées, pas
     choisies après coup ni recopiées ;
  2. sous la taille requise, la section ne conclut RIEN — c'est le test n°4 qui
     rejoue les chiffres réels du 2026-09-21 et exige « aucune conclusion » ;
  3. le sens de la bascule est dit en termes de la CONSTANTE à bouger, jamais
     par un « plus tôt / plus tard » ambigu ;
  4. le groupe « sous la coupure » est fait de FANTÔMES : la fonction ne doit
     jamais les filtrer, sinon elle mesurerait le vide.
"""
import inspect

from core.learning_layer import (FRONTIERE_DECISION_LE, FRONTIERE_N_REQUIS,
                                 FRONTIERE_TESTEE_MINUTES, _PLAYABLE_MIN_MINUTES)
from scripts import weekly_report as wr


def _ligne(ttm, outcome, odds=1.757, shadow=None, quand="2026-09-10T00:00:00+00:00"):
    d = {"time_to_match_minutes": ttm, "outcome": outcome, "odds": odds,
         "created_at": quand}
    if shadow is not None:
        d["is_shadow"] = shadow
    return d


def _jeu(n_sous_w, n_sous_l, n_sur_w, n_sur_l, fois=1):
    return ([_ligne(30, "WIN", shadow=True)] * n_sous_w
            + [_ligne(30, "LOSS", shadow=True)] * n_sous_l
            + [_ligne(600, "WIN", shadow=False)] * n_sur_w
            + [_ligne(600, "LOSS", shadow=False)] * n_sur_l) * fois


class TestPreEnregistrement:
    def test_la_coupure_est_importee_pas_recopiee(self):
        """Règle n°6 : la pré-enregistration vit dans core.learning_layer, à
        côté de la constante qu'elle teste."""
        src = inspect.getsource(wr.frontiere_jouable)
        assert "FRONTIERE_TESTEE_MINUTES" in src
        assert "60" not in src.split('"""')[2]      # pas de 60 en dur dans le corps

    def test_la_coupure_testee_est_sous_la_borne_actuelle(self):
        """L'expérience n'a de sens que si elle teste une borne PLUS BASSE : au
        dessus, il n'y aurait aucune bande à récupérer."""
        assert FRONTIERE_TESTEE_MINUTES < _PLAYABLE_MIN_MINUTES

    def test_la_date_de_decision_est_datee(self):
        """Règle 13, esprit : un organe de mesure porte son échéance."""
        assert len(FRONTIERE_DECISION_LE) == 10 and FRONTIERE_DECISION_LE[4] == "-"

    def test_le_n_requis_est_celui_de_la_puissance_calculee(self):
        """200 par groupe = 80 % de puissance pour 13,8 points d'écart à
        p̄ ≈ 0,585. Baisser cette barre rouvrirait la porte que ce test ferme."""
        assert FRONTIERE_N_REQUIS >= 200


class TestRefusDeConclure:
    def test_les_chiffres_reels_du_21_09_ne_concluent_rien(self):
        """LE test qui encode l'erreur évitée : sur les proportions réellement
        mesurées (49-46 sous, 85-45 dès), z vaut bien 2,08 — et la section doit
        néanmoins refuser de trancher, parce que n vaut 95 et 130 contre 200."""
        etat = wr.frontiere_jouable(_jeu(49, 46, 85, 45))
        assert etat["z"] is not None and round(etat["z"], 2) == 2.08
        assert etat["decidable"] is False
        rendu = "\n".join(wr.format_frontiere(etat))
        assert "AUCUNE conclusion" in rendu
        assert "DESCENDRE" not in rendu and "significatif" not in rendu
        assert FRONTIERE_DECISION_LE in rendu

    def test_le_manque_de_lignes_est_chiffre(self):
        etat = wr.frontiere_jouable(_jeu(49, 46, 85, 45))
        rendu = "\n".join(wr.format_frontiere(etat))
        assert "95 et 130" in rendu and "175 lignes à venir" in rendu

    def test_un_groupe_vide_naffiche_rien(self):
        """Pas de section plutôt qu'une section qui ne dit rien."""
        assert wr.format_frontiere(wr.frontiere_jouable([])) == []
        assert wr.format_frontiere(wr.frontiere_jouable(_jeu(0, 0, 10, 5))) == []


class TestDecision:
    def test_n_atteint_et_ecart_significatif_nomme_la_constante(self):
        etat = wr.frontiere_jouable(_jeu(49, 46, 85, 45, fois=3))
        assert etat["decidable"] is True
        rendu = "\n".join(wr.format_frontiere(etat))
        assert "_PLAYABLE_MIN_MINUTES doit DESCENDRE" in rendu
        assert "plus tard" not in rendu.lower() and "plus tôt" not in rendu.lower()

    def test_n_atteint_et_ecart_nul_clot_lexperience(self):
        etat = wr.frontiere_jouable(_jeu(120, 120, 120, 120))
        assert etat["decidable"] is True
        rendu = "\n".join(wr.format_frontiere(etat))
        assert "tient, expérience close" in rendu

    def test_un_ecart_inverse_dit_de_MONTER(self):
        """Si la bande basse faisait mieux, la conclusion s'inverse — et la
        section doit le dire, pas afficher un message générique."""
        etat = wr.frontiere_jouable(_jeu(170, 60, 130, 130))
        rendu = "\n".join(wr.format_frontiere(etat))
        assert "INVERSE" in rendu and "doit MONTER" in rendu


class TestContrat:
    def test_les_fantomes_ne_sont_PAS_filtres(self):
        """Le groupe « sous la coupure » est fait de fantômes : les filtrer
        viderait l'expérience. C'est l'inverse du contrat de league_breakdown."""
        etat = wr.frontiere_jouable(_jeu(49, 46, 85, 45))
        assert etat["sous"]["n"] == 95
        assert "is_shadow" not in inspect.getsource(wr.frontiere_jouable)

    def test_le_point_mort_utilise_le_taux_du_depot(self):
        from core.constants import TAX_RATE
        from core.stats_utils import p_breakeven
        etat = wr.frontiere_jouable(_jeu(49, 46, 85, 45))
        assert etat["sur"]["p_breakeven"] == p_breakeven(etat["sur"]["avg_odds"], TAX_RATE)

    def test_lepoque_est_respectee(self):
        """Règle 10 : seules les lignes post-A6 pèsent."""
        vieux = [_ligne(600, "WIN", quand="2026-08-01T00:00:00+00:00")] * 50
        etat = wr.frontiere_jouable(_jeu(49, 46, 85, 45) + vieux)
        assert etat["sur"]["n"] == 85 + 45

    def test_la_section_sintegre_au_rapport(self):
        from datetime import datetime, timezone
        etat = wr.frontiere_jouable(_jeu(49, 46, 85, 45))
        texte = wr.format_report({}, {}, (0, 0), datetime(2026, 9, 21, tzinfo=timezone.utc),
                                 None, None, None, wr.format_frontiere(etat))
        assert "Frontière jouable" in texte

    def test_vide_reste_vide(self):
        from datetime import datetime, timezone
        texte = wr.format_report({}, {}, (0, 0), datetime(2026, 9, 21, tzinfo=timezone.utc))
        assert "Frontière jouable" not in texte
