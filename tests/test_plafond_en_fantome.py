"""
tests/test_plafond_en_fantome.py — le refus du plafond d'edge appris est un
FANTÔME mesuré, plus un DISCARD (2026-11-17).

Un signal jeté n'est ni réglé ni appris : la bande haute ne recevait plus une
ligne et le plafond ne pouvait plus tomber (basket 4,0 %, posé sur 15 lignes
dont 10 de présaison). Reconstitué depuis les logs de 961 scans et réglé contre
ESPN : 42 refus, 27-15, réussite 64 % [IC95 49–77] pour un point mort ~52 % ;
les 13 vus à T-2..6 h, 6-7. Gardés ici — RIEN ne change dans ce qui est envoyé :
  1. _emit ne jette plus : le signal sort, marqué ;
  2. _shadow_reason : `plafond_edge` APRÈS les raisons de zone, et malgré
     l'exception de la bande de cote ;
  3. hors quota : un refus du plafond ne prend la place de personne ;
  4. il ne tient jamais une famille contre un pari normal, il passe derrière ;
  5. revu sous le plafond, il est promu ; la clé privée ne part pas en base.
"""
import logging
from datetime import datetime, timedelta, timezone

import pytest

import run_engine as eng

log = logging.getLogger("test_plafond")
SCAN = datetime(2026, 11, 17, 20, 0, tzinfo=timezone.utc)


def _emettre(monkeypatch, plafond, heures=4.0):
    """Un total NBA à +6,2 % d'EV (cote 2,00, p 0,531), plafond donné."""
    monkeypatch.setattr(eng, "_EDGE_CEILINGS", {} if plafond is None else {"basketball": plafond})
    monkeypatch.setattr(eng, "_ODDS_CEILINGS", {})
    signals = []
    eng._emit(signals, None, SCAN, log, "Miami Heat vs Houston Rockets", "basketball",
              "NBA", "totals_over", "NBA Over 231.5", 2.00, 1.86, 0.531, "🏀",
              selection_name="Over 231.5", min_edge=1.5,
              match_time=(SCAN + timedelta(hours=heures)).isoformat(), match_id="m1")
    return signals


class TestEmission:
    def test_le_refus_du_plafond_nest_plus_jete(self, monkeypatch):
        (s,) = _emettre(monkeypatch, plafond=4.0)
        assert s[eng._CLE_PLAFOND] is True and s["edge_pct"] > 4.0

    def test_sous_le_plafond_rien_nest_marque(self, monkeypatch):
        (s,) = _emettre(monkeypatch, plafond=8.0)
        assert eng._CLE_PLAFOND not in s
        (s,) = _emettre(monkeypatch, plafond=None)
        assert eng._CLE_PLAFOND not in s


def _sig(minutes=240, sport="basketball", **extra):
    d = {"match": "Miami Heat vs Houston Rockets", "sport": sport, "edge_pct": 6.2,
         "scanned_at": SCAN.isoformat(),
         "match_time": (SCAN + timedelta(minutes=minutes)).isoformat(),
         "match_id": "m1", "market_key": "totals_over", "selection_name": "Over 231.5",
         "status": "active", "sharp_prob": 0.531, "executable_odd": 2.00}
    d.update(extra)
    return d


class TestRaison:
    def test_dans_la_zone_la_raison_est_le_plafond(self):
        assert eng._shadow_reason(_sig(**{eng._CLE_PLAFOND: True})) == "plafond_edge"
        assert eng._shadow_reason(_sig()) is None

    def test_les_raisons_de_zone_priment(self):
        m = {eng._CLE_PLAFOND: True}
        assert eng._shadow_reason(_sig(minutes=60, **m)) == "t_minus_2h"
        assert eng._shadow_reason(_sig(minutes=1500, **m)) == "hors_zone_haute"
        # Le basket d'avant les effectifs garde SA raison : c'est elle que la
        # promotion de T-2..6 h connaît depuis le 2026-10-23.
        assert eng._shadow_reason(_sig(minutes=600, **m)) == "basket_avant_effectifs"

    def test_la_bande_de_cote_ne_fait_pas_sortir_un_refus_du_plafond(self, monkeypatch):
        """Sous T-2h, une cote 1,50-1,75 est recommandée (2026-09-27) — pas un
        signal que le plafond refuse : avant, il était jeté."""
        monkeypatch.setattr(eng, "BANDE_COTE_PROPOSEE", True)
        dans_bande = _sig(minutes=60, sport="soccer", executable_odd=1.65)
        assert eng._shadow_reason(dans_bande) is None
        assert eng._shadow_reason({**dans_bande, eng._CLE_PLAFOND: True}) == "plafond_edge"

    def test_la_partition_le_range_chez_les_fantomes(self):
        kept, shadowed = eng._shadow_partition([_sig(), _sig(match_id="m2", **{eng._CLE_PLAFOND: True})])
        assert [s["match_id"] for s in kept] == ["m1"]
        assert shadowed[0]["is_shadow"] is True and shadowed[0]["shadow_reason"] == "plafond_edge"


