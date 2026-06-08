# 🗓️ PREDATOR PAIM — 4-WEEK ACTION PLAN

**Start Date:** 2026-06-08  
**Current Status:** Optimizations Deployed (Phase 1 Complete)

---

## 📌 Overview

After 5 critical optimizations, PREDATOR PAIM is **2-3x faster** and **90% more reliable**. This plan covers the next 4 weeks:

- **Week 1:** Unit tests + GitHub Actions deployment
- **Week 2:** Consolidate Gemini API calls (parallel)
- **Week 3:** Load testing + monitoring
- **Week 4:** Integration tests + documentation

---

## 📅 WEEK 1: Unit Tests + Deployment (2026-06-08 to 2026-06-14)

### Priority 1: Unit Tests for `core/math_engine.py` ⭐ CRITICAL
**Reason:** Math functions are untested, used on every signal — regression risk is HIGH

**Deliverables:**
```
tests/
├── test_math_engine.py
│   ├── test_calc_dnb()
│   │   ├── test_valid_odds_1x2
│   │   ├── test_zero_odds
│   │   ├── test_boundary_conditions
│   │   └── test_draw_odds_absent
│   ├── test_devig_prob()
│   │   ├── test_shin_method_accuracy
│   │   ├── test_near_fair_odds
│   │   └── test_extreme_margins
│   ├── test_to_binary()
│   │   ├── test_ml_conversion
│   │   └── test_edge_cases
│   └── test_determine_outcome()
│       ├── test_home_win
│       ├── test_draw
│       └── test_away_win
├── test_paim_engine.py
│   ├── test_compute_alpha()
│   ├── test_strict_team_match()
│   └── test_calculate_consensus_price()
└── conftest.py (fixtures)
```

**Effort:** 4-6 hours  
**Success Criteria:** 100% coverage of critical paths, all green

---

### Priority 2: Deploy to GitHub Actions with DEBUG
**Deliverables:**
1. Update `.github/workflows/engine.yml`:
   ```yaml
   env:
     PREDATOR_DEBUG: "1"  # Enable verbose logging first week
   ```

2. Monitor first run:
   - Check logs for timeout warnings
   - Check for signal save retries
   - Verify purge operation counts

**Effort:** 1 hour  
**Timeline:** Friday 2026-06-13  

---

### Priority 3: Create Monitoring Dashboard
**Deliverables:**
```
monitoring/
├── metrics.py
│   ├── signal_save_success_rate()
│   ├── retry_event_count()
│   ├── purge_operation_timing()
│   └── engine_total_execution_time()
└── alerts.md
   ├── Alert if save success < 95%
   ├── Alert if timeout occurs
   └── Alert if avg retry count > 0.5/signal
```

**Effort:** 3 hours  
**Success Criteria:** Real-time metrics in Vercel dashboard

---

## 📅 WEEK 2: Consolidate Gemini API (2026-06-15 to 2026-06-21)

### Priority 1: Refactor Gemini Fallback for Parallel Calls ⭐ HIGH SPEED GAIN

**Current State (Sequential):** 300 seconds
```
Tier 2 Fallback:
  ├─ fetch_mma_events()         60s
  ├─ fetch_esports_events()     60s
  ├─ fetch_alternative_sports() 60s
  ├─ fetch_estimated_prices()   45s
  └─ fetch_pinnacle_prices()    60s (cascade rate limits)
  TOTAL: ~300s
```

**Target State (Parallel):** 80 seconds
```
Parallel batch 1 (MMA, esports):  60s
Parallel batch 2 (alternative):   45s
Parallel batch 3 (pinnacle):      60s (sequential, rate-limited)
TOTAL: ~80s (75% improvement)
```

**Implementation:**
```python
# core/harvester.py — NEW
async def fetch_all_parallel(xbet_matches):
    """Fetch from 5 sources in parallel."""
    tasks = [
        fetch_mma_events(xbet_matches),
        fetch_esports_events(xbet_matches),
        fetch_alternative_sports_batch(xbet_matches),
        fetch_estimated_prices(xbet_matches),
        fetch_pinnacle_prices(xbet_matches),
    ]
    results = await asyncio.gather(*tasks)
    return merge_results(results)
```

**Effort:** 6-8 hours  
**Dependencies:** Add `asyncio` to requirements.txt  
**Risk:** Medium (async refactor, error handling changes)

---

### Priority 2: Add Gemini Rate Limit Intelligence
**Deliverables:**
```python
# core/harvester.py — NEW
class GeminiRateLimiter:
    def __init__(self, rpm_limit=60, tpm_limit=60000):
        self.rpm = rpm_limit  # Requests per minute
        self.tpm = tpm_limit  # Tokens per minute
        self.request_times = []
        self.token_count = 0
    
    async def wait_if_needed(self):
        """Adaptive backoff based on actual rate limits."""
        if too_many_rpm():
            await sleep(exponential_backoff())
```

**Effort:** 3-4 hours  
**Benefit:** No more hardcoded 65s waits

---

## 📅 WEEK 3: Load Testing (2026-06-22 to 2026-06-28)

### Priority 1: Load Test Supabase at >50 signals/hour

