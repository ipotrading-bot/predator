"""
tests/test_incident_2026_09_15.py — audit des 72 h du 12 au 15/09/2026.

INCIDENTS.md, « Audit 72 h : faux règlement Juvenil, paris opposés, cliquet
des seuils, rapport hebdo perdu (2026-09-15) ». Un test par défaut mesuré :

1. Valladolid–Real Sociedad (Juvenil, U19) réglé sur Valladolid–Oviedo
   (seniors) : « juvenil » inconnu du filtre jeunes, et « Real Sociedad » ≈
   « Real Oviedo » pour `strict_team_match`.
2. Santa Cruz 0.0 puis Recoleta +0.5 recommandés sur le même match, à deux
   scans d'écart ; Shelbourne en h2h ET en handicap 0.0.
3. `threshold_seg_baseball_totals` +0,4 à chaque audit sur le même n=20.
4. Plafond d'edge soccer posé sur n=5, sur une bande qui gagnait.
5. Taux de réussite nus dans le résumé d'apprentissage (règle 7) ; retrait
   proposé d'un sport positif dont l'intervalle chevauchait le point mort.
6. Rapport hebdo refusé par Telegram (`_` de SUSPECT_DATA), job vert ; CLV
   affiché ×100 ; n du rapport ≠ n du verdict.
"""
import importlib
import inspect
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

import run_engine
from core import learning_layer as ll
from core.paim_engine import nom_avec_etage, section_jeunes, strict_team_match
from scripts import weekly_report as w

log = logging.getLogger("test")


# ── 1. Faux règlement Juvenil ────────────────────────────────────────────

class TestJuvenilEtNoms:
    @pytest.mark.parametrize("ligue, etage", [
        ("Spain - Division de Honor Juvenil - Grupo II", "u19"),   # le cas réel
        ("Germany - A-Junioren Bundesliga", "u19"),
        ("Portugal - Juniores A", "u19"),
        ("Brazil - Brasileiro Sub-20", "u20"),
        ("Spain - LaLiga", ""),
        ("Colombia - Primera A", ""),
    ])
    def test_categorie_portee_par_la_ligue(self, ligue, etage):
        assert section_jeunes(ligue) == etage

    def test_le_cas_reel_ne_s_apparie_plus_aux_seniors(self):
        nom = nom_avec_etage("Real Valladolid CF vs Real Sociedad",
                             "Spain - Division de Honor Juvenil - Grupo II")
        home, away = nom.split(" vs ")
        assert not strict_team_match(home, "Real Valladolid")
        assert not strict_team_match(away, "Real Oviedo")

    @pytest.mark.parametrize("a, b", [
        ("Real Sociedad", "Real Oviedo"),            # le cas réel, ratio 0,75
        ("Sheffield United", "Sheffield Wednesday"),
        ("Manchester United", "Newcastle United"),
        ("Atletico Nacional", "Atletico Tucuman"),
        ("AS Monaco", "AS Roma"),
        ("Los Angeles Angels", "Los Angeles Dodgers"),
    ])
    def test_un_mot_commun_ne_fait_plus_un_meme_club(self, a, b):
        assert not strict_team_match(a, b)

    @pytest.mark.parametrize("a, b", [
        ("CSD Macara", "Deportivo Macara"),          # sigle : ratio global, comme avant
        ("Brighton & Hove Albion", "Brighton and Hove Albion"),
        ("Como 1907", "Como"),
        ("Le Havre AC", "Le Havre"),
        ("Borussia Mönchengladbach", "Borussia M'gladbach"),
        ("Bayern Munchen", "Bayern Munich"),
        ("Olympiakos Piraeus", "Olympiacos Piraeus"),
    ])
    def test_les_ecritures_d_un_meme_club_restent_appariees(self, a, b):
        assert strict_team_match(a, b)

    def test_la_regle_ne_fait_que_refuser(self):
        """Mesuré sur 972 noms réels : laissée seule, la comparaison des
        restes créait « San Martin San Juan » ≈ « San Martín Burzaco »."""
        assert not strict_team_match("San Martin San Juan", "San Martín Burzaco")


# ── 2. Paris opposés d'un scan à l'autre ─────────────────────────────────

_SC = "CD Santa Cruz vs Deportes Recoleta"


def _sig(mk, sel, mid="oai_68098120", match=_SC):
    return {"match_id": mid, "market_key": mk, "selection_name": sel, "match": match}


