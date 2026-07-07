"""
tests/test_db.py — core/db.py credential resolution.
Regression guard for the 2026-07-07 incident: SUPABASE_SERVICE_KEY held a
valid-but-wrong-privilege (anon) JWT, which authenticated fine and then
failed every write with RLS 42501 for ~17h before anyone noticed. get_db()
must catch that class of misconfiguration immediately, before any network
call, by decoding the JWT's role claim.
"""
import base64
import json

import pytest

from core.db import MissingCredentialsError, _jwt_role, get_db


def _jwt(role: str) -> str:
    header  = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"role": role}).encode()).decode().rstrip("=")
    return f"{header}.{payload}.sig"


class TestJwtRole:
    def test_decodes_service_role(self):
        assert _jwt_role(_jwt("service_role")) == "service_role"

    def test_decodes_anon(self):
        assert _jwt_role(_jwt("anon")) == "anon"

    def test_garbage_token_returns_none(self):
        assert _jwt_role("not-a-jwt") is None

    def test_empty_string_returns_none(self):
        assert _jwt_role("") is None


class TestGetDb:
    def test_write_true_raises_on_missing_service_key(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
        with pytest.raises(MissingCredentialsError):
            get_db(write=True)

    def test_write_true_raises_on_anon_key_in_service_slot(self, monkeypatch):
        # The exact 2026-07-07 failure mode: a syntactically valid JWT that
        # authenticates fine but decodes to the wrong role.
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", _jwt("anon"))
        with pytest.raises(MissingCredentialsError, match="anon"):
            get_db(write=True)

    def test_write_true_succeeds_with_real_service_role_key(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", _jwt("service_role"))
        sb = get_db(write=True)
        assert sb is not None

    def test_write_false_returns_none_without_raising_when_unconfigured(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_KEY", raising=False)
        assert get_db(write=False) is None

    def test_write_false_never_requires_service_key(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SUPABASE_KEY", "anon-key-stub")
        monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
        assert get_db(write=False) is not None
