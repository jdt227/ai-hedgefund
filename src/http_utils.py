"""Shared HTTP GET: timeout, retries, per-provider rate limit, on-disk JSON cache.

``fetch`` never raises for network or API problems. It returns a ``FetchResult``
whose ``status`` says what happened, so a data module can skip itself and the
run can report the outage instead of crashing.

Cache layout: data/cache/<provider>/<YYYY-MM-DD>/<key>.json. A re-run on the same
day reads the cache and makes no request. Failures are not cached.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

CACHE_ROOT = Path("data/cache")
RETRY_STATUSES = {429, 500, 502, 503, 504}

# Parameters that must never reach the cache key, logs, or error messages.
SECRET_PARAMS = {"api_key", "apikey", "token"}

_last_request: dict[str, float] = {}


@dataclass
class FetchResult:
    status: str  # ok | cached | forbidden | rate_limited | not_found | error | budget_exhausted
    data: Any = None
    retrieved_at: str | None = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "cached")


def cache_path(provider: str, url: str, params: dict | None, run_date: date,
               cache_root: Path | None = None) -> Path:
    """Return the cache file for this request. Secrets are left out of the key."""
    public = {k: v for k, v in sorted((params or {}).items()) if k not in SECRET_PARAMS}
    key = hashlib.sha256(json.dumps([url, public], default=str).encode()).hexdigest()[:20]
    return (cache_root or CACHE_ROOT) / provider / run_date.isoformat() / f"{key}.json"


def _throttle(provider: str, min_interval: float) -> None:
    wait = _last_request.get(provider, 0.0) + min_interval - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_request[provider] = time.monotonic()


def _scrub(text: str, params: dict | None) -> str:
    """Remove secret values from text that may contain the request URL."""
    for name, value in (params or {}).items():
        if name in SECRET_PARAMS and value:
            text = text.replace(str(value), "REDACTED")
    return text


def fetch(provider: str, url: str, *, params: dict | None = None, headers: dict | None = None,
          run_date: date | None = None, min_interval: float = 0.0, timeout: float = 20.0,
          retries: int = 2, backoff: float = 2.0, as_text: bool = False,
          cache_root: Path | None = None) -> FetchResult:
    """GET a URL through the cache. Returns JSON, or text when ``as_text`` is set."""
    run_date = run_date or date.today()
    path = cache_path(provider, url, params, run_date, cache_root)
    if path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        return FetchResult("cached", cached["data"], cached["retrieved_at"])

    message = ""
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(backoff ** attempt)
        _throttle(provider, min_interval)
        try:
            response = requests.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            message = _scrub(f"{type(exc).__name__}: {exc}", params)
            continue
        code = response.status_code
        if code in RETRY_STATUSES:
            message = f"HTTP {code}"
            continue
        if code in (401, 403):
            return FetchResult("forbidden", message=f"HTTP {code}: key missing, invalid, or not on this plan")
        if code == 404:
            return FetchResult("not_found", message="HTTP 404")
        if code >= 400:
            return FetchResult("error", message=f"HTTP {code}")
        try:
            # Decode bytes directly: requests guesses Latin-1 for text/xml, which mangles a UTF-8 BOM.
            data = response.content.decode("utf-8-sig", errors="replace") if as_text else response.json()
        except ValueError:
            return FetchResult("error", message="Response was not valid JSON")
        retrieved_at = datetime.now(timezone.utc).isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"retrieved_at": retrieved_at, "data": data}), encoding="utf-8")
        return FetchResult("ok", data, retrieved_at)

    status = "rate_limited" if message == "HTTP 429" else "error"
    logger.warning("%s request failed: %s", provider, message)
    return FetchResult(status, message=message)


def remove_cached(provider: str, url: str, params: dict | None, run_date: date,
                  cache_root: Path | None = None) -> None:
    """Forget a cached response that turned out to be an API error message."""
    cache_path(provider, url, params, run_date, cache_root).unlink(missing_ok=True)
