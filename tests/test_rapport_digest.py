"""
tests/test_rapport_digest.py — le digest Telegram (run_rapport) après le
2026-09-03 : la liste des paris RECOMMANDÉS encore jouables, 🆕 pour ceux nés
depuis le digest précédent, ⏳ pour les rappels, silence quand il n'y a rien
et que le moteur vit.

Avant : fenêtre de 2 h sur created_at — un pari émis à 05:00 pour 18:45
n'était annoncé qu'une fois, puis « Aucun signal actif » douze fois par jour
avec un texte qui renvoyait vers un workflow disparu (« Predator Engine »).
"""
from datetime import datetime, timedelta, timezone

import run_rapport

NOW = datetime(2026, 9, 3, 20, 35, tzinfo=timezone.utc)


def _sig(created_h_ago: float, ko_in_h: float | None = 3.0, **over) -> dict:
    s = {
        "match": "Toulouse vs Lille", "sport": "soccer", "market_key": "h2h",
        "selection_name": "Lille", "xbet_odd": 1.56, "sharp_prob": 0.66,
        "edge_pct": 3.9, "risk_flag": "VALUE",
        "created_at": (NOW - timedelta(hours=created_h_ago)).isoformat(),
        "match_time": (NOW + timedelta(hours=ko_in_h)).isoformat() if ko_in_h is not None else "",
    }
    s.update(over)
    return s


class TestPartition:
    def test_nouveau_si_cree_depuis_le_digest_precedent(self):
        n, r = run_rapport._partitionner([_sig(1.0), _sig(3.0)], NOW)
        assert len(n) == 1 and len(r) == 1

    def test_la_borne_est_report_window_h(self):
        n, r = run_rapport._partitionner([_sig(run_rapport.REPORT_WINDOW_H - 0.01),
                                          _sig(run_rapport.REPORT_WINDOW_H + 0.01)], NOW)
        assert len(n) == 1 and len(r) == 1

    def test_sans_created_at_lisible_compte_comme_nouveau(self):
        n, r = run_rapport._partitionner([_sig(5.0, created_at=None)], NOW)
        assert len(n) == 1 and r == []

    def test_tri_par_coup_denvoi_le_plus_proche_dabord(self):
        tard, tot, inconnu = _sig(1.0, ko_in_h=6), _sig(1.0, ko_in_h=1), _sig(1.0, ko_in_h=None)
        n, _ = run_rapport._partitionner([tard, inconnu, tot], NOW)
        assert n == [tot, tard, inconnu]


class TestComposer:
    def test_silence_sans_pari_et_moteur_vivant(self):
        assert run_rapport._composer([], [], NOW, stale=False) is None

    def test_alerte_seule_si_moteur_muet(self):
        msg = run_rapport._composer([], [], NOW, stale=True)
        assert "Moteur muet" in msg
        assert "Predator Scan" in msg          # le workflow qui existe
        assert "Predator Engine" not in msg    # celui qui n'existe plus

    def test_deux_sections_et_les_comptes(self):
        msg = run_rapport._composer([_sig(1.0)], [_sig(4.0, match="A vs B", selection_name="A")],
                                    NOW)
        assert "🆕 `1` nouveau(x)" in msg and "⏳ `1` encore jouable(s)" in msg
        assert msg.index("*Nouveaux*") < msg.index("Toulouse vs Lille") \
            < msg.index("*Encore jouables*") < msg.index("A vs B")

    def test_pas_de_section_vide(self):
        msg = run_rapport._composer([_sig(1.0)], [], NOW)
        assert "Encore jouables" not in msg
        assert "⏳" not in msg

    def test_pas_de_stat_de_performance_ni_de_regroupement_par_sport(self):
        # Le taux de réussite du ledger mélangeait fantômes et recommandés ;
        # il vit sur /performance et dans l'hebdo, pas douze fois par jour.
        msg = run_rapport._composer([_sig(1.0)], [_sig(4.0)], NOW)
        for absent in ("Performance", "IC 95%", "SOCCER —", "€", "Mise"):
            assert absent not in msg

    def test_learning_en_pied_seulement_avec_des_paris(self):
        assert run_rapport._composer([], [], NOW, learning=["seuil soccer 1.0→1.5"]) is None
        msg = run_rapport._composer([_sig(1.0)], [], NOW, learning=["seuil soccer 1.0→1.5"])
        assert "🧠" in msg and "seuil soccer" in msg

    def test_troncature_entre_deux_paris_jamais_au_milieu(self):
        beaucoup = [_sig(1.0, match=f"Club {i} Longnom vs Adversaire {i} Longnom") for i in range(80)]
        msg = run_rapport._composer(beaucoup, [], NOW)
        assert len(msg) <= 4096
        assert "tronquée" in msg
        # Chaque pari est entier : autant de lignes « → sélection » que de titres.
        assert msg.count("*Club ") == msg.count("valeur `+")


class TestFenetreDeDigest:
    def test_alignee_sur_le_cron_de_reports_yml(self):
        import re
        yml = open(".github/workflows/reports.yml", encoding="utf-8").read()
        m = re.search(r"cron: '35 \*/(\d+) \* \* \*'\s*# rapport", yml)
        assert m, "cron du rapport introuvable dans reports.yml"
        assert int(m.group(1)) == run_rapport.REPORT_WINDOW_H


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
        msg = run_rapport._composer([], [], self.T, creneau="11:03 (+101 min)")
        assert msg is not None and "11:03 (+101 min)" in msg
        assert "ops.py watchdog" in msg          # le diagnostic qui tranche la cause
        assert "Moteur muet" not in msg          # ce n'est PAS la même alerte

    def test_silence_intact_sans_creneau_manque(self):
        assert run_rapport._composer([], [], self.T, creneau=None) is None

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