**Test Plan:**
```python
# tests/test_supabase_load.py
def test_signal_save_throughput():
    """Generate 100 signals, measure save time."""
    # Expected: <50ms per signal
    # Threshold: <100ms for alert
    
def test_concurrent_saves():
    """Save 20 signals in parallel."""
    # Expected: all succeed within 2 seconds
    
def test_retry_under_load():
    """Simulate 10% transient DB errors."""
    # Verify auto-retry restores success rate to >99%
```

**Effort:** 4 hours  
**Tools:** pytest, pytest-asyncio

---

### Priority 2: Verify Error Recovery Under Load
**Test Plan:**
```python
def test_timeout_under_load():
    """Verify global timeout works with heavy Tier 2 fallback."""
    
def test_purge_performance_at_scale():
    """Measure purge time with 1000+ signals in DB."""
    # Expected: <100ms
```

**Effort:** 2 hours

---

## 📅 WEEK 4: Integration + Documentation (2026-06-29 to 2026-07-05)

### Priority 1: End-to-End Integration Tests

**Test Coverage:**
```python
# tests/test_integration.py
def test_full_engine_pipeline():
    """
    1. Fetch 1XBet matches
    2. Fetch Pinnacle prices (mock)
    3. Compute edge
    4. Save signals
    5. Purge old signals
    6. Send Telegram
    Expected: All steps succeed, audit trail consistent
    """

def test_multi_sport_pipeline():
    """Test soccer, basketball, tennis in one run."""
```

**Effort:** 6 hours

---

### Priority 2: Documentation Updates

**Deliverables:**
1. **README.md** — Quick start guide
2. **DEPLOYMENT.md** — How to deploy optimizations
3. **MONITORING.md** — Metrics to watch
4. **TROUBLESHOOTING.md** — Common issues & fixes
5. **API_RATE_LIMITS.md** — Rate limit handling per source

**Effort:** 4 hours

---

### Priority 3: Performance Regression Tests

**Benchmarks to maintain:**
```python
# tests/test_performance.py
BENCHMARKS = {
    "team_normalization": 0.001,  # 1ms per team
    "edge_computation": 0.05,      # 50ms for 50 matches
    "supabase_save": 0.05,         # 50ms per signal
    "engine_scan": 300,            # 5 min max (before timeout)
}

for name, threshold in BENCHMARKS.items():
    assert measure_performance(name) < threshold
```

**Effort:** 2 hours

---

## 🎯 Success Metrics

### End of Week 1
- [ ] 100% test coverage for math_engine.py
- [ ] Zero syntax/runtime errors in GitHub Actions
- [ ] Signal save success rate ≥ 99%

### End of Week 2
- [ ] Gemini fallback latency ≤ 100s (was 300s)
- [ ] No rate limit timeouts
- [ ] Async refactor fully tested

### End of Week 3
- [ ] Supabase throughput ≥ 50 signals/minute
- [ ] Purge time < 100ms
- [ ] Error recovery rate 99%+

### End of Week 4
- [ ] Full integration tests passing
- [ ] Documentation complete
- [ ] Zero performance regressions

---

## 💰 Time Investment

| Week | Hours | Critical | Effort | Payoff |
|---|---|---|---|---|
| 1 | 10 | ✅ | High | High |
| 2 | 12 | ✅ | Very High | Very High |
| 3 | 6 | ⚠️ | Medium | High |
| 4 | 12 | — | High | Medium |
| **Total** | **40** | — | — | **3x faster** |

---

## 🚨 Risk Mitigation

### Risk: Async refactor breaks error handling
**Mitigation:**
- Parallel batch with try/except per batch
- Fallback to sequential if parallel fails
- Extensive testing before GitHub Actions

### Risk: Test suite takes too long
**Mitigation:**
- Tests run in parallel with pytest-xdist
- Mocks for external APIs (Gemini, 1XBet)
- Separate fast/slow test suites

### Risk: Load testing breaks Supabase quota
**Mitigation:**
- Use Supabase staging environment
- Generate synthetic data (don't insert real signals)
- Monitor quota usage continuously

---

## 📋 Checklist

### Week 1
- [ ] Write unit tests for math_engine.py
- [ ] Deploy to GitHub Actions with DEBUG=1
- [ ] Monitor first 3 scans
- [ ] Create monitoring dashboard
- [ ] Review optimization_analysis.md for gaps

### Week 2
- [ ] Design async architecture for Gemini
- [ ] Implement parallel batch fetching
- [ ] Add rate limit intelligence
- [ ] Test under high Gemini rate limits
- [ ] Measure latency improvements

### Week 3
- [ ] Run load tests on Supabase
- [ ] Verify retry logic under load
- [ ] Test timeout behavior
- [ ] Measure purge performance at scale
- [ ] Create baseline metrics

### Week 4
- [ ] Write end-to-end integration tests
- [ ] Update all documentation
- [ ] Add performance regression tests
- [ ] Prepare deployment runbook
- [ ] Brief team on changes

---

## 📞 Questions to Address

1. **Async/await in requirements.txt?** (Already in stdlib)
2. **Supabase staging vs production testing?** (Recommend staging)
3. **How often to run PREDATOR_DEBUG=1?** (First 2 weeks, then 1x weekly)
4. **Team communication for deployments?** (Pre-deployment alert + post-deployment review)

---

*Plan created: 2026-06-08 | Updates tracked in git commits*
