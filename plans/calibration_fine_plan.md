graph TD
    A[Start Calibration Fine Phase] --> B(Analyze Existing Codebase);
    B --> C{Dashboard Improvements};
    B --> D{Validator Prompt Update};

    C --> C1(Market Heatmap);
    C1 --> C1a[Backend: New API endpoint /api/alpha_by_sport];
    C1a --> C1b[Backend: Supabase query for historical signals to calculate average Alpha by sport];
    C1b --> C1c[Frontend: Add a new UI component in templates/index.html to display heatmap data];
    C1c --> C1d[Frontend: JavaScript to fetch and render /api/alpha_by_sport data];

    C --> C2(CLV Counter);
    C2 --> C2a[Backend: New API endpoint /api/clv_last_10_signals];
    C2a --> C2b[Backend: Supabase query for CLV of the last 10 processed signals];
    C2b --> C2c[Frontend: Update existing CLV display element in templates/index.html];
    C2c --> C2d[Frontend: JavaScript to fetch and update /api/clv_last_10_signals data];

    C --> C3(Test API Button);
    C3 --> C3a[Backend: New API endpoint /api/the_odds_api_status];
    C3a --> C3b[Backend: Utilize data/odds_fetcher.py to check API connection and retrieve remaining credits];
    C3b --> C3c[Frontend: Add a new UI button in templates/index.html];
    C3c --> C3d[Frontend: JavaScript to trigger /api/the_odds_api_status and display results];

    D --> D1[Prompt Engineering: Modify prompt in core/validator.py];
    D1 --> D1a[Update prompt logic to evaluate 'weight' of news based on points and age];

    C --> E(Review and Test Dashboard);
    D --> F(Review and Test Validator);
    E & F --> G(Final Review);
    G --> H(Switch to Code Mode);
```

### Implementation Plan: Calibration Fine Phase

This plan details the improvements for the Dashboard and the Validator prompt.

#### 1. Improve the Dashboard for the Risk Manager

**1.1. Market Heatmap: Add a component to display average Alpha by sport (e.g., NBA: 0.8%, Tennis: 1.2%).**

*   **Backend Logic (`api/index.py`):**
    *   Create a new API endpoint, e.g., [`/api/alpha_by_sport`](api/index.py).
    *   This endpoint will query the Supabase database for historical signal data.
    *   It will group signals by `sport` and calculate the average `alpha_spread` (or equivalent metric) for each sport.
    *   The data should be returned in a format like: `{"NBA": 0.008, "Tennis": 0.012}`.
    *   Consider adding a time filter (e.g., last 30 days) to the query for relevance.
*   **Frontend Update (`templates/index.html`):**
    *   Add a new `div` element in [`templates/index.html`](templates/index.html) to host the Market Heatmap. This could be within the existing "Stats Cards" section or a new dedicated section.
    *   Implement JavaScript in [`templates/index.html`](templates/index.html) to:
        *   Fetch data from the new `/api/alpha_by_sport` endpoint.
        *   Dynamically render the average Alpha values for each sport in a clear, visually appealing format (e.g., a simple list or a small table, possibly using color coding for higher/lower Alpha values).
        *   Update this data periodically (e.g., every 5 minutes).

**1.2. CLV Counter: Display the average CLV (Closing Line Value) of the last 10 signals at the top of the dashboard.**

*   **Backend Logic (`api/index.py`):**
    *   Create a new API endpoint, e.g., [`/api/clv_last_10_signals`](api/index.py).
    *   This endpoint will query the Supabase database for the `clv_estimate` (or calculated CLV) of the last 10 *processed* signals. The `api/audit.py` module is responsible for settling signals and can be a good reference for retrieving processed signals.
    *   Calculate the average CLV from these 10 signals.
    *   Return the average CLV as a simple numeric value.
*   **Frontend Update (`templates/index.html`):**
    *   Locate the existing "CLV AVG" display element in [`templates/index.html`](templates/index.html) (line 104).
    *   Modify the JavaScript in [`templates/index.html`](templates/index.html) to:
        *   Fetch data from the new `/api/clv_last_10_signals` endpoint.
        *   Update the `stat-clv` element with the fetched average CLV.
        *   Ensure the display is formatted consistently with existing CLV displays.
        *   Update this data periodically (e.g., every 60 seconds).

**1.3. Test API Button: Add a UI button to trigger a check of the connection with The-Odds-API and display remaining credits.**

*   **Backend Logic (`api/index.py`):**
    *   Create a new API endpoint, e.g., [`/api/the_odds_api_status`](api/index.py).
    *   This endpoint will import and utilize the `OddsFetcher` class from [`data/odds_fetcher.py`](data/odds_fetcher.py).
    *   Call a method on `OddsFetcher` (e.g., `get_quota_status()` or a new method) to check connectivity and retrieve the remaining requests and requests used this month.
    *   Return a JSON response indicating the connection status (e.g., "OK", "Error") and the remaining credits.
*   **Frontend Update (`templates/index.html`):**
    *   Add a new button element in [`templates/index.html`](templates/index.html), perhaps near the existing API health indicators (lines 87-91) or within the "Control" page.
    *   Implement JavaScript in [`templates/index.html`](templates/index.html) to:
        *   Attach an event listener to the button to trigger a `fetch` call to the `/api/the_odds_api_status` endpoint.
        *   Display the connection status (e.g., "The-Odds-API: Connected") and remaining credits (e.g., "Remaining: 150/200") in a new `div` element or an existing status area.
        *   Provide visual feedback (e.g., loading spinner, success/error messages).

#### 2. Update the Validator prompt in `core/validator.py`

*   **Prompt Engineering (`core/validator.py`):**
    *   Locate the `prompt` variable within the `validate` method of the `GeminiValidator` class (lines 32-35).
    *   Modify the prompt to incorporate the concept of "weight" of news. The updated prompt should explicitly ask the AI to evaluate the impact on the spread.
    *   Ensure the prompt includes the conditions: "Si l'absence du joueur X vaut plus de 2 points en NBA, augmente l'indice de confiance. Si la news est déjà vieille de plus de 4h, considère qu'elle est déjà intégrée dans le prix."
    *   The prompt should be carefully reviewed for clarity, conciseness, and effectiveness in guiding the Gemini model's evaluation.

**Revised Prompt for `core/validator.py`:**

```python
            f"En tant qu'expert en analyse de données sportives, évalue l'impact et la 'weight' de cette news sur le spread. "
            f"Considère les points suivants pour ajuster l'indice de confiance: "
            f"1. Si l'absence d'un joueur clé vaut plus de 2 points en NBA, augmente significativement l'indice de confiance. "
            f"2. Si la news est déjà vieille de plus de 4 heures, considère qu'elle est probablement déjà intégrée dans le prix et réduis l'impact perçu. "
            f"Fournis une analyse concise de l'impact."
```

#### Files to be modified:

*   [`core/validator.py`](core/validator.py)
*   [`templates/index.html`](templates/index.html)
*   [`api/index.py`](api/index.py)
*   Potentially [`data/odds_fetcher.py`](data/odds_fetcher.py) if a new method for quota status is preferred over `get_quota_status`. 