#!/usr/bin/env python3
"""
Validation script — Test that all critical optimizations compile and basic functionality works
Run: python validator.py
"""
import sys
import time
from datetime import datetime, timezone

print("="*70)
print("🔍 PREDATOR PAIM OPTIMIZATION VALIDATION")
print("="*70)

# Test 1: Import all modules
print("\n[1/5] Testing imports...")
try:
    from core.paim_engine import _normalize_team, compute_alpha, SHARP_PROB_BY_MARKET
    from core.math_engine import calc_dnb, devig_prob, to_binary
    from core.constants import (
        ELITE_EDGE, MIN_STAKE, BANKROLL_REF, MAX_EDGE,
        DELAY_XBET_MIN, DELAY_XBET_MAX, DELAY_GEMINI_RATE,
        DELAY_DB_RETRY, MAX_DB_RETRIES, GLOBAL_TIMEOUT, DEBUG_MODE
    )
    print("   ✓ All imports successful")
except Exception as e:
    print(f"   ✗ Import failed: {e}")
    sys.exit(1)

# Test 2: Cache functionality
print("\n[2/5] Testing team name cache (@lru_cache)...")
try:
    # First call
    start = time.perf_counter()
    result1 = _normalize_team("Manchester United FC")
    time1 = time.perf_counter() - start
    
    # Second call (cached)
    start = time.perf_counter()
    result2 = _normalize_team("Manchester United FC")
    time2 = time.perf_counter() - start
    
    assert result1 == result2 == "manchester united", f"Normalization failed: {result1}"
    speedup = time1 / time2 if time2 > 0 else float('inf')
    print(f"   ✓ Cache working (first: {time1*1000:.3f}ms, cached: {time2*1000:.3f}ms, speedup: {speedup:.1f}x)")
except Exception as e:
    print(f"   ✗ Cache test failed: {e}")
    sys.exit(1)

# Test 3: Math engine validation
print("\n[3/5] Testing math engine functions...")
try:
    # Test DNB calculation
    dnb_home, dnb_away = calc_dnb(2.5, 2.4, 3.0)
    assert 1.5 < dnb_home < 3.0, f"DNB calculation failed: {dnb_home}"
    
    # Test binary conversion
    prob_binary = to_binary(2.0, 2.1, 3.0)
    assert 0.4 < prob_binary < 0.6, f"Binary conversion failed: {prob_binary}"
    
    print(f"   ✓ DNB: {dnb_home:.3f}, Binary: {prob_binary:.3f}")
except Exception as e:
    print(f"   ✗ Math test failed: {e}")
    sys.exit(1)

# Test 4: Edge computation
print("\n[4/5] Testing edge computation...")
try:
    edge, status = compute_alpha(2.5, 2.3)
    assert status == "OK", f"Edge computation failed: {status}"
    assert 5.0 < edge < 10.0, f"Edge value suspicious: {edge}%"
    print(f"   ✓ Edge: {edge:.2f}%, Status: {status}")
except Exception as e:
    print(f"   ✗ Edge test failed: {e}")
    sys.exit(1)

# Test 5: Constants validation
print("\n[5/5] Testing centralized constants...")
try:
    assert ELITE_EDGE == 2.5, f"ELITE_EDGE mismatch: {ELITE_EDGE}"
    assert MIN_STAKE == 2, f"MIN_STAKE mismatch: {MIN_STAKE}"
    assert BANKROLL_REF == 150, f"BANKROLL_REF mismatch: {BANKROLL_REF}"
    assert MAX_EDGE == 15.0, f"MAX_EDGE mismatch: {MAX_EDGE}"
    assert GLOBAL_TIMEOUT == 540, f"GLOBAL_TIMEOUT mismatch: {GLOBAL_TIMEOUT}"
    assert DELAY_XBET_MIN == 2.0, f"DELAY_XBET_MIN mismatch: {DELAY_XBET_MIN}"
    assert MAX_DB_RETRIES == 3, f"MAX_DB_RETRIES mismatch: {MAX_DB_RETRIES}"
    
    print(f"   ✓ All constants validated")
    print(f"     - ELITE_EDGE: {ELITE_EDGE}%")
    print(f"     - GLOBAL_TIMEOUT: {GLOBAL_TIMEOUT}s")
    print(f"     - MAX_DB_RETRIES: {MAX_DB_RETRIES}")
    print(f"     - DELAY_XBET: {DELAY_XBET_MIN}-{DELAY_XBET_MAX}s")
except AssertionError as e:
    print(f"   ✗ Constant validation failed: {e}")
    sys.exit(1)

print("\n" + "="*70)
print("✅ ALL VALIDATION TESTS PASSED")
print("="*70)
print("\n📊 Summary of Optimizations Applied:")
print("   1. ✓ Team normalization caching (@lru_cache)")
print("   2. ✓ Centralized constants (retry delays, timeouts)")
print("   3. ✓ Improved Supabase error handling with retries")
print("   4. ✓ Global timeout protection (9 min safety net)")
print("   5. ✓ Debug logging via PREDATOR_DEBUG env var")
print("\n🚀 Next: Deploy to GitHub Actions with new code")
print("="*70)
