"""
tests/test_basket_apres_effectifs.py — le basket se parie APRÈS les annonces
d'effectif (2026-10-23).

Mesure (zone 2-24h, époque A6, mise plate, hors taxe) : basket 6-24h, 5
gagnés pour 10,8 annoncés par sharp_prob (z −2,68, n=19), dérive sharp contre
nous ; 2-6h, 7 pour 8,0 (z −0,54) ; foot 6-24h, 89 pour 88,7. Gardés ici :
  1. run_engine._shadow_reason : basket > T-6h → `basket_avant_effectifs`,
     borne IMPORTÉE de learning_layer (la mesure coupe au même endroit) ;
  2. run_engine._save : le prix d'OUVERTURE part à l'INSERT et n'est jamais
     réécrit ; un rescan sans raison de fantôme PROMEUT la ligne par un UPDATE
     filtré en base sur `basket_avant_effectifs` — aucune autre raison ;
  3. core.db.log_to_ledger recopie le prix d'ouverture ;
  4. learning_layer.calibration_stats / calibration_par_tranche et la ligne
     du rapport hebdo (consultatif).
"""
import inspect

import run_engine as eng
from core import db as core_db
from core import learning_layer as ll
from scripts import weekly_report

SCAN = "2026-10-23T10:00:00+00:00"


def _sig(sport="basketball", minutes=600, **extra):
    from datetime import datetime, timedelta
    mt = (datetime.fromisoformat(SCAN) + timedelta(minutes=minutes)).isoformat() if minutes is not None else ""
    d = {"match": "A vs B", "sport": sport, "edge_pct": 5.0, "scanned_at": SCAN, "match_time": mt,
         "match_id": "m1", "market_key": "totals", "status": "active",
         "sharp_prob": 0.54, "executable_odd": 1.93}
    d.update(extra)
    return d


class TestRaison:
    def test_basket_avant_6h_est_fantome_promouvable(self):
        assert eng._shadow_reason(_sig(minutes=600)) == "basket_avant_effectifs"
        assert eng._shadow_reason(_sig("euroleague_basketball", minutes=480)) == "basket_avant_effectifs"
        assert eng._shadow_reason(_sig(minutes=eng.BASKET_AVANT_EFFECTIFS_MIN + 1)) == "basket_avant_effectifs"

    def test_basket_2_6h_est_recommande(self):
        assert eng._shadow_reason(_sig(minutes=240)) is None
        assert eng._shadow_reason(_sig(minutes=eng.BASKET_AVANT_EFFECTIFS_MIN)) is None

    def test_les_autres_sports_ne_changent_pas(self):
        for sport in ("soccer", "tennis", "hockey", "americanfootball"):
            assert eng._shadow_reason(_sig(sport, minutes=600)) is None

    def test_les_raisons_existantes_priment(self):
        assert eng._shadow_reason(_sig(minutes=60)) == "t_minus_2h"
        assert eng._shadow_reason(_sig(minutes=1500)) == "hors_zone_haute"

    def test_linconnu_nest_pas_fantome(self):
        assert eng._shadow_reason(_sig(minutes=None)) is None

    def test_la_borne_est_celle_de_la_mesure(self):
        assert eng.BASKET_AVANT_EFFECTIFS_MIN == ll._TRANCHE_COURTE_MAX_MINUTES
        src = inspect.getsource(eng._shadow_reason)
        assert "BASKET_AVANT_EFFECTIFS_MIN" in src and "360" not in src

    def test_telegram_ne_compte_plus_les_ecartes(self):
        """Décision opérateur du 2026-10-29 : Telegram = les paris à jouer,
        sans « N écarté(s) »."""
        assert not hasattr(eng, "_ecartes")


