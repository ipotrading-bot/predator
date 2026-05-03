# Database Architecture Design for Predator

## 1. Schema Design (Normalized)

To ensure scalability and data integrity, I propose moving from a flat structure to a normalized one:

### Tables
- `events`: Central repository for event information.
  - `id`: UUID (PK)
  - `name`: TEXT
  - `sport`: TEXT
  - `starts_at`: TIMESTAMPTZ

- `signals`: Core signals table.
  - `id`: UUID (PK)
  - `event_id`: UUID (FK to `events`)
  - `market_key`: TEXT
  - `selection`: TEXT
  - `bookmaker_id`: UUID (FK to `bookmakers`)
  - `sharp_prob`: NUMERIC
  - `implied_prob_soft`: NUMERIC
  - `ev_plus`: NUMERIC
  - `recommended_stake`: NUMERIC
  - `status`: TEXT ('pending', 'settled')
  - `created_at`: TIMESTAMPTZ

- `results`: Separated results table for settled signals.
  - `signal_id`: UUID (FK to `signals`)
  - `outcome`: INTEGER
  - `profit_eur`: NUMERIC
  - `closing_odds`: NUMERIC
  - `settled_at`: TIMESTAMPTZ

- `bankroll_snapshots`: Historical balance data.
  - `id`: UUID (PK)
  - `balance`: NUMERIC
  - `drawdown`: NUMERIC
  - `roi`: NUMERIC
  - `timestamp`: TIMESTAMPTZ

## 2. Indexing Strategy
- `signals`: 
  - Index on `(status, created_at DESC)` for dashboard fetching.
  - Index on `(event_id)` for quick lookup.
- `bankroll_snapshots`:
  - Index on `(timestamp DESC)` for equity curves.

## 3. Row Level Security (RLS)
- Assuming a multi-user environment:
  - Add `user_id` (Auth.UID()) column to all tables.
  - Policy: `CREATE POLICY "Users can only access their own data" ON signals FOR ALL USING (auth.uid() = user_id);`

## 4. Real-time Subscriptions
- Enable real-time on `signals` table for dashboard updates:
  - `alter publication supabase_realtime add table signals;`

## 5. API / Database Optimizations
- Use Database Views for complex aggregations (e.g., `performance_summary_view`) to reduce load on the client.
- Use Stored Procedures for atomic updates (e.g., `update_signal_and_bankroll`).
