# État du Projet Predator PAIM v8.5
**Dernière mise à jour : 2026-05-15**

## Infrastructure & Backend
- [x] Infrastructure Vercel + Supabase (OK)
- [x] GitHub Actions (Engine scraping automatisé)
- [x] Connexion Supabase — colonnes : match_name, match_time, sport, market_type, fair_price, cote_1xbet, alpha_spread, note_ia

## Interface & Dashboard
- [x] Bloomberg Terminal v5.5 (Dark #050505, Neon Green #00ff00)
- [x] Live Signals Dashboard (onglet signaux temps réel)
- [x] Decision Modal — selection_name, Kelly, advice dans les signaux
- [x] Audit CLV Dashboard (visualisation Customer Lifetime Value)

## Moteur PAIM
- [x] Mode Guerrilla / Harvester 1XBet JSON feed
- [x] Gemini 2.0 Flash Google Search (source sharp Pinnacle)
- [x] Shin Method via Bisection (Numpy pur — scipy INTERDIT)
- [x] Kelly Fractionnaire 0.25 — arrondi entier pour obfuscation
- [x] Hunter Multi-Sport : h2h + spreads + totals (NBA, Tennis, Soccer AH 0.0)
- [x] Audit Engine (CLV Ledger)
- [x] Learning Layer (historique des signaux)
- [x] Portfolio Balancer

## Bugs Actifs
- [x] **BUG RÉSOLU** : Prob = 0% et Mise = 0€ sur les signaux soccer (2026-05-15)
  - Cause réelle : seuil `h2h = 0.65` bloquait tous les signaux soccer AH 0.0 (prob naturelle ~52-63%)
  - Fix 1 : `core/paim_engine.py` → ajout `"h2h_soccer": 0.52` dans `SHARP_PROB_BY_MARKET`
  - Fix 2 : `run_engine.py` `_process_h2h` → seuil sport-spécifique (soccer=0.52, NBA/Tennis=0.65)
  - Fix 3 : `run_engine.py` `_emit` → guard `if sharp_prob <= 0: return` (jamais sauvegarder prob=0)
  - Fix 4 : `run_engine.py` `_purge_old_signals` → purge DB des signaux avec sharp_prob=0 ou NULL
- [ ] Vérification du Strict Outcome Matching (Team A Pinnacle ↔ Team A 1XBet)

## Prochaines Étapes
- [ ] Corriger le bug des mises à 0€ dans `core/paim_engine.py`
- [ ] Activation complète Multi-Sport NBA/Tennis (valider les marchés ML/O-U)
- [ ] Tests de charge Supabase (volume de signaux > 50/heure)

## Contraintes Permanentes (MEMORY LOCK)
- INTERDIT : Draw / Nul comme sélection
- INTERDIT : The-Odds-API (quota épuisé)
- INTERDIT : scipy, pandas
- Alpha > 15% = erreur de mapping → REJETER
- Seuil détection : 1.5% | Alerte Elite : 2.5%
