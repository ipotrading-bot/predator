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
    api_football_key: str = Field(default="", alias="API_FOOTBALL_KEY")
    historical_odds_key: str = Field(default="", alias="HISTORICAL_ODDS_KEY")
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
    min_ev_threshold: float = Field(default=0.015, ge=0.005)  # 1.5% display min
    alpha_display_min: float = Field(default=0.015)            # 1.5% → affiché
    alpha_elite_min: float = Field(default=0.025)              # 2.5% → ELITE
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
    scan_hours_ahead: int = Field(default=48)

    # ── Bookmakers ────────────────────────────────────────────
    sharp_books: list[str] = Field(
        default=["pinnacle"]
    )
    soft_books: list[str] = Field(
        # Toutes les variantes 1XBet reconnues (insensible à la casse géré dans le scanner)
        default=["onexbet", "1xbet", "1xbit", "1xstavka", "1x_bet", "oneexbet",
                 "bet365", "unibet", "williamhill"]
    )

    # ── Multi-Sport v3.5 (MIT Portfolio d'Alpha Global) ─────────
    # Diversification: NBA Playoffs, Tennis Masters, MLB, Euroleague, LoL MSI
    # Doctrine: Le seul "free lunch" en finance est la diversification
    target_sports: list[str] = Field(default=[
        # ═══════════════════════════════════════════════════════════
        # BASKETBALL — Haute liquidité, Playoffs = Alpha maximal
        # ═══════════════════════════════════════════════════════════
        "basketball_nba",           # Playoffs: cotes volatiles
        "basketball_wnba",          # Fenêtre nuit Dakar
        "basketball_euroleague",    # Final Four = inefficiences
        # ═══════════════════════════════════════════════════════════
        # TENNIS — Masters Madrid/Rome (Mai), météo/altitude sous-estimée
        # ═══════════════════════════════════════════════════════════
        "tennis_atp",               # Masters 1000
        "tennis_wta",               # Circuit féminin
        # ═══════════════════════════════════════════════════════════
        # BASEBALL — MLB: Très prévisible, volume élevé
        # ═══════════════════════════════════════════════════════════
        "baseball_mlb",             # Saison régulière + playoffs
        # ═══════════════════════════════════════════════════════════
        # ESPORTS — MSI (Mai), Draft inefficience maximale
        # ═══════════════════════════════════════════════════════════
        "esports_lol",              # MSI 2026 / Tournois majeurs
        "esports_lol_msi",          # Mid-Season Invitational
        # ═══════════════════════════════════════════════════════════
        # SOCCER — Protection du Capital (Asian Handicap 0.0)
        # ═══════════════════════════════════════════════════════════
        "soccer_uefa_champs_league",
        "soccer_epl",
        "soccer_spain_la_liga",
        "soccer_germany_bundesliga",
        "soccer_italy_serie_a",
        "soccer_france_ligue_one",
    ])

    # ── Seuils Alpha par Sport (ajustement dynamique) ──────────
    # NBA: 2.0% (volume stable), Foot: 2.5% (marché efficient)
    alpha_thresholds: dict[str, float] = Field(default={
        "basketball_nba": 0.020,
        "basketball_wnba": 0.020,
        "basketball_euroleague": 0.020,
        "tennis_atp": 0.020,
        "tennis_wta": 0.020,
        "baseball_mlb": 0.018,
        "esports_lol": 0.015,
        "esports_lol_msi": 0.015,
        "soccer_epl": 0.025,
        "soccer_uefa_champs_league": 0.025,
        "default": 0.020,
    })

    # ── Synonyms ──────────────────────────────────────────────
    synonyms: dict[str, str] = Field(default={
        # Normaliser toutes les variantes 1XBet vers "onexbet"
        "1xbet":    "onexbet",
        "1xbit":    "onexbet",
        "1xstavka": "onexbet",
        "1x_bet":   "onexbet",
        "oneexbet": "onexbet",
    })

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