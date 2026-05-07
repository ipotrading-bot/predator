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

    # ── Multi-Sport v3.6 (MIT Alpha Watchlist - The-Odds-API Official Keys) ─
    # Gisements d'Alpha classés par potentiel de rentabilité
    # Source: nomenclature technique officielle The-Odds-API
    target_sports: list[str] = Field(default=[
        # ════════════════════════════════════════════════════════════
        # 1. BASKETBALL — Priorité Alpha MAXIMALE (Lag Layer efficace)
        #    Fréquence des points = cotes volatiles = inefficiences
        # ════════════════════════════════════════════════════════════
        "basketball_nba",              # NBA USA — Cible n°1 institutionnelle
        "basketball_euroleague",         # Euroleague Europe — Final Four
        "basketball_ncaab",            # NCAA Basket US — March Madness
        "basketball_spain_liga_acb",     # Liga ACB Espagne
        "basketball_wnba",             # WNBA — Fenêtre nuit Dakar
        # ════════════════════════════════════════════════════════════
        # 2. TENNIS — Zéro Nul (Binary Synthesis Sniper Strategy)
        #    Deux issues uniquement = modèle Shin optimal
        # ════════════════════════════════════════════════════════════
        "tennis_atp_french_open",        # Roland Garros (Mai-Juin)
        "tennis_atp_wimbledon",          # Wimbledon (Juillet)
        "tennis_atp_us_open",            # US Open (Août-Sept)
        "tennis_atp_aus_open",            # Australian Open (Janvier)
        "tennis_atp_masters_1000",        # Madrid, Rome, etc.
        "tennis_wta",                    # Circuit WTA féminin
        # ════════════════════════════════════════════════════════════
        # 3. SOCCER — Ligues Majeures (Liquidité Haute, Asian Handicap 0.0)
        #    Protection du Capital sur marchés efficientes
        # ════════════════════════════════════════════════════════════
        "soccer_epl",                    # Premier League Angleterre
        "soccer_uefa_champs_league",     # Champions League
        "soccer_uefa_europa_league",     # Europa League
        "soccer_germany_bundesliga",     # Bundesliga Allemagne
        "soccer_spain_la_liga",          # La Liga Espagne
        "soccer_italy_serie_a",          # Serie A Italie
        "soccer_france_ligue_1",         # Ligue 1 France (underscore)
        # ════════════════════════════════════════════════════════════
        # 4. ESPORTS — Gisement de Latence MAXIMALE
        #    1XBet erreurs fréquentes = Alpha brutal
        # ════════════════════════════════════════════════════════════
        "esports_lol",                   # LoL générique
        "esports_lol_msi",               # Mid-Season Invitational
        "esports_lol_lpl",               # League of Legends Chine
        "esports_lol_lck",               # League of Legends Corée
        "esports_csgo_esl_pro_league",   # Counter-Strike ESL
        "esports_dota2_ti",              # Dota 2 The International
        # ════════════════════════════════════════════════════════════
        # 5. SPORTS US & COMBAT — Diversification
        #    Shin Method optimal sur baseball, volatilité MMA
        # ════════════════════════════════════════════════════════════
        "baseball_mlb",                  # MLB — Très prévisible
        "icehockey_nhl",                 # NHL Hockey sur glace
        "mma_mixed_martial_arts",        # UFC / MMA
    ])

    # ── Seuils Alpha par Sport (ajustement dynamique) ──────────
    # Basket: 2.0% | Tennis: 2.0% | Esports: 1.5% (latence max)
    # Soccer: 2.5% (marché efficient) | MLB: 1.8% | NHL/MMA: 2.0%
    alpha_thresholds: dict[str, float] = Field(default={
        # ═══ BASKETBALL ═══
        "basketball_nba": 0.020,
        "basketball_euroleague": 0.020,
        "basketball_ncaab": 0.020,
        "basketball_spain_liga_acb": 0.020,
        "basketball_wnba": 0.020,
        # ═══ TENNIS ═══
        "tennis_atp_french_open": 0.020,
        "tennis_atp_wimbledon": 0.020,
        "tennis_atp_us_open": 0.020,
        "tennis_atp_aus_open": 0.020,
        "tennis_atp_masters_1000": 0.020,
        "tennis_wta": 0.020,
        # ═══ SOCCER ═══
        "soccer_epl": 0.025,
        "soccer_uefa_champs_league": 0.025,
        "soccer_uefa_europa_league": 0.025,
        "soccer_germany_bundesliga": 0.025,
        "soccer_spain_la_liga": 0.025,
        "soccer_italy_serie_a": 0.025,
        "soccer_france_ligue_1": 0.025,
        # ═══ ESPORTS (Seuil bas = latence maximale) ═══
        "esports_lol": 0.015,
        "esports_lol_msi": 0.015,
        "esports_lol_lpl": 0.015,
        "esports_lol_lck": 0.015,
        "esports_csgo_esl_pro_league": 0.015,
        "esports_dota2_ti": 0.015,
        # ═══ AUTRES ═══
        "baseball_mlb": 0.018,
        "icehockey_nhl": 0.020,
        "mma_mixed_martial_arts": 0.020,
        # ═══ DEFAULT ═══
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