"""
config.py — Configuration centralisée
Charge depuis st.secrets (Streamlit Cloud) ou .env (local)
"""
import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _inject_secrets() -> None:
    """Injecte st.secrets dans os.environ avant que Pydantic ne lise les variables."""
    try:
        import streamlit as st
        secrets = dict(st.secrets)
        for key, value in secrets.items():
            if isinstance(value, str) and key not in os.environ:
                os.environ[key] = value
    except Exception:
        pass  # Pas sur Streamlit Cloud — on utilise .env


_inject_secrets()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
        extra="ignore",
    )

    # ── API Keys ──────────────────────────────────────────────
    ODDS_API_KEY: str
    GEMINI_API_KEY: str
    TELEGRAM_BOT_TOKEN: str
    TELEGRAM_CHAT_ID: str = ""

    # ── Supabase ──────────────────────────────────────────────
    SUPABASE_URL: str
    SUPABASE_KEY: str

    # ── PAIM Engine ───────────────────────────────────────────
    min_ev_threshold: float = 0.08
    min_snr_ratio: float = 1.5
    kelly_fraction: float = 0.25
    max_drawdown_pct: float = 0.15
    starting_bankroll: float = 10_000.0

    # ── Système 7/9 ───────────────────────────────────────────
    system_size: int = 9
    system_min_wins: int = 7

    # ── Rate Limiting ─────────────────────────────────────────
    api_requests_per_minute: int = 15
    jitter_min_seconds: float = 1.5
    jitter_max_seconds: float = 4.0

    # ── Smart Staking ─────────────────────────────────────────
    stake_rounding_base: int = 10

    # ── Scan Windows ──────────────────────────────────────────
    scan_hours_ahead: int = 24
    scan_schedule: list[str] = ["00:00", "08:00", "16:00"]

    # ── Bookmakers ────────────────────────────────────────────
    sharp_books: list[str] = ["pinnacle", "betfair_ex_eu"]
    soft_books: list[str] = ["1xbet", "bet365", "unibet"]
    target_sports: list[str] = [
        "soccer_epl", "soccer_ligue_1", "basketball_nba",
        "americanfootball_nfl", "tennis_atp"
    ]

    # ── Aliases pour compatibilité avec l'ancien code ─────────
    @property
    def odds_api_key(self) -> str: return self.ODDS_API_KEY
    @property
    def gemini_api_key(self) -> str: return self.GEMINI_API_KEY
    @property
    def telegram_bot_token(self) -> str: return self.TELEGRAM_BOT_TOKEN
    @property
    def telegram_chat_id(self) -> str: return self.TELEGRAM_CHAT_ID
    @property
    def supabase_url(self) -> str: return self.SUPABASE_URL
    @property
    def supabase_key(self) -> str: return self.SUPABASE_KEY


# Singleton global
settings = Settings()
