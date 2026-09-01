"""
tests/test_shadow_mode.py — un segment fantôme est mesuré, jamais recommandé.

Le mode fantôme (SHADOW_SPORTS / SHADOW_GOLDEN_HOUR) reprend le mécanisme du
disjoncteur par sport : le signal est persisté, réglé et appris comme les
autres, seule la recommandation Telegram est retirée. La distinction compte —
couper le cron aurait aussi arrêté la collecte, et on n'aurait jamais su si le
segment se redresse.

Ce qui est vérifié ici, c'est la SÉLECTION (qui est écarté de l'envoi), pas
l'envoi lui-même : run() exige Supabase et le réseau. La règle a donc été
extraite dans _shadow_partition(), qui est la fonction réellement appelée par
run() — on teste le code de production, pas une copie.
"""
import run_engine

_partition = run_engine._shadow_partition


def _sig(sport, match="A vs B"):
    return {"match": match, "sport": sport, "edge_pct": 5.0}


class TestSegmentsFantomes:
    def test_baseball_nest_plus_ecarte_de_telegram(self):
        # Fantôme levé le 2026-09-01 (décision opérateur) : la mesure du
        # 2026-08-04 datait de l'ancien moteur, avant la refonte EV.
        kept, shadowed = _partition([_sig("baseball")], golden_hour=False)
        assert len(kept) == 1
        assert shadowed == []

    def test_les_autres_sports_passent(self):
        signals = [_sig("soccer"), _sig("basketball")]
        kept, shadowed = _partition(signals, golden_hour=False)
        assert len(kept) == 2
        assert shadowed == []

    def test_golden_hour_ecarte_tout_y_compris_les_sports_sains(self):
        # Le fantôme golden_hour porte sur la FENÊTRE, pas sur le sport :
        # à T-2h le soccer perd aussi (39% global sur la tranche).
        signals = [_sig("soccer"), _sig("basketball"), _sig("baseball")]
        kept, shadowed = _partition(signals, golden_hour=True)
        assert kept == []
        assert len(shadowed) == 3

    def test_hors_golden_hour_le_soccer_reste_recommande(self):
        # Non-régression du cas qui porte le profit : soccer 2-24h.
        kept, _ = _partition([_sig("soccer")], golden_hour=False)
        assert len(kept) == 1

    def test_partition_sans_perte(self):
        signals = [_sig("soccer"), _sig("baseball"), _sig("basketball")]
        kept, shadowed = _partition(signals, golden_hour=False)
        assert len(kept) + len(shadowed) == len(signals)


class TestConfiguration:
    def test_aucun_sport_en_fantome(self):
        assert run_engine.SHADOW_SPORTS == set()

    def test_golden_hour_est_bien_fantome(self):
        assert run_engine.SHADOW_GOLDEN_HOUR is True

    def test_soccer_nest_pas_fantome(self):
        # Le soccer hors golden_hour est le principal contributeur positif
        # (95 paris, +3,22 unités sur h2h 2-24h) — le passer en fantôme
        # viderait le système.
        assert "soccer" not in run_engine.SHADOW_SPORTS
