"""
tests/test_telegram_format.py — Telegram, trois messages simples (2026-10-29).

Décision opérateur du 2026-10-29 : « Telegram : les signaux à jouer, les
résultats après chaque audit, les gains/pertes du jour précédent, par exemple
22/11 4 gagnés 1 perdu taux 80 %. Simple, lisible. » Tout le reste a quitté
Telegram (digest 2 h, résumé d'apprentissage, alertes, hebdo).

Gardés ici, sans aucun envoi réseau :
  1. le message des paris : match, heure, sélection, cote, book — ni valeur,
     ni favori, ni tranche, ni mise ; SILENCE quand rien n'est à jouer ;
  2. les résultats de l'audit : un pari par ligne, ✅ ❌ ↩️ ;
  3. le bilan de la veille au format demandé, une seule fois par jour.
"""
from datetime import datetime, timezone

import pytest

import run_engine
from core import telegram_bilan as tb

NOW = datetime(2026, 7, 21, 18, 8, tzinfo=timezone.utc)


def _sig(**over) -> dict:
    base = {
        "match": "Real Madrid vs Barcelona", "sport": "soccer",
        "league": "La Liga", "market_key": "h2h", "market": "AH 0.0",
        "selection_name": "Barcelona", "executable_odd": 3.40, "sharp_prob": 0.31,
        "edge_pct": 5.4, "risk_flag": "HIGH_VALUE", "soft_book": "1xbet",
        "match_time": "2026-07-21T21:00:00+00:00", "kelly_pct": 2.5,
    }
    base.update(over)
    return base


@pytest.fixture
def sent(monkeypatch):
    box: list[str] = []
    monkeypatch.setattr(run_engine, "_telegram", box.append)
    return box


class TestParisAJouer:
    def test_rien_a_jouer_rien_n_est_envoye(self, sent):
        run_engine._telegram_signals([], NOW, matches=42, no_pin_count=3,
                                     shadowed=[{"shadow_reason": "t_minus_2h"}])
        assert sent == []

    def test_l_en_tete_dit_combien_de_paris_et_rien_d_autre(self, sent):
        run_engine._telegram_signals([_sig()], NOW, matches=42, no_pin_count=3)
        assert len(sent) == 1
        en_tete = sent[0].splitlines()[0]
        assert "1 pari(s) à jouer" in en_tete
        for bruit in ("matchs analysés", "écarté", "Pinnacle"):
            assert bruit not in sent[0]

    def test_un_pari_le_match_l_heure_la_selection_la_cote_le_book(self):
        bloc = run_engine._signal_block(_sig(), NOW)
        assert "Real Madrid vs Barcelona" in bloc and "21:00" in bloc
        assert "→ Barcelona @ 3.40 · 1xbet" in bloc

    @pytest.mark.parametrize("bruit", ["valeur", "Favori", "favori", "tranche", "Mise",
                                       "Kelly", "€", "%"])
    def test_ni_valeur_ni_favori_ni_mise(self, bruit):
        assert bruit not in run_engine._signal_block(_sig(executable_odd=1.62), NOW)

    def test_markdown_equilibre(self):
        bloc = run_engine._signal_block(_sig(match="A_b vs C"), NOW)
        assert bloc.count("*") % 2 == 0 and bloc.count("`") % 2 == 0

    def test_une_longue_liste_est_coupee_entre_deux_paris(self, sent):
        paris = [_sig(match=f"Equipe {i} vs Autre {i}") for i in range(80)]
        run_engine._telegram_signals(paris, NOW, matches=200, no_pin_count=0)
        assert len(sent) > 1 and all(len(m) <= 4096 for m in sent)
        assert sum(m.count("→ ") for m in sent) == 80


class TestResultatsDeLAudit:
    def test_un_pari_par_ligne(self):
        texte = tb.message_resultats([
            {"match": "Lyon vs Nice", "selection": "Over 2.5", "odds": 1.85, "outcome": "WIN"},
            {"match": "Real vs Getafe", "selection": "Real", "odds": "1.62", "outcome": "LOSS"},
            {"match": "A vs B", "selection": "A", "odds": 1.9, "outcome": "PUSH"},
        ])
        assert texte.splitlines() == ["📋 Résultats",
                                      "✅ Lyon vs Nice — Over 2.5 @ 1.85",
                                      "❌ Real vs Getafe — Real @ 1.62",
                                      "↩️ A vs B — A @ 1.90 (remboursé)"]

    def test_rien_de_regle_rien_n_est_envoye(self):
        assert tb.message_resultats([]) is None
        assert tb.message_resultats([{"match": "A vs B", "outcome": "expired"}]) is None


