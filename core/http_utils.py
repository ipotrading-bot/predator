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
from urllib.parse import urlparse, parse_qs

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
#
# 2026-07-11: discovered the flag must be scoped per (model, api key) —
# a single process can hold multiple Gemini keys (GEMINI_API_KEY,
# _AUDIT, _ALT) pointed at different Google Cloud projects with
# independent quotas, and even a single key can have a 0 free-tier
# allocation for one model (e.g. gemini-2.0-flash on a newly created
# project) while another model on that SAME key (gemini-2.5-flash-lite)
# works fine. A global bool meant the first 429 anywhere poisoned every
# later call on every other key/model for the rest of the process.
_gemini_dead_scopes: set[str] = set()


def _quota_scope(url: str) -> str:
    """(model, key-fragment) identity — never logs/exposes the full key."""
    model = url.split("/models/")[-1].split(":")[0] if "/models/" in url else url
    key   = parse_qs(urlparse(url).query).get("key", [""])[0]
    return f"{model}:{key[-6:]}"


def gemini_quota_dead() -> bool:
    """True once ANY Gemini key/model this run hit the DAILY quota limit."""
    return bool(_gemini_dead_scopes)


def post_with_retry(url, payload, timeout, max_attempts=3,
                     rate_limit_wait=(65, 30), retry_wait=5, label=""):
    """
    POST with retry on connection errors, timeouts, HTTP 429, and HTTP 5xx.
    Returns the last `requests.Response` (caller still checks status_code),
    or None if every attempt raised an exception (no response ever came
    back) — or instantly None once the Gemini daily quota is known dead.
    """
    is_gemini = "generativelanguage" in url
    scope = _quota_scope(url) if is_gemini else None
    if is_gemini and scope in _gemini_dead_scopes:
        log.debug("%s: skipped — Gemini daily quota exhausted for this key/model", label)
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
                _gemini_dead_scopes.add(scope)
                log.critical("%s: Gemini DAILY quota exhausted for this key/model — "
                             "skipping further calls to it this run (resets ~07:00 UTC) | %s",
                             label, r.text[:2000])
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


GEMINI_MODEL_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def post_gemini(models, api_key, payload, timeout, max_attempts=3,
                 rate_limit_wait=(65, 30), retry_wait=5, label=""):
    """
    Try each model in `models`, in order, on the same key/payload. Falls
    through to the next model on HTTP 404 (Google retiring free-tier access
    to a specific model per-project, observed 2026-07-11 rolling out
    inconsistently: gemini-2.5-flash-lite works on one project, 404s "no
    longer available to new users" on another created the same week) AND on
    429/5xx (observed same day: a model can have essentially zero usable
    free-tier quota on a given project — e.g. gemini-3.5-flash 429ing on
    every one of 3 separate attempts, ~95s apart, across 3 different
    fetches in the same run — in which case post_with_retry's own
    wait-and-retry loop just burns ~1-3 minutes per call confirming what
    the previous call already proved).

    Every model EXCEPT THE LAST only gets ONE attempt here — if a fallback
    model is available, it's cheaper to try it immediately than to spend
    the full retry_wait budget re-confirming the current one is broken.
    The LAST model in the list still gets the full `max_attempts` (nothing
    left to fall back to, so waiting out a transient per-minute limit is
    the only remaining option). Returns the last response, so callers keep
    their existing `r.status_code` handling unchanged — just pass a model
    list instead of a single URL.
    """
    r = None
    for i, model in enumerate(models):
        is_last = i == len(models) - 1
        url = GEMINI_MODEL_URL.format(model=model) + f"?key={api_key}"
        r = post_with_retry(url, payload, timeout, max_attempts if is_last else 1,
                             rate_limit_wait, retry_wait, label=f"{label}[{model}]")
        if r is not None and not is_last:
            if r.status_code == 404:
                log.warning("%s: model %s unavailable on this project (404) — trying %s next",
                            label, model, models[i + 1])
                continue
            if r.status_code == 429 or r.status_code >= 500:
                log.warning("%s: model %s failed (HTTP %d) — trying %s next",
                            label, model, r.status_code, models[i + 1])
                continue
        return r
    return r
