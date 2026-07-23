"""
tests/test_wiz_ai.py — WIZ : classification des erreurs fournisseur.

Verrouille le mode de panne découvert en PRODUCTION le 2026-07-23 (premier
run réel, job 30006390396) : le connecteur `web_search` de Mistral a un
quota PROPRE, indépendant de celui des modèles et appliqué au niveau du
COMPTE. Sa signature :

    GET  /v1/models        -> 200   la clé est parfaitement valide
    POST /v1/conversations -> 429   {"detail":"web_search rate limit reached."}

Le message ne contient ni « per day » ni « quota », donc la classification
générique des 429 le prenait pour une limite par MINUTE et retentait :
3 modèles x 3 tentatives x ~31 s de throttle = ~7 minutes brûlées par match,
pour zéro résultat, jusqu'au timeout global du run.

Ces tests échouent sur le code d'avant le correctif.
"""
import core.wiz_ai as wiz_ai


class _Resp:
    def __init__(self, code, text="", payload=None):
        self.status_code, self.text, self._p = code, text, payload

    def json(self):
        if self._p is None:
            raise ValueError("no json")
        return self._p


def setup_function():
    wiz_ai._reset_state()


def test_quota_connecteur_est_terminal():
    """« web_search rate limit reached » ne se retente pas : le quota est au
    niveau du compte, changer de modèle donne exactement la même 429."""
    r = _Resp(429, '{"detail":"web_search rate limit reached."}')
    assert wiz_ai._handle_error(r, "mistral-small-latest", "T") == "search_dead"
    assert wiz_ai.search_quota_dead() is True
    assert wiz_ai.search_exhausted() is True
    assert wiz_ai.search_credits_left() == 0


def test_quota_connecteur_ne_tue_pas_les_modeles():
    """Le connecteur est mort, pas les modèles : mistral_complete() (sans
    recherche) doit rester utilisable."""
    wiz_ai._handle_error(_Resp(429, '{"detail":"web_search rate limit reached."}'),
                         "mistral-small-latest", "T")
    assert wiz_ai.wiz_dead() is False


def test_429_par_minute_reste_retentable():
    """Une vraie limite par minute doit continuer à être retentée — sinon on
    abandonne un run pour un incident passager."""
    r = _Resp(429, '{"message":"Requests rate limit exceeded"}')
    assert wiz_ai._handle_error(r, "mistral-small-latest", "T") == "retry"
    assert wiz_ai.search_quota_dead() is False


def test_429_quota_journalier_tue_le_modele_seulement():
    r = _Resp(429, '{"message":"You exceeded your per day quota"}')
    assert wiz_ai._handle_error(r, "mistral-small-latest", "T") == "dead"
    assert wiz_ai.search_quota_dead() is False
    assert "mistral-small-latest" in wiz_ai._mistral_dead_models


def test_401_tue_tous_les_modeles():
    assert wiz_ai._handle_error(_Resp(401, "Unauthorized"), "mistral-small-latest", "T") == "dead"
    assert wiz_ai.wiz_dead() is True


def test_mistral_search_abandonne_sans_essayer_les_autres_modeles(monkeypatch):
    """Le bug d'origine : 3 modèles x 3 tentatives x 31 s de throttle.
    Un seul appel HTTP doit suffire à conclure."""
    monkeypatch.setenv("MISTRAL_API_KEY", "x")
    monkeypatch.setattr(wiz_ai, "WIZ_MISTRAL_MIN_INTERVAL_S", 0.0)
    appels = []

    def fake_post(url, **kwargs):
        appels.append(url)
        return _Resp(429, '{"detail":"web_search rate limit reached."}')

    monkeypatch.setattr(wiz_ai.requests, "post", fake_post)
    text, sources, model = wiz_ai.mistral_search("prompt", label="T")

    assert (text, sources, model) == (None, [], None)
    assert len(appels) == 1, f"{len(appels)} appels HTTP au lieu d'un seul"
    assert wiz_ai.search_exhausted() is True


def test_sources_lues_dans_tool_execution(monkeypatch):
    """Régression : les sources vivent dans tool.execution[].info.result, pas
    seulement dans les blocs tool_reference (absents quand le modèle rend du
    JSON pur au lieu de citer en prose). Ne lire que ces derniers donnait 0
    source à chaque appel — R4 rejetait alors tous les arguments."""
    monkeypatch.setenv("MISTRAL_API_KEY", "x")
    monkeypatch.setattr(wiz_ai, "WIZ_MISTRAL_MIN_INTERVAL_S", 0.0)
    payload = {
        "outputs": [
            {"type": "tool.execution", "name": "web_search", "info": {"result":
                '{"a1": {"url": "https://x.example/1", "title": "T1",'
                ' "description": "d1", "snippets": ["s1"]}}'}},
            {"type": "message.output", "content": '{"verdict":"NEUTRE"}'},
        ]
    }
    monkeypatch.setattr(wiz_ai.requests, "post",
                        lambda url, **k: _Resp(200, "", payload))
    text, sources, model = wiz_ai.mistral_search("prompt", label="T")

    assert text == '{"verdict":"NEUTRE"}'
    assert [s["url"] for s in sources] == ["https://x.example/1"]
    assert sources[0]["description"] == "s1"   # snippets priment sur description


def test_aucune_cle_ne_leve(monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    assert wiz_ai.mistral_search("p") == (None, [], None)
    assert wiz_ai.mistral_complete("p") == (None, None)
