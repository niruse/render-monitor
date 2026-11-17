"""Read-only client for the Render REST API v1 (https://api-docs.render.com).

Render API keys cannot be limited to reading: a key can also deploy, suspend and delete.
This client therefore only ever sends GET requests - there is one place that talks to the
network (``RenderClient._get``) and it calls ``session.get`` and nothing else. A test
(tests/test_read_only.py) inspects this package and fails if any other HTTP method appears.

The API key is only ever put in the Authorization header. It is never logged, never part
of an exception message, and hidden from ``repr``.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://api.render.com/v1"
TIMEOUT = aiohttp.ClientTimeout(total=30)
PAGE_LIMIT = 100  # the API's maximum page size
MAX_PAGES = 50  # stop a runaway cursor loop: 5000 items is far beyond any workspace we read
MAX_RETRIES = 3  # attempts after a 429 before giving up
MAX_WAIT = 60.0  # never sleep longer than this on one 429
# Our own request budget per rolling minute. Render documents 400 GETs a minute in general and
# 30 a minute for the logs endpoints; stay below both so a large workspace never reaches 429.
BUDGET_WINDOW = 60.0
BUDGET_PER_MINUTE = 300
LOGS_BUDGET_PER_MINUTE = 20
# A 403 on these list paths means the key itself cannot read the workspace. Anywhere else a
# 403 only means this one read is not allowed (a plan or resource), not that the key is bad.
AUTH_PATHS = frozenset({"/owners", "/services"})

# GET /metrics/<name>; anything else is refused before a request is made.
METRICS = frozenset({
    "cpu",
    "cpu-limit",
    "memory",
    "memory-limit",
    "http-requests",
    "http-latency",
    "disk-usage",
    "disk-capacity",
    "instance-count",
})

Params = dict[str, Any]


class RenderError(Exception):
    """Base error for the Render client."""


class RenderAuthError(RenderError):
    """The API key was refused (401 anywhere, or 403 on /owners or /services)."""


class RenderConnectionError(RenderError):
    """Render could not be reached (DNS, TLS, timeout)."""


class RenderRateLimited(RenderError):
    """Render kept answering 429 after the retries."""


class RenderApiError(RenderError):
    """Render answered with an unexpected status or body."""

    def __init__(self, status: int, path: str) -> None:
        super().__init__(f"Render API returned HTTP {status} for {path}")
        self.status = status
        self.path = path


def iso(moment: datetime) -> str:
    """ISO 8601 UTC with a Z, the form Render's examples use."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _segment(value: str) -> str:
    """A path segment taken from an id, quoted so it can never change the path."""
    if not value or not isinstance(value, str):
        raise ValueError("an id is required")
    return quote(value, safe="")


def _query(params: Params) -> list[tuple[str, str]]:
    """Drop empty values; a list repeats its key (resource=a&resource=b)."""
    out: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        values = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
        for item in values:
            if item is None:
                continue
            if isinstance(item, bool):
                item = "true" if item else "false"
            elif isinstance(item, datetime):
                item = iso(item)
            out.append((key, str(item)))
    return out


def _retry_after(headers: Any, attempt: int) -> float:
    """Seconds to wait after a 429: Retry-After, else Ratelimit-Reset, else backoff with jitter."""
    wait: float | None = None
    raw = headers.get("Retry-After") if headers else None
    if raw:
        try:
            wait = float(raw)
        except ValueError:
            wait = None
    if wait is None and headers:
        reset = headers.get("Ratelimit-Reset")
        if reset:
            try:
                value = float(reset)
            except ValueError:
                value = None
            if value is not None:
                # Documented as UTC epoch seconds; a small number is read as seconds-to-wait.
                wait = value - time.time() if value > 1_000_000_000 else value
    if wait is None or wait <= 0:
        wait = 2.0 ** attempt
    return min(MAX_WAIT, wait) + random.uniform(0, 0.5)


