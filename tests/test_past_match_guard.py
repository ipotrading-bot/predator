"""
tests/test_past_match_guard.py — aucun signal sur un match déjà commencé.

_emit() ne filtrait que le « trop loin » (garde J+72h, filtre lineup MLB),
jamais le « déjà joué ». Constaté le 2026-08-04 dans ai_learning_ledger :
20 paris réglés avec un time_to_match_minutes NÉGATIF, jusqu'à -30 181 min
(21 jours dans le passé), tous en mma/esports/tabletennis — les sports dont
le match_time vient de la recherche web et n'est donc pas une donnée de flux
vérifiée.

Ces lignes « gagnaient » 15 fois sur 20, puisqu'on pariait sur des événements
déjà disputés : c'est ce qui donnait au MMA un 8/8 flatteur et qui gonflait de
faux gagnants la tranche golden_hour (un filtre `time_to_match_minutes < 120`
embarque tous les négatifs).

Le refus doit être SEC : aucun edge, si gros soit-il, ne rattrape un match
commencé — c'est une donnée fausse, pas une opportunité.
"""
import logging
from datetime import datetime, timedelta, timezone

import run_engine

log = logging.getLogger("test")


def _now():
    return datetime.now(timezone.utc)


def _totals_match(kickoff_hours):
    """Marché totals largement au-dessus du seuil — seul le coup d'envoi varie."""
    return {
        "id": "match-past",
        "commence_time": (_now() + timedelta(hours=kickoff_hours)).isoformat(),
        "totals_1xbet":    {"over": 2.00, "under": 1.80, "point": 2.5},
        "totals_pinnacle": {"over": 1.90, "under": 1.90, "point": 2.5},
    }


def _run(m):
    out = []
    run_engine._process_totals(m, "Arsenal vs Chelsea", "soccer", "PL", "⚽",
                               out, None, _now(), log, min_edge=1.0)
    return out


class TestMatchDejaCommence:
    def test_coup_envoi_futur_emet_normalement(self):
        # Témoin : sans ce cas, le test ci-dessous passerait même si _emit
        # refusait tout.
        assert len(_run(_totals_match(kickoff_hours=3))) == 1

    def test_coup_envoi_passe_de_peu_ne_emet_rien(self):
        assert _run(_totals_match(kickoff_hours=-2)) == []

    def test_coup_envoi_passe_de_trois_semaines_ne_emet_rien(self):
        # -30 181 min ≈ -21 jours : le pire cas réellement observé en base.
        assert _run(_totals_match(kickoff_hours=-504)) == []

    def test_match_en_cours_ne_emet_rien(self):
        # Quelques minutes après le coup d'envoi — la cote 1XBet est encore
        # servie mais la ligne Pinnacle pré-match n'a plus aucun sens.
        assert _run(_totals_match(kickoff_hours=-0.1)) == []
