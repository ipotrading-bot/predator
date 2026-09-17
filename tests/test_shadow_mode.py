"""
tests/test_shadow_mode.py — un segment fantôme est mesuré, jamais recommandé.

Le mode fantôme (SHADOW_SPORTS / SHADOW_GOLDEN_HOUR, ce dernier appliqué par
SIGNAL à moins de 2 h du coup d'envoi — le mode de run golden n'existe plus
depuis le 2026-09-03) reprend le mécanisme du
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
    def test_le_baseball_est_de_nouveau_ecarte_de_telegram(self):
        # Fantôme levé le 2026-09-01 « à réévaluer après 30 réglés
        # post-époque », REMIS le 2026-09-17 sur cette réévaluation : 14-16
        # (46,7 %) pour un point mort à 53 %, −3,56 u en mise plate, la perte
        # concentrée sur totals_under. Décision opérateur, hors taxe.
        # Les trois ligues sont aussi retirées du scan payant
        # (core.odds_api.LIGUES_RETIREES) : le fantôme couvre ce que les
        # sources soft pourraient encore présenter.
        kept, shadowed = _partition([_sig("baseball")])
        assert kept == []
        assert len(shadowed) == 1

    def test_les_autres_sports_passent(self):
        signals = [_sig("soccer"), _sig("basketball")]
        kept, shadowed = _partition(signals)
        assert len(kept) == 2
        assert shadowed == []

    def test_le_soccer_reste_recommande(self):
        # Non-régression du cas qui porte le profit : soccer 2-24h.
        kept, _ = _partition([_sig("soccer")])
        assert len(kept) == 1

    def test_partition_sans_perte(self):
        signals = [_sig("soccer"), _sig("baseball"), _sig("basketball")]
        kept, shadowed = _partition(signals)
        assert len(kept) + len(shadowed) == len(signals)


class TestConfiguration:
    def test_le_baseball_est_le_seul_sport_en_fantome(self):
        # Un sport n'entre ici que sur instruction opérateur (règle 11).
        assert run_engine.SHADOW_SPORTS == {"baseball"}

    def test_golden_hour_est_bien_fantome(self):
        assert run_engine.SHADOW_GOLDEN_HOUR is True

    def test_soccer_nest_pas_fantome(self):
        # Le soccer hors golden_hour est le principal contributeur positif
        # (95 paris, +3,22 unités sur h2h 2-24h) — le passer en fantôme
        # viderait le système.
        assert "soccer" not in run_engine.SHADOW_SPORTS
