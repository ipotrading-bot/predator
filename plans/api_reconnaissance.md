## API Reconnaissance Report

This report summarizes the existing sports-related APIs used in the project and identifies opportunities for new integrations.

### Existing Sports-Related APIs:

1.  **The-Odds-API** (`odds_api_key` in [`config.py`](config.py:19), used by [`data/odds_fetcher.py`](data/odds_fetcher.py)): Primary source for real-time upcoming sports odds across various sports.
2.  **API-Sports.io** (`api_football_key` in [`config.py`](config.py:25), used by [`data/multi_source_fetcher.py`](data/multi_source_fetcher.py)): Provides soccer and NBA fixtures, potentially enriched with odds via Gemini.
3.  **TheSportsDB** (used by [`data/multi_source_fetcher.py`](data/multi_source_fetcher.py)): Free source for fixtures across several major leagues.
4.  **Google Search Grounding via Gemini** (`gemini_api_key` in [`config.py`](config.py:20), used by [`core/gemini_search.py`](core/gemini_search.py) and [`data/multi_source_fetcher.py`](data/multi_source_fetcher.py)): Serves as a fallback for odds searching and an 'AI Market Scout' to identify value bets and enrich existing fixtures with odds.
5.  **NewsAPI** (`news_api_key` in [`config.py`](config.py:22), used by [`core/news_engine.py`](core/news_engine.py)): Fetches market-moving news (injuries, suspensions, etc.) to assess event impact.
6.  **Groq** (`groq_api_key` in [`config.py`](config.py:21), used by [`core/gemini_search.py`](core/gemini_search.py)): Used for rapid JSON parsing of Gemini's responses.

### Identified Opportunity: Historical Odds API Integration

I found `historical_odds_key` defined in [`config.py`](config.py:26) but currently unused. This presents a significant opportunity to integrate a historical odds API to enhance the system's analytical capabilities.

### Proposed Integration Plan for Historical Odds API:

1.  **Provider Selection:** Leverage the existing `historical_odds_key` to integrate with the intended historical odds provider (likely The-Odds-API if they offer historical data, or a dedicated historical data provider).
2.  **New Data Fetcher Module:** Create a dedicated module, e.g., [`data/historical_odds_fetcher.py`](data/historical_odds_fetcher.py), to manage API calls, error handling, and rate limiting for historical data.
3.  **Implement Fetching Logic:** Develop functions to retrieve historical odds based on various criteria (sport, team, date range, market type).
4.  **Database Integration:** Potentially extend the database schema to store historical odds, enabling efficient querying and analysis.
5.  **Integration Points for Enhanced Analysis:**
    *   **Backtesting Engine:** Develop a new component to simulate betting strategies against historical data, validating their effectiveness.
    *   **CLV (Closing Line Value) Analysis:** Utilize historical odds to calculate and analyze CLV, providing deeper insights into odds movement and model accuracy (e.g., in a module like [`scripts/clv_tracker.py`](scripts/clv_tracker.py)).
    *   **Advanced Model Training:** Provide a rich dataset for training and validating more sophisticated machine learning models for predictions and value detection.

This integration would significantly expand the system's ability to "chercher infos partout ou c'est possible" by incorporating historical market data, crucial for advanced sports analytics and strategy development.