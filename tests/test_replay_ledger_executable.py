"""
tests/test_replay_ledger_executable.py — PHASE A0.

Garde les fonctions PURES de `scripts/replay_ledger_executable.py`. Le script
est un instrument de mesure : s'il se trompe, il produit un chiffre faux qui
sera ensuite recopié dans une décision de calibration (A6). Les tests portent
donc sur ce qui pourrait mentir en silence — le périmètre du prélèvement, la
détection des deux unités d'edge, et la neutralité du PUSH.

Aucune I/O : `replay()` est une fonction de sa liste de lignes.
"""
import pytest

from core.constants import TAX_RATE
from scripts.replay_ledger_executable import (
    EDGE_UNIT_TOL,
    HAIRCUT_DEFAULT,
    edge_of,
    executable_odd,
    is_devigged,
    measure_haircut,
    normalize,
    replay,
    resolution_rate,
    roi,
    sensitivity,
    sharp_prob_of,
    survivors,
)


def _ligne(**kw) -> dict:
    """Ligne de ledger minimale, surchargeable."""
    base = {
        "sport": "soccer", "market_type": "h2h", "outcome": "WIN",
        "odds": 2.00, "sharp_prob": 0.55, "initial_edge": 10.0,
        "kelly_pct": 1.0, "created_at": "2026-08-25T12:00:00+00:00",
    }
    base.update(kw)
    return base


class TestPerimetreDuPrelevement:
    """Seul le football h2h stocke un prix dévigorisé — le reste est déjà brut."""

    def test_le_football_h2h_est_devigorise(self):
        assert is_devigged("soccer", "h2h") is True

    def test_les_autres_marches_du_football_sont_bruts(self):
        for marche in ("totals_over", "totals_under", "spreads_home", "spreads_away"):
            assert is_devigged("soccer", marche) is False, marche

    def test_le_h2h_hors_football_est_brut(self):
        for sport in ("basketball", "mma", "baseball", "tennis"):
            assert is_devigged(sport, "h2h") is False, sport

    def test_le_prelevement_ne_touche_que_le_football_h2h(self):
        assert executable_odd(2.00, "soccer", "h2h", 0.90) == 1.80
        # Ailleurs la cote stockée est celle du book : on n'y touche pas.
        assert executable_odd(2.00, "basketball", "h2h", 0.90) == 2.00
        assert executable_odd(2.00, "soccer", "totals_over", 0.90) == 2.00

    def test_une_cote_invalide_rend_zero_et_pas_une_valeur_negative(self):
        assert executable_odd(0.0, "soccer", "h2h") == 0.0
        assert executable_odd(1.0, "soccer", "h2h") == 0.0


class TestPrelevementMesure:
    """`measure_haircut` doit reproduire le rapport DNB exécutable / DNB
    dévigorisé, sans supposer la formule multiplicative."""

    def test_un_1x2_sans_marge_ne_preleve_presque_rien(self):
        # Marché à somme des probabilités = 1 : le dévigorisé et l'exécutable
        # coïncident, le prélèvement doit être ~1.0.
        o1, ox, o2 = 2.0, 4.0, 4.0     # 0.50 + 0.25 + 0.25 = 1.00
        d = measure_haircut([{"1": o1, "X": ox, "2": o2}])
        assert d["n"] == 1
        assert abs(d["median"] - 1.0) < 0.01

    def test_une_marge_de_10_pct_preleve_environ_10_pct(self):
        # 1X2 margé ~10 % : le prélèvement doit tomber dans la bande mesurée.
        d = measure_haircut([{"1": 1.80, "X": 3.40, "2": 4.20}])
        assert 0.85 < d["median"] < 0.96

    def test_les_cotes_invalides_sont_ecartees_pas_comptees(self):
        d = measure_haircut([{"1": 0, "X": 3.4, "2": 4.2},
                             {"1": 1.8, "X": 1.0, "2": 4.2},
                             {"1": 1.80, "X": 3.40, "2": 4.20}])
        assert d["n"] == 1

    def test_une_liste_vide_ne_leve_pas(self):
        assert measure_haircut([])["n"] == 0
        assert measure_haircut(None)["n"] == 0

    def test_la_formule_est_bien_o1_fois_ox_moins_un_sur_ox(self):
        # Le prélèvement n'est pas un facteur choisi : c'est le rapport entre
        # le DNB synthétique et le `calc_dnb()` du dépôt, sur le MÊME carnet.
        from core.math_engine import calc_dnb
        o1, ox, o2 = 1.80, 3.60, 4.50
        attendu = (o1 * (ox - 1) / ox) / calc_dnb(o1, o2, ox)
        assert measure_haircut([{"1": o1, "X": ox, "2": o2}])["median"] == \
            pytest.approx(attendu, abs=1e-4)

    def test_le_defaut_du_script_est_dans_la_bande_mesurable(self):
        # Garde-fou contre un HAIRCUT_DEFAULT retouché à la main hors mesure.
        assert 0.85 <= HAIRCUT_DEFAULT <= 1.0


