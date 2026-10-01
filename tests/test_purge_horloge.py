"""
tests/test_purge_horloge.py — une horloge par signal, jamais deux (2026-10-01).

La purge appliquait à tout signal actif DEUX délais : 48 h après le coup
d'envoi (96 h en famine de settlement) ET 48 h après l'ÉMISSION. Le second
gagnait toujours sur un signal émis tôt. Depuis que la zone jouable va jusqu'à
T-24 h (2026-09-22), il coupait le règlement ~24 h après le coup d'envoi, sous
les 36 h de `audit_engine.EXPIRE_AFTER_H`, et il ignorait la famine.

VÉCU : signal 10547 (WTA Pékin, Over 21.5), émis le 29/09 à 11:21 pour un
match annoncé le 30/09 à 02:00 et JOUÉ le 01/10 vers 09:00. Expiré par le
reprice de 12:31 — 49 h après l'émission — alors qu'ESPN publiait le score
depuis 11:00, que la famine était posée depuis 10:27, et que le log du même
run annonçait « fenêtre portée à 96 h pour ne pas détruire d'échantillon ».

Contrat gardé ici :
  1. un signal DATÉ ne se purge que sur son coup d'envoi (48 h / 96 h) ;
  2. un signal sans date lisible garde la règle d'âge (48 h après émission) ;
  3. on archive et on supprime EXACTEMENT les mêmes lignes ;
  4. toute suppression reste scopée `status='active'`.
"""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import run_engine as eng
from core.audit_engine import EXPIRE_AFTER_H

NOW = datetime(2026, 10, 1, 12, 31, tzinfo=timezone.utc)


def _iso(heures_avant: float) -> str:
    return (NOW - timedelta(hours=heures_avant)).isoformat()


def _sig(emis_h, coup_h=None, id_=1):
    """Signal émis il y a `emis_h` heures, coup d'envoi il y a `coup_h`."""
    return {"id": id_, "created_at": _iso(emis_h),
            "match_time": _iso(coup_h) if coup_h is not None else None}


class TestUneHorlogeParSignal:
    def test_le_signal_10547_reste_actif(self):
        """Émis il y a 49 h 10, coup d'envoi (annoncé) il y a 34 h 31."""
        sig = {"id": 10547, "created_at": "2026-09-29T11:21:00+00:00",
               "match_time": "2026-09-30T02:00:00+00:00"}
        assert eng._perime(sig, NOW, 48) is False
        assert eng._perime(sig, NOW, 96) is False

    def test_un_signal_date_suit_son_coup_d_envoi(self):
        assert eng._perime(_sig(emis_h=60, coup_h=47), NOW, 48) is False
        assert eng._perime(_sig(emis_h=60, coup_h=49), NOW, 48) is True

    def test_la_famine_double_la_fenetre_du_signal_date(self):
        assert eng._perime(_sig(emis_h=100, coup_h=80), NOW, 96) is False
        assert eng._perime(_sig(emis_h=120, coup_h=97), NOW, 96) is True

    def test_un_match_a_venir_n_est_jamais_perime(self):
        """Émis depuis trois jours pour un match de demain : l'ancienne règle
        d'âge le détruisait avant le coup d'envoi."""
        assert eng._perime(_sig(emis_h=72, coup_h=-20), NOW, 48) is False

    def test_sans_date_lisible_la_regle_d_age_demeure(self):
        for illisible in (None, "", "bientôt"):
            vieux = {"id": 1, "created_at": _iso(49), "match_time": illisible}
            jeune = {"id": 2, "created_at": _iso(47), "match_time": illisible}
            assert eng._perime(vieux, NOW, 48) is True
            assert eng._perime(vieux, NOW, 96) is True      # la famine ne le sauve pas
            assert eng._perime(jeune, NOW, 48) is False

    def test_rien_de_datable_on_ne_devine_pas(self):
        assert eng._perime({"id": 1}, NOW, 48) is False

    def test_la_fenetre_laisse_l_audit_finir(self):
        """Un signal daté survit au-delà d'EXPIRE_AFTER_H, quel que soit son
        âge : c'est l'audit qui le rend terminal, pas la purge."""
        limite = _sig(emis_h=EXPIRE_AFTER_H + 24, coup_h=EXPIRE_AFTER_H + 1)
        assert eng._perime(limite, NOW, 48) is False


