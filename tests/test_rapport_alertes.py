"""
tests/test_rapport_alertes.py — run_rapport ne porte plus que les ALERTES
TECHNIQUES (décisions opérateur du 2026-10-29) : base injoignable, moteur
muet, créneau `standard` dû et non servi. Plus aucune liste de paris.

La garde du créneau dû vient de l'ancien tests/test_rapport_digest.py
(incident du 2026-09-11), réécrite sur `composer_alertes`.
"""
from datetime import datetime, timezone

import run_rapport


class TestCreneauStandard:
    """Le 2026-09-11 le chien de garde Cloudflare s'est tu (« Workers Cron
    Triggers degraded ») : créneau 11:03 jamais servi, 14 ticks de closing
    line sur 15 perdus, tous les runs verts, et ce digest déclarait le moteur
    vivant à 11:42 — les reprice gardent `last_scan` frais (règle dure n°12 :
    surveiller le CRÉNEAU DÛ, jamais la fraîcheur d'un fichier)."""
    # 12:44 UTC : le dernier créneau dû est 11:03, le précédent 09:03.
    T = datetime(2026, 9, 11, 12, 44, tzinfo=timezone.utc)

    def test_le_cas_du_2026_09_11_alerte(self):
        # meta tel qu'il était : 09:03 servi à 10:54, réclamation de 09:03 à 10:48.
        m = run_rapport._creneau_manque("2026-09-11T09:03",
                                        "2026-09-11T09:03|2026-09-11T10:48:08+00:00", self.T)
        assert m == "11:03 (+101 min)"

    def test_servi_pas_d_alerte(self):
        assert run_rapport._creneau_manque("2026-09-11T11:03", None, self.T) is None

    def test_reclame_recemment_pas_d_alerte(self):
        # Un scan en cours réclame le créneau avant de payer : ce n'est pas un trou.
        claim = "2026-09-11T11:03|2026-09-11T12:30:00+00:00"
        assert run_rapport._creneau_manque("2026-09-11T09:03", claim, self.T) is None

    def test_reclamation_expiree_alerte(self):
        # Réclamé puis mort : après SLOT_CLAIM_TTL_MIN le créneau redevient dû.
        claim = "2026-09-11T11:03|2026-09-11T11:05:00+00:00"
        assert run_rapport._creneau_manque("2026-09-11T09:03", claim, self.T) is not None

    def test_dans_la_grace_pas_d_alerte(self):
        tot = datetime(2026, 9, 11, 11, 3 + run_rapport.CRENEAU_GRACE_MIN - 1, tzinfo=timezone.utc)
        assert run_rapport._creneau_manque(None, None, tot) is None
        tard = datetime(2026, 9, 11, 11, 3 + run_rapport.CRENEAU_GRACE_MIN, tzinfo=timezone.utc)
        assert run_rapport._creneau_manque(None, None, tard) is not None

    def test_claim_illisible_ne_bloque_pas_l_alerte(self):
        assert run_rapport._creneau_manque(None, "n'importe quoi", self.T) is not None
        assert run_rapport._creneau_manque(None, "2026-09-11T11:03|pas-une-date", self.T) is not None

    def test_le_message_porte_l_alerte_seule_et_renvoie_au_diagnostic(self):
        msg = run_rapport.composer_alertes(self.T, creneau="11:03 (+101 min)")
        assert msg is not None and "11:03 (+101 min)" in msg
        assert "ops.py watchdog" in msg          # le diagnostic qui tranche la cause
        assert "Moteur muet" not in msg          # ce n'est PAS la même alerte

    def test_silence_intact_sans_creneau_manque(self):
        assert run_rapport.composer_alertes(self.T, creneau=None) is None

    def test_la_grace_laisse_le_worker_servir_en_premier(self):
        """Le Worker sert un créneau à grace_min + 10 (son tick) au plus tard ;
        alerter avant reviendrait à crier avant qu'il ait eu sa chance. Lu dans
        le JS, pas recopié (règle n°6)."""
        import re
        js = open("scripts/cloudflare_watchdog_worker.js", encoding="utf-8").read()
        m = re.search(r"grace_min:\s*(\d+)", js)
        assert m, "grace_min introuvable dans le Worker"
        assert run_rapport.CRENEAU_GRACE_MIN >= int(m.group(1)) + 10
        # …et reste sous le retard du seul cron GitHub, sinon l'alerte ne dirait
        # rien du Worker (mesuré le 2026-09-11 : +28 à +104 min).
        assert run_rapport.CRENEAU_GRACE_MIN < 28


class TestAlertesSeulement:
    T = datetime(2026, 10, 29, 13, 35, tzinfo=timezone.utc)

    def test_aucun_probleme_silence(self):
        assert run_rapport.composer_alertes(self.T) is None

    def test_moteur_muet(self):
        msg = run_rapport.composer_alertes(self.T, muet_h=3.4)
        assert "Moteur muet depuis 3.4 h" in msg

    def test_plus_aucune_liste_de_paris(self):
        import inspect
        src = inspect.getsource(run_rapport)
        assert '.table("signals")' not in src and "Encore jouables" not in src

    def test_texte_brut(self):
        import inspect
        assert "parse_mode" not in inspect.getsource(run_rapport._send)
