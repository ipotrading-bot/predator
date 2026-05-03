# Cockpit Transformation Plan - Predator PAIM

## 1. API Status Band
- New Endpoint: `GET /api/status`
  - Returns JSON: `{ "pinnacle": "online|offline", "1xbet": "online|offline", "gemini": "online|offline", "telegram": "online|offline", "latencies": { "pinnacle": 120, ... } }`
- Frontend: Add a banner in the header to display status with LED indicators (colors) and latency.

## 2. Metrics Cards
- Update `/api/stats` to include:
  - `exposure` (current active bets sum)
  - `sharpe_ratio`
  - `total_engaged`
- Update UI in `index.html` to add these cards in the stats grid.

## 3. Real-time Logs Terminal
- New Endpoint: `GET /api/logs`
- Frontend: Add a `<div id="log-terminal" class="bg-black text-green-500 font-mono text-xs h-40 overflow-y-scroll">` under the Scan button. Use JS to fetch and append logs periodically.

## 4. Audit Chart
- Use Chart.js via CDN.
- Data Endpoint: `GET /api/audit/equity`
- Frontend: Initialize Chart.js instance in the `#page-audit` div.

## 5. UI/UX
- Ticker: Add a `<marquee>` or CSS animation in the header.
- Clickable Cards: Add `onclick="window.open(...)"` to signals in `page-live`.