# ── La purge de bout en bout, sur une base factice ───────────────────────

class _Requete:
    def __init__(self, base, table):
        self.base, self.table, self.op, self.filtres = base, table, None, []

    def select(self, *_a):
        self.op = "select"
        return self

    def delete(self):
        self.op = "delete"
        return self

    def __getattr__(self, nom):
        def poser(*args):
            self.filtres.append((nom, args))
            return self
        return poser

    def execute(self):
        self.base.appels.append((self.table, self.op, list(self.filtres)))
        donnees = []
        if self.table == "signals" and self.op == "select":
            donnees = list(self.base.actifs)
        elif self.table == "signals" and self.op == "delete":
            ids = [a[1] for n, a in self.filtres if n == "in_" and a[0] == "id"]
            if ids:
                donnees = [{"id": i} for i in ids[0]]
        return type("R", (), {"data": donnees})()


class _Base:
    def __init__(self, actifs):
        self.actifs, self.appels = actifs, []

    def table(self, nom):
        return _Requete(self, nom)


@pytest.fixture
def purge(monkeypatch):
    archives: list = []
    monkeypatch.setattr(eng, "_archive_before_purge",
                        lambda sb, sigs: archives.extend(s["id"] for s in sigs))

    def lancer(actifs, famine=False):
        monkeypatch.setattr(eng, "_settlement_affame", lambda sb: famine)
        base = _Base(actifs)
        eng._purge_old_signals(base)
        supprimes = [list(a[1]) for t, op, f in base.appels if t == "signals" and op == "delete"
                     for n, a in f if n == "in_" and a[0] == "id"]
        return archives, (supprimes[0] if supprimes else []), base
    return lancer


def _reel(emis_h, coup_h, id_):
    maintenant = datetime.now(timezone.utc)
    return {"id": id_, "xbet_odd": 1.9, "pinnacle_price": 1.8,
            "created_at": (maintenant - timedelta(hours=emis_h)).isoformat(),
            "match_time": (maintenant - timedelta(hours=coup_h)).isoformat()
            if coup_h is not None else None}


class TestLaPurgeArchiveCeQuElleSupprime:
    def test_le_signal_emis_tot_n_est_ni_archive_ni_supprime(self, purge):
        archives, supprimes, _ = purge([_reel(49, 34, 10547)])
        assert archives == [] and supprimes == []

    def test_les_memes_lignes_des_deux_cotes(self, purge):
        actifs = [_reel(60, 50, 1), _reel(49, 34, 2), _reel(55, None, 3), _reel(80, 70, 4)]
        archives, supprimes, _ = purge(actifs)
        assert sorted(archives) == sorted(supprimes) == [1, 3, 4]

    def test_en_famine_le_signal_date_attend_96_heures(self, purge):
        archives, supprimes, _ = purge([_reel(60, 50, 1), _reel(120, 100, 2)], famine=True)
        assert archives == supprimes == [2]

    def test_la_suppression_reste_scopee_aux_actifs(self, purge):
        _, _, base = purge([_reel(60, 50, 1)])
        for table, op, filtres in base.appels:
            if table == "signals" and op == "delete":
                assert ("eq", ("status", "active")) in filtres, filtres


class TestLaRegleDAgeNEstPasRevenue:
    _SOURCE = Path(eng.__file__).read_text(encoding="utf-8")
    _CORPS = _SOURCE.split("def _purge_old_signals(sb):", 1)[1].split("\ndef ", 1)[0]

    def test_plus_de_suppression_sur_la_seule_date_d_emission(self):
        regles = self._CORPS.split("purge_rules = [", 1)[1].split("]", 1)[0]
        assert "created_at" not in regles, (
            "Une règle de purge sur `created_at` est revenue : elle expire un "
            "signal 48 h après son ÉMISSION, avant que l'audit ait pu le régler.")

    def test_plus_de_suppression_directe_sur_match_time(self):
        """La décision passe par `_perime`, signal par signal : un DELETE
        filtré sur `match_time` rouvrirait un second chemin non archivé."""
        assert not re.search(r'\.delete\(\)[^\n]*\n?[^\n]*\.lt\("match_time"', self._CORPS)
        assert "_perime(" in self._CORPS
