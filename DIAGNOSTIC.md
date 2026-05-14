# PREDATOR PAIM — Diagnostic Complet v6.0

## 🔴 CRITICAL (CRASH / BLOCKANT)

### 1. CIRCULAR IMPORT (`core/paim_engine.py` ↔ `data/odds_fetcher.py`)
- `core/paim_engine.py` importe `OddsFetcher` depuis `data/odds_fetcher.py`
- `data/odds_fetcher.py` importe `MarketOdds` depuis `core/paim_engine.py`
- **Cause:** `PAIMEngine.fetch_and_process_events()` appelle `OddsFetcher.fetch_all_upcoming()` qui n'existe même pas
- **Fix:** Déplacer la logique métier hors de paim_engine.py

### 2. APScheduler HASH COLLISION (`main.py` L116-121)
- Les 3 jobs scheduler utilisent tous `lambda: asyncio.create_task(...)`
- APScheduler calcule l'ID via le hash de la fonction = même hash pour toutes les lambdas
- **Résultat:** Un seul job survit, les 2 autres sont écrasés
- **Fix:** Donner des `id=` explicites à chaque job

### 3. `_save_scan_log(None)` CRASH (`main.py` L111)
- `_save_scan_log(None, session)` → tente `result.events_analyzed`
- **Résultat:** AttributeError: 'NoneType' object has no attribute 'events_analyzed'
- **Fix:** Capturer le retour de `scanner.run_scan()`

### 4. `alpha_spread` sur PAIMSignal (`api/index.py` L995)
- `s.alpha_spread` n'existe pas sur `PAIMSignal` (c'est `s.ev_plus`)
- **Résultat:** AttributeError dans `/api/hunter-scan`

### 5. `tennis_atp_tour` INEXISTANT (`templates/index.html` L533)
- La fonction `triggerScan()` utilise `tennis_atp_tour` qui n'existe pas dans l'API
- **Résultat:** Erreur 404 silencieuse dans le frontend

## 🟡 HIGH (PROBLÈMES FONCTIONNELS)

### 6. PÉRIPLEXITY SUMMARY AVANT ASSIGNMENT (`signals/scanner.py` L680)
- `_build_gemini_context()` appelé avec `dossier.perplexity_summary` AVANT qu'il soit défini
- Quand EV+ < 2%, `dossier.perplexity_summary` n'est jamais assigné → AttributeError
- **Fix:** Initialiser `dossier.perplexity_summary = ""` avant usage

### 7. DRAW DETECTION BUG (`signals/scanner.py` L579-582)
```python
for outcome in raw_outcomes:
    if outcome.get("name", "").lower() in ("draw", "nul", "match nul"):
        logger.warning(...)
        continue  # ← continue la boucle for, pas le traitement
```
- `continue` ne fait que passer à l'outcome suivant, ne rejette PAS le signal
- **Fix:** Remplacer par un `break` + flag + `continue` sur le market level

### 8. `_is_within_window` DEFAULT TRUE (`signals/scanner.py` L101)
- Si le parse ISO échoue, retourne `True` → matchs anciens potentiellement traités
- **Fix:** Retourner `False` sur erreur

### 9. GEMINI MODEL MISMATCH
- `config.py` L216: `gemini-2.0-flash-exp`
- `core/validator.py` L29: `gemini-2.0-flash`
- `core/paim_engine.py` L307: `gemini-2.0-flash`
- 3 modèles différents → incohérence

### 10. EVENT LOOP LEAKS (`api/index.py` plusieurs endroits)
- `asyncio.new_event_loop()` créé mais pas toujours fermé proprement
- Plusieurs loops créés dans hunter_scan (un par sport)

### 11. `_save_scan_log` LOST RESULT (`main.py` L108-111)
- Le scheduler ignore le résultat de `scanner.run_scan()` → logs vides

### 12. `fetch_all_upcoming()` N'EXISTE PAS (`core/paim_engine.py` L110)
- `PAIMEngine.fetch_and_process_events()` appelle une méthode inexistante

### 13. SIGNALS_VALIDATED LOGIC FLAW (scanner.py)
- `signals_found += len(event_dossiers)` puis `signals_validated += 1` pour chaque
- Mais `signals_rejected = signals_found - signals_validated` = 0 à chaque événement

## 🔵 MEDIUM (QUALITÉ ET MAINTENANCE)

### 14. DÉPENDANCES MANQUANTES (`requirements.txt`)
- `httpx` (utilisé dans odds_fetcher.py) - ABSENT
- `tenacity` (utilisé dans odds_fetcher.py) - ABSENT
- `apscheduler` (utilisé dans main.py) - ABSENT

### 15. CONFIG INCONSISTENTE
- Les seuils `alpha_thresholds` sont TOUS à 0.010 (1.0%) alors que les commentaires disent "Basket: 2.0% | Tennis: 2.0% | Esports: 1.5%"

### 16. NOUVEAU CODE MORT
- `paim_engine.py` a `to_binary_probs()` mais la doctrine Zéro Nul interdit le match nul
- `GeminiValidator` n'est jamais utilisé ailleurs que dans `check_market_red_flags`
- `database.py` a `batch_processor()` qui n'est jamais démarré (pas de background task)

### 17. DANGER SYNTACTIQUE: `.lower()` sur None
- Plusieurs endroits font `str(s.get("selection", "")).lower()` mais `s.get("match_name")` peut être None
- `_is_soft_book` peut recevoir `None` de `bm.get("key", "")` mais fait `.lower()` dessus