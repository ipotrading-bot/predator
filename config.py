"""
config.py v2.0 — Configuration centralisée
Compatible : local (.env) + GitHub Actions (secrets) + Vercel (env vars)
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",           # ignore les vars d'env inconnues (Vercel)
    )

    # ── API Keys ──────────────────────────────────────────────
    odds_api_key: str = Field(..., alias="ODDS_API_KEY")
    gemini_api_key: str = Field(..., alias="GEMINI_API_KEY")
    groq_api_key: str = Field(default="", alias="GROQ_API_KEY")
    news_api_key: str = Field(default="", alias="NEWS_API_KEY")
    perplexity_api_key: str = Field(default="", alias="PERPLEXITY_API_KEY")
    rapidapi_key: str = Field(default="", alias="RAPIDAPI_KEY")
    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(..., alias="TELEGRAM_CHAT_ID")

    # ── Supabase ──────────────────────────────────────────────
    supabase_url: str = Field(..., alias="SUPABASE_URL")
    supabase_key: str = Field(..., alias="SUPABASE_KEY")
    supabase_service_key: str = Field(default="", alias="SUPABASE_SERVICE_KEY")

    # ── Dashboard Security ────────────────────────────────────
    predator_secret: str = Field(default="", alias="PREDATOR_SECRET")

    # ── BetterStack Logging ───────────────────────────────────
    betterstack_token: str = Field(default="", alias="BETTERSTACK_TOKEN")
    betterstack_source_id: str = Field(default="", alias="BETTERSTACK_SOURCE_ID")

    # ── PAIM Engine (Seuils Doctrinaires PhD MIT) ─────────────
    min_ev_threshold: float = Field(default=0.08, ge=0.05)
    min_snr_ratio: float = Field(default=3.0, ge=1.5)
    kelly_fraction: float = Field(default=0.25)
    max_single_stake_pct: float = Field(default=0.03)   # 3% max par pari
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

    # ── Scan ──────────────────────────────────────────────────
    scan_hours_ahead: int = Field(default=24)

    # ── Bookmakers ────────────────────────────────────────────
    sharp_books: list[str] = Field(
        default=["pinnacle"]
    )
    soft_books: list[str] = Field(
        default=["1xbet", "bet365", "unibet", "williamhill"]
    )

    # ── Multi-Sport v2.1 — Bernoulli Trials Uniquement ────────
    # Sports binaires naturels (Moneyline : h2h)
    # Tennis, NBA, NHL, Esports = binaires purs
    # Soccer = Asian Handicap / Spreads uniquement (pas de 1N2)
    target_sports: list[str] = Field(default=[
        # Basketball (NBA - haute liquidité, binaire)
        "basketball_nba",
        # Tennis (ATP - binaire naturel)
        "tennis_atp",
        # Hockey (NHL - binaire, pas de match nul pro)
        "nhl",
        # Esports (LoL - binaire)
        "esports_lol",
        # Football (UEFA - spreads/asian handicap uniquement)
        "soccer_uefa_champs_league",
        "soccer_epl",
        "soccer_spain_la_liga",
    ])

    # ── 1XBet Link Template ───────────────────────────────────
    # Template pour liens directs depuis le dashboard
    xbet_base_url: str = Field(
        default="https://1xbet.com/en/line/",
        alias="XBET_BASE_URL",
    )

    # ── News Sources ──────────────────────────────────────────
    news_sources: list[str] = Field(default=[
        "espn",
        "bbc-sport",
        "sky-sports",
        "the-sports-db",
    ])

    # ── RSS Feeds ─────────────────────────────────────────────
    rss_feeds: list[str] = Field(default=[
        "https://www.espn.com/espn/rss/news",
        "https://www.skysports.com/rss/12",  # Football
        "https://www.skysports.com/rss/16",  # NBA
    ])

    # ── Groq Model ────────────────────────────────────────────
    groq_model: str = Field(default="llama-3.1-70b-versatile")

    # ── Gemini Model ──────────────────────────────────────────
    gemini_model: str = Field(default="gemini-2.0-flash-exp")


# Singleton
settings = Settings()