class TestDeuxUnitesDEdge:
    """Le ledger porte deux échelles d'edge ; les confondre fabrique un
    « avant » qui n'a existé à aucune époque."""

    def test_une_ligne_du_moteur_actuel_est_reconnue_comme_unite_ev(self):
        # edge = (0.55 × 2.00 − 1) × 100 = +10.0
        r = normalize(_ligne(odds=2.00, sharp_prob=0.55, initial_edge=10.0), 1.0)
        assert r["unite_ev"] is True
        assert r["edge_base"] == 10.0

    def test_une_ligne_en_ratio_de_prix_est_reconnue(self):
        # Cas réel du 2026-08 : +5.88 écrit, −7.34 d'EV vraie.
        r = normalize(_ligne(odds=1.08, sharp_prob=0.8580, initial_edge=5.88), 1.0)
        assert r["unite_ev"] is False
        assert r["edge_base"] < 0

    def test_larrondi_de_la_cote_ne_fait_pas_basculer_dunite(self):
        # `signals.xbet_odd` est un numeric(6,2) : l'edge écrit peut différer
        # de l'identité de quelques dixièmes sans changer d'unité.
        r = normalize(_ligne(odds=1.07, sharp_prob=0.9661, initial_edge=2.94), 1.0)
        assert abs(r["edge_base"] - r["edge_stored"]) < EDGE_UNIT_TOL
        assert r["unite_ev"] is True

    def test_le_rapport_compte_les_deux_unites_separement(self):
        rows = [_ligne(odds=2.00, sharp_prob=0.55, initial_edge=10.0),
                _ligne(odds=1.08, sharp_prob=0.8580, initial_edge=5.88)]
        rep = replay(rows)
        assert rep["unites"]["ev"] == 1
        assert rep["unites"]["ratio_de_prix"] == 1


class TestAvantApres:
    """L'« avant » et l'« après » doivent différer par la COTE seulement."""

    def test_avant_et_apres_partagent_la_formule(self):
        r = normalize(_ligne(odds=2.00, sharp_prob=0.55), 0.90)
        assert r["edge_base"] == edge_of(0.55, 2.00)
        assert r["edge_replay"] == edge_of(0.55, 1.80)

    def test_un_marche_non_devigorise_ne_bouge_pas(self):
        r = normalize(_ligne(sport="basketball", market_type="h2h",
                             odds=2.00, sharp_prob=0.55), 0.90)
        assert r["edge_base"] == r["edge_replay"]

    def test_les_survivants_se_comptent_sur_lavant_recalcule(self):
        # `emis_a_lepoque` lit l'edge ÉCRIT ; `before` lit l'edge recalculé.
        # Sur une ligne en ratio de prix, les deux doivent diverger.
        rows = [_ligne(odds=1.08, sharp_prob=0.8580, initial_edge=5.88)]
        recs = [normalize(r, HAIRCUT_DEFAULT) for r in rows]
        s = survivors(recs, 1.2)
        assert s["emis_a_lepoque"] == 1
        assert s["before"] == 0
        assert s["after"] == 0


class TestROI:
    """Le ROI doit rester une mesure, pas une opinion."""

    def test_un_gain_paie_la_cote_moins_la_mise(self):
        recs = [normalize(_ligne(outcome="WIN", odds=2.00), 1.0)]
        d = roi(recs, "odds", tax=0.0)
        assert d["n"] == 1 and d["profit"] == 1.0 and d["roi_pct"] == 100.0

    def test_la_taxe_ne_frappe_que_le_gain_net(self):
        recs = [normalize(_ligne(outcome="WIN", odds=2.00), 1.0)]
        assert roi(recs, "odds", tax=0.20)["profit"] == 0.80

    def test_une_perte_coute_la_mise_entiere_taxe_ou_non(self):
        recs = [normalize(_ligne(outcome="LOSS", odds=2.00), 1.0)]
        assert roi(recs, "odds", tax=0.20)["profit"] == -1.0

    def test_le_push_est_neutre_et_reste_au_denominateur(self):
        # Le DNB rembourse la mise sur le nul — au prix dévigorisé COMME au
        # prix exécutable, la jambe « nul » rendant exactement la mise.
        recs = [normalize(_ligne(outcome="PUSH", odds=2.00), 0.90)]
        d = roi(recs, "exec_odds", tax=0.20)
        assert d["profit"] == 0.0 and d["n"] == 1 and d["pushes"] == 1

    def test_une_ligne_sans_resultat_nentre_dans_aucun_ratio(self):
        recs = [normalize(_ligne(outcome="expired"), 1.0)]
        d = roi(recs, "odds")
        assert d["n"] == 0 and d["roi_pct"] is None

    def test_kelly_ignore_une_ligne_sans_mise_au_lieu_den_inventer_une(self):
        recs = [normalize(_ligne(outcome="WIN", kelly_pct=0.0), 1.0)]
        assert roi(recs, "odds", kelly=True)["n"] == 0