class _Budget:
    """At most ``limit`` requests in any rolling ``window`` seconds; waits when it is used up.

    The check and the booking happen with no await in between, so concurrent callers in one
    event loop cannot overbook it.
    """

    def __init__(self, limit: int, window: float, clock: Callable[[], float]) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._sent: deque[float] = deque()

    def wait_time(self) -> float:
        """0 and a booking when a request may go now, else the seconds until a slot frees."""
        now = self._clock()
        while self._sent and now - self._sent[0] >= self.window:
            self._sent.popleft()
        if len(self._sent) < self.limit:
            self._sent.append(now)
            return 0.0
        return max(0.01, self._sent[0] + self.window - now)


class RenderClient:
    """Async, GET-only Render API client."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        *,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_retries: int = MAX_RETRIES,
        per_minute: int = BUDGET_PER_MINUTE,
        logs_per_minute: int = LOGS_BUDGET_PER_MINUTE,
    ) -> None:
        self._session = session
        self._api_key = api_key
        self._sleep = sleep
        self._max_retries = max(0, max_retries)
        self._budget = _Budget(per_minute, BUDGET_WINDOW, clock)
        self._logs_budget = _Budget(logs_per_minute, BUDGET_WINDOW, clock)

    def __repr__(self) -> str:
        return "RenderClient(api_key=**REDACTED**)"

    # ------------------------------------------------------------------ transport

    async def _take_budget(self, path: str) -> None:
        """Wait until the request budget (and the logs budget for /logs) has room."""
        budgets = [self._budget]
        if path.startswith("/logs"):
            budgets.append(self._logs_budget)
        for budget in budgets:
            while (wait := budget.wait_time()) > 0:
                _LOGGER.debug("Render request budget used up, waiting %.1f s", wait)
                await self._sleep(wait)

    async def _get(self, path: str, params: Params | None = None) -> Any:
        """The only network call in this package: GET, with timeout, budget and 429 handling."""
        url = f"{BASE_URL}{path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        query = _query(params or {})
        attempt = 0
        while True:
            await self._take_budget(path)
            try:
                async with self._session.get(
                    url, headers=headers, params=query, timeout=TIMEOUT
                ) as response:
                    status = response.status
                    if status == 429:
                        if attempt >= self._max_retries:
                            raise RenderRateLimited(f"Render rate limit reached on {path}")
                        wait = _retry_after(response.headers, attempt)
                    elif status == 401 or (status == 403 and path in AUTH_PATHS):
                        raise RenderAuthError(f"Render rejected the API key (HTTP {status})")
                    elif status >= 400:
                        raise RenderApiError(status, path)
                    else:
                        try:
                            return await response.json(content_type=None)
                        except (ValueError, aiohttp.ContentTypeError) as err:
                            raise RenderApiError(status, path) from err
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as err:
                raise RenderConnectionError(f"Unable to reach Render ({type(err).__name__})") from err
            except aiohttp.ClientError as err:
                # A broken or truncated body, too many redirects and the like: the read failed,
                # the key is fine. Never a bare aiohttp error out of this client.
                raise RenderConnectionError(f"Unable to read the response from Render ({type(err).__name__})") from err
            attempt += 1
            _LOGGER.debug("Render rate limit on %s, retrying in %.1f s", path, wait)
            await self._sleep(wait)

    async def _paginate(
        self,
        path: str,
        key: str,
        params: Params | None = None,
        *,
        max_items: int | None = None,
    ) -> list[dict[str, Any]]:
        """Follow Render's cursor pagination: pass the last item's cursor until a short/empty page."""
        params = dict(params or {})
        limit = PAGE_LIMIT if max_items is None else max(1, min(PAGE_LIMIT, max_items))
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            page = await self._get(path, {**params, "limit": limit, "cursor": cursor})
            if not isinstance(page, list):
                raise RenderApiError(200, path)
            for row in page:
                if isinstance(row, dict) and isinstance(row.get(key), dict):
                    items.append(row[key])
            if max_items is not None and len(items) >= max_items:
                return items[:max_items]
            if len(page) < limit:
                return items
            cursor = page[-1].get("cursor") if isinstance(page[-1], dict) else None
            if not cursor or cursor in seen:
                return items
            seen.add(cursor)
        _LOGGER.warning("Stopped reading %s after %s pages", path, MAX_PAGES)
        return items

    # ------------------------------------------------------------------ endpoints

    async def list_owners(self) -> list[dict[str, Any]]:
        """GET /owners - the workspaces this key can see."""
        return await self._paginate("/owners", "owner")

    async def list_services(
        self, owner_id: str | None = None, *, include_previews: bool = False
    ) -> list[dict[str, Any]]:
        """GET /services?ownerId=..&includePreviews=false."""
        return await self._paginate(
            "/services",
            "service",
            {"ownerId": owner_id, "includePreviews": include_previews},
        )

    async def get_service(self, service_id: str) -> dict[str, Any]:
        """GET /services/{serviceId}."""
        return await self._get(f"/services/{_segment(service_id)}")

    async def list_deploys(
        self,
        service_id: str,
        *,
        created_after: datetime | None = None,
        max_items: int | None = 100,
    ) -> list[dict[str, Any]]:
        """GET /services/{serviceId}/deploys, newest first."""
        return await self._paginate(
            f"/services/{_segment(service_id)}/deploys",
            "deploy",
            {"createdAfter": created_after},
            max_items=max_items,
        )

    async def list_events(
        self,
        service_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        event_type: str | None = None,
        max_items: int | None = 500,
    ) -> list[dict[str, Any]]:
        """GET /services/{serviceId}/events (Render defaults to the last hour without start)."""
        return await self._paginate(
            f"/services/{_segment(service_id)}/events",
            "event",
            {"startTime": start, "endTime": end, "type": event_type},
            max_items=max_items,
        )

    async def list_instances(self, service_id: str) -> list[dict[str, Any]]:
        """GET /services/{serviceId}/instances - the running instances ({id, createdAt})."""
        result = await self._get(f"/services/{_segment(service_id)}/instances")
        return [row for row in result if isinstance(row, dict)] if isinstance(result, list) else []

    async def get_metric(
        self,
        name: str,
        resource: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        resolution_seconds: int | None = None,
        aggregation: str | None = None,
        quantile: float | None = None,
    ) -> list[dict[str, Any]]:
        """GET /metrics/<name>?resource=..: a list of {labels, values[{timestamp, value}], unit}."""
        if name not in METRICS:
            raise ValueError(f"unknown metric {name!r}")
        params: Params = {
            "resource": resource,
            "startTime": start,
            "endTime": end,
            "resolutionSeconds": resolution_seconds,
        }
        if aggregation is not None:
            if aggregation not in ("AVG", "MAX", "MIN"):
                raise ValueError(f"unknown aggregation {aggregation!r}")
            params["aggregationMethod"] = aggregation
        if quantile is not None:
            params["quantile"] = quantile
        result = await self._get(f"/metrics/{name}", params)
        return [row for row in result if isinstance(row, dict)] if isinstance(result, list) else []

    async def list_logs(
        self,
        owner_id: str,
        resources: Iterable[str],
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        text: Iterable[str] | None = None,
        level: Iterable[str] | None = None,
        log_type: Iterable[str] | None = None,
        max_items: int = 200,
        max_pages: int = 3,
    ) -> list[dict[str, Any]]:
        """GET /logs - newest first; follows hasMore with nextStartTime/nextEndTime.

        The logs endpoints have their own, much lower limit (30 per minute), so this reads
        at most ``max_pages`` pages.
        """
        params: Params = {
            "ownerId": owner_id,
            "resource": list(resources),
            "startTime": start,
            "endTime": end,
            "text": list(text) if text else None,
            "level": list(level) if level else None,
            "type": list(log_type) if log_type else None,
            "direction": "backward",
            "limit": max(1, min(PAGE_LIMIT, max_items)),
        }
        logs: list[dict[str, Any]] = []
        for _ in range(max(1, max_pages)):
            page = await self._get("/logs", params)
            if not isinstance(page, dict):
                raise RenderApiError(200, "/logs")
            logs.extend(row for row in page.get("logs") or [] if isinstance(row, dict))
            if len(logs) >= max_items or not page.get("hasMore"):
                break
            next_start, next_end = page.get("nextStartTime"), page.get("nextEndTime")
            if not next_start or not next_end:
                break
            params = {**params, "startTime": next_start, "endTime": next_end}
        return logs[:max_items]