class TestRienNeChangeDansLEnvoye:
    def test_hors_quota(self, monkeypatch):
        """Trié par edge, un refus du plafond passerait DEVANT les recommandés
        et en sortirait un du quota du sport."""
        monkeypatch.setitem(eng.SPORT_QUOTA, "basketball", 2)
        normaux = [_sig(match_id=f"n{i}", edge_pct=3.0 - i * 0.1) for i in range(2)]
        refus = [_sig(match_id=f"p{i}", edge_pct=7.0, **{eng._CLE_PLAFOND: True}) for i in range(3)]
        candidats, au_plafond = eng._hors_plafond(refus + normaux)
        assert candidats == normaux and au_plafond == refus
        assert eng._portfolio_balance(candidats) == normaux      # personne n'est sorti

    def test_une_ligne_plafond_ne_tient_pas_la_famille(self):
        """Avant, la ligne n'existait pas : le pari opposé vu plus tard sortait."""
        ligne = {"id": 1, "match_id": "m1", "market_key": "totals_over",
                 "selection_name": "Over 231.5", "match": "Miami Heat vs Houston Rockets",
                 "sport": "basketball", "shadow_reason": "plafond_edge"}
        oppose = _sig(market_key="totals_under", selection_name="Under 231.5")
        assert eng._arbitrer_familles([oppose], [], [ligne], log) == [oppose]
        # … alors qu'une ligne ordinaire le tient toujours.
        assert eng._arbitrer_familles([oppose], [], [{**ligne, "shadow_reason": None}], log) == []

    def test_un_refus_du_plafond_passe_derriere(self):
        tenu = {"id": 1, "match_id": "m1", "market_key": "totals_under",
                "selection_name": "Under 231.5", "match": "Miami Heat vs Houston Rockets",
                "sport": "basketball", "shadow_reason": None}
        refus = _sig(**{eng._CLE_PLAFOND: True})
        assert eng._arbitrer_familles([], [refus], [tenu], log) == []
        # … et derrière un signal normal du MÊME scan.
        normal = _sig(market_key="totals_under", selection_name="Under 231.5")
        assert eng._arbitrer_familles([normal], [refus], [], log) == [normal]

    def test_un_refus_seul_sur_son_match_est_garde(self):
        refus = _sig(**{eng._CLE_PLAFOND: True})
        assert eng._arbitrer_familles([], [refus], [], log) == [refus]

    def test_les_actifs_sont_lus_avec_leur_raison(self):
        import inspect
        assert "shadow_reason" in inspect.getsource(eng._actifs_des_matchs)


class _SB:
    """INSERT refusé (ligne active), UPDATE noté ; la promotion ne matche que
    si la raison stockée est dans la liste demandée."""

    def __init__(self, raison_en_base=None, insert_ok=False):
        self.inserts, self.updates, self.raison, self.insert_ok = [], [], raison_en_base, insert_ok

    def table(self, _t):
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

            def in_(self, col, vals):
                self.filtres[col] = tuple(vals)
                return self

            def execute(self):
                if self.op == "insert":
                    if sb.insert_ok:
                        sb.inserts.append(self.champs)
                        return type("R", (), {"data": [{"id": 1}]})()
                    raise RuntimeError('duplicate key value violates unique constraint "signals_active_uniq"')
                sb.updates.append((self.champs, dict(self.filtres)))
                voulues = self.filtres.get("shadow_reason", (sb.raison,))
                return type("R", (), {"data": [{"id": 1}] if sb.raison in voulues else []})()
        return _Q()


def _pret(**extra):
    s = _sig(**extra)
    raison = eng._shadow_reason(s)
    s["is_shadow"], s["shadow_reason"] = raison is not None, raison
    return s


class TestPersistance:
    def test_la_cle_privee_ne_part_pas_en_base(self):
        sb = _SB(insert_ok=True)
        assert eng._save(sb, _pret(**{eng._CLE_PLAFOND: True})) is True
        (ins,) = sb.inserts
        assert eng._CLE_PLAFOND not in ins
        assert ins["is_shadow"] is True and ins["shadow_reason"] == "plafond_edge"

    def test_revu_sous_le_plafond_il_est_promu(self):
        """Avant, le signal revu sous le plafond sortait neuf et recommandé."""
        sb = _SB(raison_en_base="plafond_edge")
        assert eng._save(sb, _pret()) is True
        promo = [c for c, f in sb.updates if "shadow_reason" in f]
        assert promo == [{"is_shadow": False, "shadow_reason": None}]
        assert "plafond_edge" in eng._RAISONS_PROMOUVABLES

    def test_encore_au_dessus_il_reste_fantome(self):
        sb = _SB(raison_en_base="plafond_edge")
        eng._save(sb, _pret(**{eng._CLE_PLAFOND: True}))
        assert all("shadow_reason" not in f for _, f in sb.updates)
        rafraichi, _ = sb.updates[0]
        assert eng._CLE_PLAFOND not in rafraichi and "is_shadow" not in rafraichi

    def test_une_recommandee_passee_au_dessus_reste_recommandee(self):
        """Déjà envoyée, peut-être jouée : le drapeau est figé à l'insertion."""
        sb = _SB(raison_en_base=None)
        eng._save(sb, _pret(**{eng._CLE_PLAFOND: True}))
        assert len(sb.updates) == 1 and "is_shadow" not in sb.updates[0][0]

    @pytest.mark.parametrize("raison", ["t_minus_2h", "shadow_sport", "hors_zone_haute"])
    def test_les_autres_fantomes_ne_basculent_jamais(self, raison):
        assert raison not in eng._RAISONS_PROMOUVABLES
