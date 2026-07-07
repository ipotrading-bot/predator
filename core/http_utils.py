"""core/http_utils.py — Shared retrying POST for Gemini API calls.
Every Gemini call site used a `for attempt in range(N)` loop that only
retried on HTTP 429 — a connection error, timeout, or 5xx response broke
out of the loop immediately with zero retries despite the loop's shape
suggesting otherwise. This centralizes retry-on-failure (network
exceptions, 429, 5xx) in one place instead of repeating (and
under-implementing) it at every call site.
"""
import logging
import time

import requests

log = logging.getLogger("PREDATOR.http")

# Gemini returns 429 RESOURCE_EXHAUSTED for BOTH per-minute rate limits
# (retrying after 20-40s works) and the free-tier DAILY quota (nothing
# short of waiting for the midnight-Pacific reset helps). The quotaId in
# the response body distinguishes them ("...PerMinute..." vs "...PerDay...").
# 2026-07-07: a guerrilla run burned its whole 9-min GLOBAL_TIMEOUT
# sleeping 40+20+20s per call site against a dead daily quota — every one
# of ~7 Gemini call sites retried in vain, so the run died mid-Tier-2 with
# no Telegram, no heartbeat, red X. Once one call reports PerDay
# exhaustion, every later Gemini call this process makes is pointless —
# this flag short-circuits them all instantly.
_gemini_daily_quota_dead = False


def gemini_quota_dead() -> bool:
    """True once any Gemini call this run hit the DAILY quota limit."""
    return _gemini_daily_quota_dead


def post_with_retry(url, payload, timeout, max_attempts=3,
                     rate_limit_wait=(65, 30), retry_wait=5, label=""):
    """
    POST with retry on connection errors, timeouts, HTTP 429, and HTTP 5xx.
    Returns the last `requests.Response` (caller still checks status_code),
    or None if every attempt raised an exception (no response ever came
    back) — or instantly None once the Gemini daily quota is known dead.
    """
    global _gemini_daily_quota_dead
    is_gemini = "generativelanguage" in url
    if _gemini_daily_quota_dead and is_gemini:
        log.debug("%s: skipped — Gemini daily quota exhausted", label)
        return None

    r = None
    for attempt in range(max_attempts):
        try:
            r = requests.post(url, json=payload, timeout=timeout)
        except Exception as e:
            log.warning("%s: request error (attempt %d/%d): %s",
                        label, attempt + 1, max_attempts, e)
            r = None
            if attempt < max_attempts - 1:
                time.sleep(retry_wait * (attempt + 1))
            continue

        if r.status_code == 429:
            if is_gemini and "PerDay" in r.text:
                _gemini_daily_quota_dead = True
                log.critical("%s: Gemini DAILY quota exhausted — skipping all "
                             "further Gemini calls this run (resets ~07:00 UTC)", label)
                return r
            if attempt < max_attempts - 1:
                wait = rate_limit_wait[0] if attempt == 0 else rate_limit_wait[1]
                log.warning("%s: rate limit — waiting %ds", label, wait)
                time.sleep(wait)
            continue

        if r.status_code >= 500:
            log.warning("%s: HTTP %d (attempt %d/%d)",
                        label, r.status_code, attempt + 1, max_attempts)
            if attempt < max_attempts - 1:
                time.sleep(retry_wait * (attempt + 1))
            continue

        break

    return r