class TestParisOpposes:
    def test_le_cas_santa_cruz(self):
        actif = _sig("spreads_home", "CD Santa Cruz 0.0")
        nouveau = _sig("spreads_away", "Deportes Recoleta +0.5")
        assert run_engine._sans_contradiction([nouveau], [actif], log) == []

    def test_revoir_le_meme_pari_passe(self):
        actif = _sig("spreads_home", "CD Santa Cruz 0.0")
        revu = _sig("spreads_home", "CD Santa Cruz -0.25")
        assert run_engine._sans_contradiction([revu], [actif], log) == [revu]

    def test_h2h_et_handicap_du_meme_camp_sont_le_meme_pari(self):
        m = "Shelbourne FC vs Drogheda United FC"
        h2h = _sig("h2h", "Shelbourne FC", "oai_73888520", m)
        ah0 = _sig("spreads_home", "Shelbourne FC 0.0", "oai_73888520", m)
        assert run_engine._sans_contradiction([h2h, ah0], [], log) == [h2h]

    def test_un_h2h_qui_change_de_camp_sous_la_meme_cle_est_refuse(self):
        m = "Real Valladolid CF vs Real Sociedad"
        actif = _sig("h2h", "Real Valladolid CF", "x", m)
        bascule = _sig("h2h", "Real Sociedad", "x", m)
        assert run_engine._sans_contradiction([bascule], [actif], log) == []

    def test_total_et_cote_d_un_meme_match_coexistent(self):
        total = _sig("totals_over", "Over 2.5")
        cote = _sig("spreads_home", "CD Santa Cruz 0.0")
        assert run_engine._sans_contradiction([total, cote], [], log) == [total, cote]

    def test_deux_matchs_differents_ne_se_genent_pas(self):
        a = _sig("spreads_home", "CD Santa Cruz 0.0")
        b = _sig("spreads_away", "Deportes Recoleta +0.5", mid="autre")
        assert run_engine._sans_contradiction([b], [a], log) == [b]

    def test_une_lecture_en_panne_ouvre_la_garde_sans_lever(self):
        class _SB:
            def table(self, _n):
                raise RuntimeError("réseau")
        assert run_engine._actifs_des_matchs(_SB(), [_sig("h2h", "x")], log) == []

    def test_la_garde_passe_avant_le_partage_fantome(self):
        src = Path(run_engine.__file__).read_text(encoding="utf-8")
        assert (src.index("signals = _sans_contradiction(")
                < src.index("recommandes, shadowed = _shadow_partition(signals)"))


# ── 3-5. Couche d'apprentissage ──────────────────────────────────────────

def _row(outcome, odds=2.0, market_type=None, initial_edge=None):
    return {"outcome": outcome, "kelly_pct": 10.0, "odds": odds,
            "market_type": market_type, "initial_edge": initial_edge,
            "sharp_prob": None, "clv_pct_real": None,
            "created_at": "2026-09-12T12:00:00+00:00"}


class _Res:
    def __init__(self, data):
        self.data = data


class _Ledger:
    def __init__(self, rows_by_sport):
        self.rows_by_sport, self.sport = rows_by_sport, None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        if col == "sport":
            self.sport = val
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        return _Res(list(self.rows_by_sport.get(self.sport, [])))


class _Meta:
    """`meta` qui PERSISTE : ce qu'un audit écrit, le suivant le relit."""

    def __init__(self, store):
        self.store, self.prefix, self.key, self.suppr = store, None, None, False

    def select(self, *_a, **_k):
        return self

    def like(self, _c, motif):
        self.prefix = motif.rstrip("%")
        return self

    def eq(self, _c, val):
        self.key = val
        return self

    def limit(self, *_a, **_k):
        return self

    def upsert(self, payload, **_k):
        self.store[payload["key"]] = payload["value"]
        return self

    def delete(self):
        self.suppr = True
        return self

    def execute(self):
        if self.suppr:
            self.store.pop(self.key, None)
            return _Res(None)
        if self.prefix is not None:
            return _Res([{"key": k, "value": v} for k, v in self.store.items()
                         if k.startswith(self.prefix)])
        if self.key is not None:
            return _Res([{"key": self.key, "value": self.store[self.key]}]
                        if self.key in self.store else [])
        return _Res([])


class _SB:
    def __init__(self, rows_by_sport, store):
        self.rows_by_sport, self.store = rows_by_sport, store

    def table(self, name):
        if name == "ai_learning_ledger":
            return _Ledger(self.rows_by_sport)
        if name == "meta":
            return _Meta(self.store)
        raise AssertionError(f"table inattendue : {name}")


class TestCliquet:
    def test_un_seuil_ne_bouge_pas_deux_fois_sur_le_meme_echantillon(self):
        rows = [_row("LOSS", market_type="totals") for _ in range(ll._MIN_SAMPLES)]
        store: dict = {}
        sb = _SB({"soccer": rows}, store)
        ll.compute_and_save(sb)
        sport, segment = store["threshold_soccer"], store["threshold_seg_soccer_totals"]
        for _ in range(3):                      # trois audits de plus, rien de neuf
            ll.compute_and_save(sb)
        assert store["threshold_soccer"] == sport
        assert store["threshold_seg_soccer_totals"] == segment

    def test_une_ligne_neuve_rouvre_la_decision(self):
        rows = [_row("LOSS") for _ in range(ll._MIN_SAMPLES)]
        store: dict = {}
        sb = _SB({"soccer": rows}, store)
        ll.compute_and_save(sb)
        premier = float(store["threshold_soccer"])
        rows.append(_row("LOSS"))
        ll.compute_and_save(sb)
        assert float(store["threshold_soccer"]) > premier


