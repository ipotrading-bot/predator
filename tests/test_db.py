"""
tests/test_db.py — core/db.py credential resolution.

Regression guard for two incidents on 2026-07-07:
1. SUPABASE_SERVICE_KEY held a valid-but-wrong-privilege (anon) legacy JWT,
   which authenticated fine and then failed every write with RLS 42501 for
   ~17h before anyone noticed.
2. After that fix shipped, the same project's Vercel deployment kept
   failing — the value pasted there was a new-format Supabase key
   (`sb_secret_...` / `sb_publishable_...`, not a JWT at all), which the
   first fix's JWT-only decoder couldn't recognize and reported as
   role=None. get_db() must handle both key formats.
"""
import base64
import json

import pytest

from core.db import MissingCredentialsError, _key_role, get_db


def _jwt(role: str) -> str:
    header  = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"role": role}).encode()).decode().rstrip("=")
    return f"{header}.{payload}.sig"


class TestKeyRole:
    def test_decodes_legacy_jwt_service_role(self):
        assert _key_role(_jwt("service_role")) == "service_role"

    def test_decodes_legacy_jwt_anon(self):
        assert _key_role(_jwt("anon")) == "anon"

    def test_recognizes_new_format_secret_key(self):
        assert _key_role("sb_secret_abc123xyz") == "service_role"

    def test_recognizes_new_format_publishable_key(self):
        assert _key_role("sb_publishable_abc123xyz") == "anon"

    def test_garbage_token_returns_none(self):
        assert _key_role("not-a-jwt") is None

    def test_empty_string_returns_none(self):
        assert _key_role("") is None


class TestGetDb:
    def test_write_true_raises_on_missing_service_key(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
        with pytest.raises(MissingCredentialsError):
            get_db(write=True)

    def test_write_true_raises_on_anon_key_in_service_slot(self, monkeypatch):
        # The 2026-07-07 failure mode #1: a syntactically valid legacy JWT
        # that authenticates fine but decodes to the wrong role.
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", _jwt("anon"))
        with pytest.raises(MissingCredentialsError, match="anon"):
            get_db(write=True)

    def test_write_true_raises_on_publishable_key_in_service_slot(self, monkeypatch):
        # The 2026-07-07 failure mode #2: new-format key, wrong privilege.
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "sb_publishable_abc123")
        with pytest.raises(MissingCredentialsError, match="anon"):
            get_db(write=True)

    def test_write_true_succeeds_with_real_service_role_key(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", _jwt("service_role"))
        sb = get_db(write=True)
        assert sb is not None

    def test_write_true_succeeds_with_new_format_secret_key(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "sb_secret_abc123xyz")
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