class TestBilanDeLaVeille:
    JOUR = datetime(2026, 11, 22, tzinfo=timezone.utc)

    def test_le_format_demande(self):
        assert tb.message_bilan(self.JOUR, 4, 1) == "📊 22/11 · 4 gagnés · 1 perdu · taux 80 %"

    def test_rembourses_et_attente_seulement_s_il_y_en_a(self):
        assert tb.message_bilan(self.JOUR, 1, 2, rembourses=1, en_attente=2) == \
            "📊 22/11 · 1 gagné · 2 perdus · taux 33 % · 1 remboursé · 2 en attente"

    def test_une_journee_sans_pari_ne_fait_pas_de_message(self):
        assert tb.message_bilan(self.JOUR, 0, 0) is None

    class _SB:
        """meta (dernier bilan), signals (paris d'hier), ledger (issues)."""

        def __init__(self, deja=None):
            self.meta = {} if deja is None else {tb.BILAN_KEY: deja}
            self.t = None

        def table(self, nom):
            self.t, self.f = nom, {}
            return self

        def select(self, *_a, **_k): return self
        def eq(self, c, v): self.f[c] = v; return self
        def gte(self, *_a): return self
        def lt(self, *_a): return self
        def in_(self, *_a): return self
        def limit(self, *_a): return self

        def upsert(self, row, **_k):
            self.meta[row["key"]] = row["value"]
            return self

        def execute(self):
            if self.t == "meta":
                v = self.meta.get(tb.BILAN_KEY)
                return type("R", (), {"data": [{"value": v}] if v else []})()
            if self.t == "signals":
                return type("R", (), {"data": [{"id": i, "status": "settled"} for i in range(5)]
                                       + [{"id": 9, "status": "active"}]})()
            issues = ["WIN"] * 4 + ["LOSS"]
            return type("R", (), {"data": [{"signal_id": i, "outcome": o}
                                           for i, o in enumerate(issues)]})()

    def test_une_seule_fois_par_jour_et_pas_avant_six_heures(self, monkeypatch):
        envois = []
        monkeypatch.setattr(tb, "envoyer", lambda t: envois.append(t) or True)
        sb = self._SB()
        assert tb.annoncer_bilan_veille(sb, datetime(2026, 11, 23, 3, 45, tzinfo=timezone.utc)) is False
        assert tb.annoncer_bilan_veille(sb, datetime(2026, 11, 23, 6, 45, tzinfo=timezone.utc)) is True
        assert tb.annoncer_bilan_veille(sb, datetime(2026, 11, 23, 9, 45, tzinfo=timezone.utc)) is False
        assert envois == ["📊 22/11 · 4 gagnés · 1 perdu · taux 80 % · 1 en attente"]

    def test_un_envoi_rate_repart_au_passage_suivant(self, monkeypatch):
        monkeypatch.setattr(tb, "envoyer", lambda t: False)
        sb = self._SB()
        assert tb.annoncer_bilan_veille(sb, datetime(2026, 11, 23, 6, 45, tzinfo=timezone.utc)) is False
        assert tb.BILAN_KEY not in sb.meta

    def test_texte_brut_jamais_de_markdown(self):
        import inspect
        assert "parse_mode" not in inspect.getsource(tb.envoyer)


class TestPlusRienDAutre:
    def test_le_rapport_toutes_les_2h_est_muet(self):
        import inspect
        import run_rapport
        src = inspect.getsource(run_rapport)
        assert "sendMessage" not in src and "_send(" not in src

    def test_l_audit_n_envoie_que_resultats_et_bilan(self):
        import inspect
        import core.audit_engine as ae
        src = inspect.getsource(ae)
        assert "sendMessage" not in src
        assert "annoncer_resultats" in src and "annoncer_bilan_veille" in src

    def test_le_scan_n_envoie_que_les_paris(self):
        import inspect
        import re
        src = inspect.getsource(run_engine.run)
        assert not re.search(r"(?<![\w])_telegram\(", src), \
            "le scan ne parle que par _telegram_signals"
