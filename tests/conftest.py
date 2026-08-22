"""
tests/conftest.py — garantit que la suite reste PURE (aucun réseau, aucune base).

POURQUOI CE FICHIER EXISTE
--------------------------
`run_engine.py` appelle `load_dotenv()` au niveau module. Dès qu'un test
l'importe, le `.env` de développement — s'il existe — peuple `os.environ`
pour TOUT le reste de la session pytest. Les modules qui résolvent une
credential à l'exécution (`core/secret_store.py`, `core/daily_quota.py`) se
mettent alors à joindre le vrai Supabase, et les sources de cotes le vrai
Internet, depuis des tests censés être hors-ligne.

Constaté le 2026-08-20 : après le branchement d'api-sports et d'odds-api.io
dans le harvester, `tests/test_harvester.py` ouvrait 31 clients Supabase
réels. Aucun test n'échouait — c'est précisément le problème : la suite
devenait lente, dépendante du réseau, et capable d'écrire dans la base de
production (le compteur de quota fait des UPSERT).

CE QUE FAIT LE GARDE-FOU
------------------------
`socket.socket.connect` lève. Un test qui a besoin d'une réponse HTTP doit
stubber `requests.get`/`post` comme le fait déjà toute la suite — ce garde
ne remplace pas les stubs, il rend leur absence visible immédiatement au
lieu de la laisser passer en appel réel.

CLAUDE.md : « Tests purs uniquement (pas de réseau, pas de rendu de
template) ». Ce fichier fait respecter la première moitié de la règle.
"""
import socket

import pytest


class NetworkUseInTest(RuntimeError):
    """Un test a tenté une vraie connexion réseau."""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def blocked(self, address, *args, **kwargs):
        raise NetworkUseInTest(
            f"connexion réseau interdite dans les tests (vers {address!r}). "
            "Stubbez requests.get/post — voir tests/conftest.py."
        )

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


@pytest.fixture(autouse=True)
def _inert_daily_quota(monkeypatch):
    """Neutralise le compteur de quota journalier.

    Son contrat est déjà « inerte sans base » (core/daily_quota.py), mais il
    construisait quand même un client Supabase à chaque appel — inutile, et
    bruyant dans la sortie de test. Les tests qui vérifient le budget
    patchent les accesseurs de leur propre module et ne sont pas concernés.
    """
    from core import daily_quota
    monkeypatch.setattr(daily_quota, "spent", lambda bucket: 0)
    monkeypatch.setattr(daily_quota, "add", lambda bucket, n: None)


@pytest.fixture(autouse=True)
def _no_ambient_ai_keys(monkeypatch):
    """Neutralise les clés de fournisseurs IA présentes dans l'environnement.

    POURQUOI — même piège que le `load_dotenv()` documenté en tête de fichier,
    et constaté le 2026-08-22 : dès qu'une vraie clé (OPENROUTER_API_KEY,
    GEMINI_API_KEY…) atterrit dans le `.env` de développement, elle peuple
    `os.environ` pour toute la session pytest. Trois tests de
    `test_settlement.py` se sont mis à échouer — non pas parce qu'un
    comportement avait changé, mais parce qu'ils supposaient « aucun
    fournisseur de repli configuré » et qu'il y en avait soudain un.

    Une suite dont le résultat dépend des clés que le développeur a dans son
    `.env` ne prouve rien. Les tests qui ONT besoin d'un fournisseur le
    déclarent eux-mêmes avec `monkeypatch.setenv` — ce qui reste possible,
    puisque ce garde tourne avant eux.
    """
    from core.ai_router import REGISTRY
    for provider in REGISTRY:
        monkeypatch.delenv(provider.env_key, raising=False)
    for extra in ("CLOUDFLARE_ACCOUNT_ID", "TAVILY_API_KEY", "JINA_API_KEY"):
        monkeypatch.delenv(extra, raising=False)
