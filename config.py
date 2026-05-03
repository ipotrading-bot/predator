"""
config.py — Configuration centralisée via Pydantic Settings
Charge depuis st.secrets (Streamlit Cloud) ou .env (local)
"""
import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_streamlit_secrets() -> None:
    """Injecte st.secrets dans os.environ si on est sur Streamlit Cloud."""
    try:
        import streamlit as st
        for key, value in st.secrets.items():
            if isinstance(value, str):
                os.environ.setdefault(key, value)
    except Exception:
        pass


_load_streamlit_secrets()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    # ── API Keys ──────────────────────────────────────────────
    odds_api_key: str = Field(..., alias="ODDS_API_KEY")
    gemini_api_key: str = Field(..., alias="GEMINI_API_KEY")
    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")

    # ── Supabase ──────────────────────────────────────────────
    supabase_url: str = Field(..., alias="SUPABASE_URL")
    supabase_key: str = Field(..., alias="SUPABASE_KEY")

    # ── PAIM Engine ───────────────────────────────────────────
    min_ev_threshold: float = Field(default=0.08)
    min_snr_ratio: float = Field(default=1.5)
    kelly_fraction: float = Field(default=0.25)
    max_drawdown_pct: float = Field(default=0.15)
    starting_bankroll: float = Field(default=10_000.0)

    # ── Système 7/9 ───────────────────────────────────────────
    system_size: int = Field(default=9)
    system_min_wins: int = Field(default=7)

    # ── Rate Limiting ─────────────────────────────────────────
    api_requests_per_minute: int = Field(default=15)
    jitter_min_seconds: float = Field(default=1.5)
    jitter_max_seconds: float = Field(default=4.0)

    # ── Smart Staking ─────────────────────────────────────────
    stake_rounding_base: int = Field(default=10)

    # ── Scan Windows ──────────────────────────────────────────
    scan_hours_ahead: int = Field(default=24)
    scan_schedule: list[str] = Field(default=["00:00", "08:00", "16:00"])

    # ── Bookmakers ────────────────────────────────────────────
    sharp_books: list[str] = Field(default=["pinnacle", "betfair_ex_eu"])
    soft_books: list[str] = Field(default=["1xbet", "bet365", "unibet"])
    target_sports: list[str] = Field(
        default=["soccer_epl", "soccer_ligue_1", "basketball_nba",
                 "americanfootball_nfl", "tennis_atp"]
    )


# Singleton global
settings = Settings()
