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


def post_with_retry(url, payload, timeout, max_attempts=3,
                     rate_limit_wait=(65, 30), retry_wait=5, label=""):
    """
    POST with retry on connection errors, timeouts, HTTP 429, and HTTP 5xx.
    Returns the last `requests.Response` (caller still checks status_code),
    or None if every attempt raised an exception (no response ever came back).
    """
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
