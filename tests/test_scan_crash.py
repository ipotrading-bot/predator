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


def test_trace_caviardee():
    """meta se lit avec la clé anon : aucun secret ne doit y entrer."""
    brut = ("HTTPSConnectionPool: Max retries exceeded with url: "
            "/v4/sports/x/odds/?apiKey=abcdef0123456789&regions=eu "
            "https://api.telegram.org/bot123456:AAH-secret_tok/sendMessage "
            "https://x.supabase.co/rest/v1/meta?key=eq.scan&select=value "
            "Authorization: Bearer eyJhbGciOi.eyJpc3MiOi.c2lnbmF0dXJl "
            "{'apikey': 'sb_secret_zzz'} password=hunter2")
    motif = eng._motif_de_sortie(_lever(RuntimeError(brut)))
    for secret in ("abcdef0123456789", "AAH-secret_tok", "eyJhbGciOi", "c2lnbmF0dXJl",
                   "sb_secret_zzz", "hunter2", "eq.scan"):
        assert secret not in motif, secret
    assert "RuntimeError" in motif and "api.telegram.org/bot***" in motif