class _SB:
    """Faux client : l'INSERT est refusé (ligne active existante) et chaque
    UPDATE est noté avec ses filtres ; `shadow_reason_en_base` simule la ligne
    stockée — le filtre `.eq("shadow_reason", …)` ne matche que si égal."""

    def __init__(self, shadow_reason_en_base=None, insert_ok=False):
        self.inserts, self.updates = [], []
        self.raison = shadow_reason_en_base
        self.insert_ok = insert_ok

    def table(self, t):
        sb = self

        class _Q:
            def __init__(self):
                self.op, self.champs, self.filtres = None, None, {}

            def insert(self, payload):
                self.op, self.champs = "insert", payload
                return self

            def update(self, champs):
                self.op, self.champs = "update", champs
                return self

            def eq(self, col, val):
                self.filtres[col] = val
                return self

            def execute(self):
                if self.op == "insert":
                    if sb.insert_ok:
                        sb.inserts.append(self.champs)
                        return type("R", (), {"data": [{"id": 1}]})()
                    raise RuntimeError('duplicate key value violates unique constraint "signals_active_uniq"')
                sb.updates.append((self.champs, dict(self.filtres)))
                voulu = self.filtres.get("shadow_reason", sb.raison)
                return type("R", (), {"data": [{"id": 1}] if voulu == sb.raison else []})()
        return _Q()


def _pret(minutes):
    s = _sig(minutes=minutes)
    raison = eng._shadow_reason(s)
    s["is_shadow"], s["shadow_reason"] = raison is not None, raison
    return s


class TestPersistance:
    def test_louverture_part_a_linsert(self):
        sb = _SB(insert_ok=True)
        assert eng._save(sb, _pret(600)) is True
        (ins,) = sb.inserts
        assert ins["ouverture_sharp_prob"] == 0.54 and ins["ouverture_cote"] == 1.93
        assert ins["is_shadow"] is True and ins["shadow_reason"] == "basket_avant_effectifs"

    def test_louverture_nest_jamais_reecrite(self):
        sb = _SB(shadow_reason_en_base="basket_avant_effectifs")
        eng._save(sb, _pret(240))
        rafraichi, _ = sb.updates[0]
        assert not ({"ouverture_sharp_prob", "ouverture_cote"} & set(rafraichi))
        assert {"ouverture_sharp_prob", "ouverture_cote"} <= eng._OPTIONAL_COLS

    def test_rescan_a_t_moins_4h_promeut(self):
        sb = _SB(shadow_reason_en_base="basket_avant_effectifs")
        assert eng._save(sb, _pret(240)) is True
        promo = [(c, f) for c, f in sb.updates if f.get("shadow_reason")]
        assert promo == [({"is_shadow": False, "shadow_reason": None},
                          {"status": "active", "match_id": "m1", "market_key": "totals",
                           "shadow_reason": "basket_avant_effectifs"})]

    def test_rescan_encore_avant_6h_ne_promeut_pas(self):
        sb = _SB(shadow_reason_en_base="basket_avant_effectifs")
        eng._save(sb, _pret(500))
        assert all("shadow_reason" not in f for _, f in sb.updates)

    def test_un_autre_fantome_ne_bascule_jamais(self):
        """La ligne en base est `t_minus_2h` : le filtre en base ne la
        trouve pas, rien ne bascule même si le payload est recommandé."""
        sb = _SB(shadow_reason_en_base="t_minus_2h")
        eng._save(sb, _pret(240))
        assert all(f.get("shadow_reason") in (None, "basket_avant_effectifs") for _, f in sb.updates)
        rafraichi, _ = sb.updates[0]
        assert "is_shadow" not in rafraichi and "shadow_reason" not in rafraichi

    def test_une_recommandee_ne_devient_pas_fantome(self):
        sb = _SB(shadow_reason_en_base=None)
        eng._save(sb, _pret(600))      # payload fantôme sur une ligne recommandée
        rafraichi, _ = sb.updates[0]
        assert "is_shadow" not in rafraichi and len(sb.updates) == 1


class TestLedger:
    def test_louverture_est_recopiee(self):
        cap = {}

        class _Q:
            def insert(self, payload): cap.update(payload); return self
            def execute(self): return type("R", (), {"data": []})()
            def select(self, *a): return self
            def eq(self, *a): return self
            def gte(self, *a): return self
            def in_(self, *a): return self
            def limit(self, *a): return self
            def order(self, *a, **k): return self

        class _FSB:
            def table(self, t): return _Q()
        sig = {**_sig(minutes=600), "id": 5, "xbet_odd": 1.88, "sharp_prob": 0.56,
               "ouverture_sharp_prob": 0.54, "ouverture_cote": 1.93}
        core_db.log_to_ledger(_FSB(), sig, clv=0.0, outcome="LOSS")
        assert cap["ouverture_sharp_prob"] == 0.54 and cap["ouverture_cote"] == 1.93
        assert cap["sharp_prob"] == 0.56      # le dernier prix reste celui du pari joué


