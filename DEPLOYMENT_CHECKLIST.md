# 🚀 DEPLOYMENT CHECKLIST

## Pre-Deployment Validation

✅ **Run these before pushing to GitHub:**

```bash
# 1. Validate Python syntax
python3 -m py_compile core/paim_engine.py core/constants.py run_engine.py

# 2. Validate imports
python3 -c "from core.paim_engine import _normalize_team; from core.constants import GLOBAL_TIMEOUT; print('✓ Imports OK')"

# 3. Run validation script
python3 validate_optimizations.py

# 4. Check constants
python3 -c "
from core.constants import ELITE_EDGE, GLOBAL_TIMEOUT, MAX_DB_RETRIES
assert ELITE_EDGE == 2.5
assert GLOBAL_TIMEOUT == 540
assert MAX_DB_RETRIES == 3
print('✓ Constants OK')
"

# 5. Verify cache
python3 -c "
from core.paim_engine import _normalize_team
assert hasattr(_normalize_team, 'cache_info')
print('✓ Cache OK')
"
```

---

## Deployment Steps

### Step 1: Commit Changes (Local)
```bash
git add -A
git commit -m "chore: deploy PAIM optimizations v8.5+ (5 critical fixes)

- Add @lru_cache to team normalization (+20% speed)
- Centralize delay constants (MAX_DB_RETRIES, GLOBAL_TIMEOUT, etc)
- Improve Supabase error recovery with retry logic (+90% reliability)
- Add global 9-minute timeout protection
- Batch purge operations (+75% DB speed)

See: DEPLOYMENT_REPORT.md, ACTION_PLAN.md, OPTIMIZATION_ANALYSIS.md"
```

### Step 2: Push to GitHub
```bash
git push origin main
```

### Step 3: Enable Debug Logging
Edit `.github/workflows/engine.yml`:
```yaml
env:
  PREDATOR_DEBUG: "1"  # Enable for first week
```

### Step 4: Monitor First Run
```bash
# Watch GitHub Actions logs at:
# https://github.com/ipotrading-bot/predator/actions

# Look for:
# ✓ No timeout errors
# ✓ Signal save success rate >= 99%
# ✓ Purge operations completing
# ✓ Debug logs showing cache hits
```

### Step 5: Disable Debug After 1 Week
```bash
# In .github/workflows/engine.yml
env:
  PREDATOR_DEBUG: "0"  # Switch off after validation
```

---

## Verification Metrics

Monitor these during first week:

| Metric | Target | Alert If |
|--------|--------|----------|
| Signal save success | ≥99% | <95% |
| Purge time | <100ms | >200ms |
| Engine timeout | Never | Occurs once |
| Team cache hits | 70%+ | <50% |
| DB retry rate | <1% | >5% |

---

## Rollback Plan (If Issues Arise)

```bash
# Option 1: Revert specific file
git revert <commit-hash> -- run_engine.py

# Option 2: Full rollback
git revert <commit-hash>
git push origin main
```

**Most likely rollback scenarios:**
1. Global timeout fires unexpectedly → Check Gemini API latency
2. DB retries consuming quota → Reduce MAX_DB_RETRIES to 2
3. Cache memory issues → Reduce lru_cache maxsize to 256

---

## Post-Deployment Checklist

- [ ] First engine.py run completed successfully
- [ ] No timeout errors in logs
- [ ] Signal save success rate ≥99%
- [ ] Telegram notifications working
- [ ] Dashboard showing new signals
- [ ] Database size stable (no data loss)
- [ ] Team communicated status

---

## Support

**Questions?** Check these files:
- `DEPLOYMENT_REPORT.md` — Technical details
- `ACTION_PLAN.md` — Next steps
- `OPTIMIZATION_ANALYSIS.md` — Full analysis

**Emergency contact:** [Your name/team]

---

**Status: READY FOR DEPLOYMENT** 🚀