def _bandes(gagnes_bas, n_bas, gagnes_haut, n_haut):
    return ([_row("WIN", initial_edge=1.0)] * gagnes_bas
            + [_row("LOSS", initial_edge=1.0)] * (n_bas - gagnes_bas)
            + [_row("WIN", initial_edge=9.0)] * gagnes_haut
            + [_row("LOSS", initial_edge=9.0)] * (n_haut - gagnes_haut))


class TestPlafondEtBandes:
    def test_une_bande_haute_gagnante_ne_pose_aucun_plafond(self):
        # 10/14 (71 %) pour 50 % requis : moins bonne que la basse, mais gagnante.
        rows = _bandes(18, 20, 10, 14)
        fake, plafond, _n, _b = ll._top_band_verdict(rows)
        assert fake is False and plafond is None
        assert ll._edge_band_diagnostic("soccer", rows) is None

    def test_cinq_paris_ne_posent_pas_de_plafond(self):
        rows = _bandes(12, 15, 0, 5)             # bande haute 0/5 : n trop court
        assert ll._top_band_verdict(rows)[1] is None


class TestJamaisUnTauxNu:
    def test_les_raisons_de_seuil(self):
        stats = ll._sport_stats([_row("WIN")] * 10 + [_row("LOSS")] * 10)
        _t, raison = ll._decide_threshold(3.0, stats, ll._clv_stats([]), False)
        assert "IC95" in raison and "point mort" in raison
        assert "win rate" not in raison

    def test_le_diagnostic_de_bandes(self):
        msg = ll._edge_band_diagnostic("soccer", _bandes(13, 15, 3, 15))
        assert msg and "IC95" in msg and "point mort" in msg

    def test_un_intervalle_qui_chevauche_ne_propose_pas_de_retrait(self):
        v = ll.sport_verdict({"n": 41, "hit_rate": 0.68, "wilson_lower": 0.555,
                              "wilson_upper": 0.824, "p_breakeven": 0.577,
                              "roi": -0.039, "pnl_flat": 9.86})
        assert v["status"] == "non_demontre" and v["retrait_propose"] is False


def test_les_logs_d_apprentissage_sortent_dans_le_job_d_audit():
    importlib.import_module("core.audit_engine")      # pose les handlers à l'import
    assert logging.getLogger("LEARN").handlers


# ── 6. Rapport hebdo ─────────────────────────────────────────────────────

def _ligne(outcome):
    return {"outcome": outcome, "kelly_pct": 0.5, "odds": 1.9, "market_type": "h2h",
            "initial_edge": 3.0, "sharp_prob": 0.55, "clv_pct_real": 4.34,
            "time_to_match_minutes": 300}


_NOW = datetime(2026, 9, 14, 13, 43, tzinfo=timezone.utc)


class TestRapportHebdo:
    def test_le_clv_est_deja_en_points(self):
        assert w._pts(4.34) == "+4.34%"

    def test_aucun_underscore_nu_pour_telegram(self):
        m = w.sport_truth_metrics([_ligne("WIN")] * 12 + [_ligne("LOSS")] * 8)
        texte = w.format_report({"soccer": m}, {}, (0, 503), _NOW)
        assert not re.search(r"(?<!\\)_", texte), texte

    def test_les_chiffres_du_verdict_priment(self):
        m = w.sport_truth_metrics([_ligne("WIN")] * 40 + [_ligne("LOSS")] * 17)
        v = {"n": 39, "hit_rate": 0.68, "wilson_lower": 0.53, "wilson_upper": 0.81,
             "p_breakeven": 0.58, "status": "non_demontre", "retrait_propose": False,
             "avg_clv": 2.0, "clv_n": 30, "roi": 0.01, "pnl_flat": 5.0}
        texte = w.format_report({"soccer": m}, {"soccer": v}, (0, 1), _NOW)
        assert "n=39 réglés" in texte and "n=57 réglés" not in texte
        assert "+434" not in texte

    def test_un_refus_de_mise_en_forme_repart_en_texte_brut(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
        envois = []

        class _R:
            def __init__(self, code, text):
                self.status_code, self.text = code, text

        def post(url, json, timeout):
            envois.append(json)
            if "parse_mode" in json:
                return _R(400, "Bad Request: can't parse entities: byte offset 1056")
            return _R(200, "ok")
        monkeypatch.setattr(w.requests, "post", post)
        assert w._send("SUSPECT_DATA") is True
        assert len(envois) == 2 and "parse_mode" not in envois[1]

    def test_un_envoi_perdu_fait_echouer_le_job(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
        monkeypatch.setattr(w.requests, "post",
                            lambda url, json, timeout: type("R", (), {"status_code": 500, "text": "x"})())
        assert w._send("x") is False
        assert "return 0 if _send(text) else 1" in inspect.getsource(w.main)