def _ligne(outcome, p, ttm=600, close=None, ouverture=None, source="exchange"):
    return {"outcome": outcome, "sharp_prob": p, "time_to_match_minutes": ttm,
            "closing_pinnacle_price": close, "closing_source": source,
            "ouverture_sharp_prob": ouverture}


class TestCalibration:
    def test_z_et_attendus(self):
        rows = [_ligne("WIN", 0.5), _ligne("LOSS", 0.5), _ligne("LOSS", 0.5), _ligne("LOSS", 0.5)]
        c = ll.calibration_stats(rows)
        assert (c["n"], c["gagnes"], c["attendus"]) == (4, 1, 2.0)
        assert c["z"] == -1.0                   # (1 − 2) / √(4 × 0,25)

    def test_push_et_proba_absente_ignores(self):
        rows = [_ligne("PUSH", 0.5), _ligne("WIN", None), _ligne("WIN", 0.6)]
        assert ll.calibration_stats(rows)["n"] == 1

    def test_louverture_prime_sur_le_dernier_prix(self):
        c = ll.calibration_stats([_ligne("LOSS", 0.60, ouverture=0.50)])
        assert c["attendus"] == 0.5

    def test_derive_ignore_oracle_et_cloture_absente(self):
        rows = [_ligne("WIN", 0.5, close=1.8),            # 2,0/1,8 − 1 = +11,1 %
                _ligne("LOSS", 0.5, close=None),
                _ligne("LOSS", 0.5, close=1.5, source="oracle")]
        c = ll.calibration_stats(rows)
        assert c["derive_n"] == 1 and c["derive_moy"] == 11.11

    def test_par_tranche(self):
        rows = [_ligne("WIN", 0.5, ttm=200), _ligne("LOSS", 0.5, ttm=359),
                _ligne("LOSS", 0.5, ttm=360), _ligne("LOSS", 0.5, ttm=1440),
                _ligne("WIN", 0.5, ttm=60), _ligne("WIN", 0.5, ttm=None)]
        t = ll.calibration_par_tranche(rows)
        assert (t["2-6h"]["n"], t["2-6h"]["gagnes"]) == (2, 1)
        assert (t["6-24h"]["n"], t["6-24h"]["gagnes"]) == (2, 0)

    def test_joint_au_verdict_jamais_applique(self):
        poses = []

        class _Q:
            def upsert(self, row): poses.append(row); return self
            def execute(self): return self

        class _FSB:
            def table(self, t): return _Q()
        calib = {"basketball": {"6-24h": {"n": 19, "gagnes": 5, "attendus": 10.8,
                                          "z": -2.68, "derive_moy": -3.9, "derive_n": 19}}}
        ll._save_sport_verdicts(_FSB(), {"basketball": {"n": 19}}, {}, SCAN,
                                calib_by_sport=calib)
        import json
        assert json.loads(poses[0]["value"])["calibration"] == calib["basketball"]
        # Consultatif : ni la décision de seuil ni le moteur ne la lisent.
        assert "calibration_par_tranche" not in inspect.getsource(ll._verdict_seuil)
        assert "calibration_par_tranche" not in inspect.getsource(eng)


class TestRapport:
    def test_ligne_de_calibration(self):
        txt = weekly_report.format_calibration(
            {"2-6h": {"n": 15, "gagnes": 7, "attendus": 8.0, "z": -0.54, "derive_moy": 0.4},
             "6-24h": {"n": 19, "gagnes": 5, "attendus": 10.8, "z": -2.68, "derive_moy": -3.9}})
        assert txt == ("   calibration 2-6h 7/8.0 z -0.54 dérive +0.4% · "
                       "⚠️ 6-24h 5/10.8 z -2.68 dérive -3.9%")

    def test_vide_sans_mesure(self):
        assert weekly_report.format_calibration(None) == ""
        assert weekly_report.format_calibration({"2-6h": {"n": 0}}) == ""
