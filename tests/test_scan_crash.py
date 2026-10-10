"""Le motif d'un scan tombé se lit dans meta.scan_crash, pas seulement dans
les logs Actions (2026-10-10 : trois scans standard en code 1, logs illisibles
sans jeton)."""
import run_engine as eng


def _lever(exc):
    try:
        raise exc
    except BaseException as e:      # noqa: BLE001
        return e


def test_sortie_normale_non_consignee():
    assert eng._motif_de_sortie(SystemExit(0)) is None
    assert eng._motif_de_sortie(SystemExit()) is None


def test_exception_consignee_avec_sa_ligne():
    motif = eng._motif_de_sortie(_lever(TypeError("can't subtract")))
    assert "TypeError: can't subtract" in motif
    assert "test_scan_crash.py" in motif


def test_contrat_de_fin_et_timeout_consignes():
    assert "SystemExit" in eng._motif_de_sortie(_lever(SystemExit(1)))
    assert "EngineTimeout" in eng._motif_de_sortie(_lever(eng.EngineTimeout("900s")))


def test_trace_bornee_par_la_fin():
    motif = eng._motif_de_sortie(_lever(ValueError("x" * 10_000 + "FIN")))
    assert motif.endswith("FIN\n") and len(motif) < eng._CRASH_MAX + 200


def test_consigner_ne_leve_jamais(monkeypatch):
    def boum(**_k):
        raise RuntimeError("supabase injoignable")
    monkeypatch.setattr(eng, "get_db", boum)
    eng._consigner_plantage(_lever(KeyError("k")))


def test_consigner_ecrit_la_cle(monkeypatch):
    ecrit = {}
    monkeypatch.setattr(eng, "get_db", lambda **_k: object())
    monkeypatch.setattr(eng, "_meta_stamp", lambda sb, k, v: ecrit.update({k: v}))
    eng._consigner_plantage(_lever(KeyError("k")))
    assert "KeyError" in ecrit[eng.CRASH_META_KEY]
