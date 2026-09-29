"""
tests/test_diagnostic_sans_sharp.py — les matchs du Tier 2 écartés « Échec
prix Sharp » étaient loggés en total par sport seulement (mesure du
2026-09-29) : impossible de dire combien étaient un NOM non apparié plutôt
qu'un marché sans sharp. Le diagnostic nomme le candidat le plus proche, sans
jamais poser de prix ni casser le scan.
"""

import run_engine
from core.exchange_match import (FENETRE_NOM_H, lookup_exchange, nom_probable,
                                 preparer_candidats)
from core.exchange_match import candidat_proche as _cp


def candidat_proche(m, prix):
    return _cp(m, preparer_candidats(prix))

KO = "2026-09-27T13:30:00Z"


def _row(h, a, t=KO):
    return {"home": h, "away": a, "match": f"{h} vs {a}", "commence_time": t,
            "1": 2.0, "X": 3.4, "2": 3.6}


def _m(h, a, t=KO, sport="soccer"):
    return {"home": h, "away": a, "match": f"{h} vs {a}", "commence_time": t,
            "sport": sport, "league": "Bundesliga"}


class TestCandidatProche:
    def test_le_cas_koln_cologne(self):
        """Un camp identique au même horaire : lookup_exchange refuse (l'autre
        camp ne ressemble pas), le diagnostic le nomme."""
        m, prix = _m("1. FC Köln", "VfB Stuttgart"), {"k": _row("FC Cologne", "VfB Stuttgart")}
        assert lookup_exchange(m, prix) is None
        c = candidat_proche(m, prix)
        assert c[0] == "FC Cologne vs VfB Stuttgart" and nom_probable(c)

    def test_sens_inverse(self):
        c = candidat_proche(_m("Lyon", "Nice"), {"k": _row("Nice", "Lyon")})
        assert c[1] == 1.0 and c[2] == 1.0

    def test_autre_horaire_ignore(self):
        loin = "2026-09-27T%02d:30:00Z" % (13 + int(FENETRE_NOM_H) + 1)
        assert candidat_proche(_m("Lyon", "Nice"), {"k": _row("Lyon", "Nice", loin)}) is None

    def test_horaire_inconnu_garde_le_candidat(self):
        assert candidat_proche(_m("Lyon", "Nice", t=None), {"k": _row("Lyon", "Nice")})

    def test_match_sans_rapport_nest_pas_probable(self):
        c = candidat_proche(_m("Lyon", "Nice"), {"k": _row("Hertha BSC", "Schalke 04")})
        assert c is not None and not nom_probable(c)

    def test_aucun_candidat(self):
        assert candidat_proche(_m("Lyon", "Nice"), {}) is None and not nom_probable(None)


class _Log:
    def __init__(self):
        self.lignes = []

    def info(self, fmt, *a):
        self.lignes.append(fmt % a)

    warning = info


class TestDiagnosticDuScan:
    def test_compte_et_echantillonne_par_sport(self):
        log = _Log()
        ecartes = [_m("1. FC Köln", "VfB Stuttgart"), _m("Lyon", "Nice"),
                   _m("Real Madrid", "Olympiacos", sport="basketball")]
        pools = [{"k": _row("FC Cologne", "VfB Stuttgart")}, {}]
        assert run_engine._diagnostic_sans_sharp(ecartes, pools, log) == {"soccer": 1}
        assert any("NOM PROBABLE" in l and "Köln" in l for l in log.lignes)
        assert any("basketball=0/1" in l and "soccer=1/2" in l for l in log.lignes)

    def test_echantillon_borne(self):
        log = _Log()
        ecartes = [_m(f"Club {i:03d}", f"Autre {i:03d}") for i in range(40)]
        run_engine._diagnostic_sans_sharp(ecartes, [{}], log)
        assert sum(l.startswith("SANS SHARP") for l in log.lignes) == run_engine.SANS_SHARP_ECHANTILLON

    def test_ne_leve_jamais(self, monkeypatch):
        def boum(*_):
            raise RuntimeError("boum")
        monkeypatch.setattr(run_engine, "_candidat_proche", boum)
        log = _Log()
        assert run_engine._diagnostic_sans_sharp([_m("Lyon", "Nice")], [{}], log) == {}
        assert any("diagnostic sans sharp" in l for l in log.lignes)

    def test_branche_sur_le_tri_du_tier2(self):
        import inspect
        src = inspect.getsource(run_engine)
        assert "_diagnostic_sans_sharp(ecartes_sans_sharp, [exchange_prices, sharp_payes], log)" in src

    def test_ne_pose_aucun_prix(self):
        m = _m("1. FC Köln", "VfB Stuttgart")
        avant = dict(m)
        run_engine._diagnostic_sans_sharp([m], [{"k": _row("FC Cologne", "VfB Stuttgart")}], _Log())
        assert m == avant
