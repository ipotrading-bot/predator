"""
tests/test_rapport_signaux_a_venir.py — le digest Telegram n'annonce pas un
pari dont le match a commencé.

2026-08-28 15:20 UTC : « FK Akron Tolyatti vs CSKA Moscow · 15:00 UTC →
CSKA @ 1.39 · valeur +6.1% » — coup d'envoi vingt minutes plus tôt. Le
signal était `active` (le statut ne tombe qu'à l'audit, toutes les 6 h) et
créé dans la fenêtre de 2 h : le filtre sur `created_at` ne regarde jamais
`match_time`.
"""
from datetime import datetime, timedelta, timezone

import run_rapport

NOW = datetime(2026, 8, 28, 15, 20, tzinfo=timezone.utc)


def _sig(match_time):
    return {"match": "x", "match_time": match_time, "edge_pct": 6.1}


class TestAVenir:
    def test_un_match_commence_est_ecarte(self):
        assert run_rapport._a_venir([_sig("2026-08-28T15:00:00+00:00")], NOW) == []

    def test_un_match_a_venir_est_garde(self):
        s = _sig("2026-08-28T18:30:00Z")
        assert run_rapport._a_venir([s], NOW) == [s]

    def test_un_horodatage_naif_est_lu_en_utc(self):
        assert run_rapport._a_venir([_sig("2026-08-28T15:19:00")], NOW) == []
        assert len(run_rapport._a_venir([_sig("2026-08-28T15:21:00")], NOW)) == 1

    def test_sans_coup_denvoi_lisible_on_ne_tranche_pas(self):
        for mt in ("", None, "n'importe quoi"):
            assert len(run_rapport._a_venir([_sig(mt)], NOW)) == 1

    def test_lordre_est_conserve(self):
        a = _sig((NOW + timedelta(hours=1)).isoformat())
        b = _sig((NOW + timedelta(hours=2)).isoformat())
        assert run_rapport._a_venir([a, _sig("2026-08-28T15:00:00Z"), b], NOW) == [a, b]

    def test_kickoff_reutilise_le_meme_parseur(self):
        assert run_rapport._kickoff(_sig("2026-08-28T18:30:00Z"), NOW) == " · 18:30 UTC"
        assert run_rapport._kickoff(_sig("bidon"), NOW) == ""