class TestTauxDeResolution:
    """réglés / (réglés + expired) — `active`/`closed` ne comptent nulle part."""

    def test_le_ledger_se_lit_sur_outcome(self):
        rows = [{"outcome": "WIN"}, {"outcome": "LOSS"}, {"outcome": "expired"},
                {"outcome": "PUSH"}]
        d = resolution_rate(rows)
        assert d["settled"] == 3 and d["expired"] == 1 and d["rate_pct"] == 75.0

    def test_les_signals_se_lisent_sur_status(self):
        rows = [{"status": "settled"}, {"status": "expired"}]
        d = resolution_rate(rows, field="status")
        assert d["settled"] == 1 and d["denom"] == 2 and d["rate_pct"] == 50.0

    def test_un_signal_encore_vivant_nabaisse_pas_le_taux(self):
        rows = [{"status": "settled"}, {"status": "active"}, {"status": "closed"}]
        d = resolution_rate(rows, field="status")
        assert d["denom"] == 1 and d["rate_pct"] == 100.0

    def test_aucune_ligne_ne_rend_none_et_pas_une_division_par_zero(self):
        assert resolution_rate([])["rate_pct"] is None


class TestSharpProb:
    """Reconstruire une probabilité manquante ne doit jamais inventer d'edge."""

    def test_la_probabilite_stockee_prime(self):
        assert sharp_prob_of({"sharp_prob": 0.55, "odds": 2.0, "initial_edge": 10.0}) == 0.55

    def test_labsence_est_comblee_par_lhypothese_du_scan(self):
        # fair = 2.00 / 1.10 → p = 0.55, soit l'edge écrit reconstitué à l'identique.
        p = sharp_prob_of({"sharp_prob": None, "odds": 2.00, "initial_edge": 10.0})
        assert abs(p - 0.55) < 1e-9

    def test_sans_donnee_exploitable_on_rend_zero(self):
        assert sharp_prob_of({"sharp_prob": None, "odds": None, "initial_edge": None}) == 0.0


class TestContratDuRapport:
    def test_replay_est_une_fonction_pure_de_ses_lignes(self):
        rows = [_ligne(), _ligne(outcome="LOSS")]
        assert replay(rows) == replay(rows)

    def test_une_ligne_illisible_est_ecartee_sans_lever(self):
        assert normalize({"odds": None}, 1.0) is None
        assert normalize({"odds": 1.0}, 1.0) is None

    def test_le_rapport_tient_sur_un_ledger_vide(self):
        rep = replay([])
        assert rep["n_lignes"] == 0
        assert rep["roi_reel_net"]["roi_pct"] is None


class TestSurvivantsEtSensibilite:
    """Le prélèvement ne peut que COÛTER : tout résultat qui améliorerait
    l'edge ou le nombre de survivants trahirait une erreur de signe."""

    LIGNES = [
        {"sport": "soccer", "market_type": "h2h", "odds": 1.50,
         "sharp_prob": 0.70, "initial_edge": 5.0, "outcome": "WIN"},
        {"sport": "soccer", "market_type": "h2h", "odds": 1.80,
         "sharp_prob": 0.60, "initial_edge": 8.0, "outcome": "LOSS"},
        {"sport": "soccer", "market_type": "h2h", "odds": 1.60,
         "sharp_prob": 0.66, "initial_edge": 5.6, "outcome": "expired"},
    ]

    def test_le_prelevement_ne_peut_que_reduire_le_nombre_de_survivants(self):
        recs = [normalize(r, 0.90) for r in self.LIGNES]
        s = survivors(recs, floor=1.2)
        assert s["after"] <= s["before"]

    def test_la_sensibilite_est_monotone_en_h(self):
        # Un prélèvement plus dur ne peut pas faire REMONTER l'edge médian.
        med = [s["edge_replay_median"]
               for s in sensitivity(self.LIGNES, [0.88, 0.92, 1.0])]
        assert med == sorted(med)

    def test_le_rapport_ne_touche_jamais_la_base(self):
        # `replay` est une fonction pure de ses lignes : aucun client Supabase
        # ne lui est passé, donc aucune écriture n'est possible.
        rep = replay(self.LIGNES, haircut=0.90)
        assert rep["n_lignes"] == 3
        assert rep["n_decisives"] == 2
        assert rep["n_devigorisees"] == 3
        assert rep["resolution_globale"]["rate_pct"] == pytest.approx(66.7)
        assert rep["roi_exec_net"]["roi_pct"] <= rep["roi_reel_brut"]["roi_pct"]

    def test_le_rapport_expose_la_taxe_du_depot_ET_la_taxe_reelle(self):
        # Les deux colonnes doivent rester visibles côte à côte : c'est ce qui
        # rend l'écart lisible AVANT que A2 ne rétablisse TAX_RATE. L'assertion
        # lit la constante plutôt que sa valeur du jour — sinon ce test
        # tomberait au moment précis où le dépôt se corrige.
        p = replay(self.LIGNES, haircut=0.90)["parametres"]
        assert p["tax_rate_reel"] == 0.20
        assert p["tax_rate_depot"] == pytest.approx(TAX_RATE)
