"""One read of Render per scan interval; metrics and logs only every metrics interval (cached).

Calls per fast cycle: 1 (services) + per service 2-3 (deploys, events, instances).
Calls per metrics cycle: per service up to 8 metrics, and 2 log searches for all services
together, because the logs endpoints allow only 30 requests a minute.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .advice import build_advice, needs_attention
from .analysis import HTTP_TYPES, NO_METRICS_TYPES, build_snapshot, logs_by_service, parse_time
from .api import RenderAuthError, RenderClient, RenderError
from .const import (
    CONF_METRICS_INTERVAL,
    CONF_OWNER_ID,
    CONF_OWNER_NAME,
    CONF_SCAN_INTERVAL,
    CONF_SERVICES,
    DEFAULT_METRICS_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    IMPORT_LOG_TEXT,
    MIN_METRICS_INTERVAL,
    MIN_SCAN_INTERVAL,
    SLOW_LOG_TEXT,
)

_LOGGER = logging.getLogger(__name__)

PARALLEL_CALLS = 4  # Render allows 400 GETs a minute; stay far below it
DEPLOYS_READ = 50  # newest deploys read each cycle (enough for "today" and the last one)
EVENTS_READ = 500
LOG_LINES = 300  # per log search: 3 pages of 100
RESOLUTION_SECONDS = 300
_ID = re.compile(r"\b(srv|crn|tea|usr|dep|evt|dsk)-[0-9a-z]+", re.IGNORECASE)


def safe_error(err: Exception) -> str:
    """An error text for attributes/diagnostics: never the key, and no Render ids."""
    return _ID.sub(lambda m: f"{m.group(1)}-…", str(err) or type(err).__name__)


def _oldest(rows: list[dict[str, Any]] | None) -> datetime | None:
    times = [t for t in (parse_time(r.get("timestamp")) for r in rows or []) if t]
    return min(times) if times else None


class RenderCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Reads every selected service of one workspace."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: RenderClient) -> None:
        self.entry = entry
        self.client = client
        minutes = max(MIN_SCAN_INTERVAL, int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)))
        self.metrics_interval = timedelta(minutes=max(
            MIN_METRICS_INTERVAL, int(entry.options.get(CONF_METRICS_INTERVAL, DEFAULT_METRICS_INTERVAL))
        ))
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"Render {entry.data.get(CONF_OWNER_NAME) or 'workspace'}",
            update_interval=timedelta(minutes=minutes),
        )
        self._semaphore = asyncio.Semaphore(PARALLEL_CALLS)
        self.metrics_at: datetime | None = None
        self._metrics: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._metric_errors: dict[str, str] = {}
        self._logs: dict[str, Any] = {
            "slow": {}, "import": {}, "slow_truncated": False, "slow_since": None, "error": None,
        }
        # instance ids created today, per service, as seen across polls: (local day, ids)
        self._instances: dict[str, tuple[str, set[str]]] = {}
        self._raw: dict[tuple[str, str], list[dict[str, Any]] | None] = {}

    @property
    def owner_id(self) -> str:
        return self.entry.data[CONF_OWNER_ID]

    def selected(self, services: list[dict[str, Any]]) -> list[dict[str, Any]]:
        wanted = set(self.entry.options.get(CONF_SERVICES) or [])
        return [s for s in services if s.get("id") and (not wanted or s["id"] in wanted)]

    async def _call(self, func, *args, **kwargs):
        async with self._semaphore:
            return await func(*args, **kwargs)

    async def _try(self, errors: dict[str, str], what: str, func, *args, **kwargs):
        """One call whose failure is reported on the service instead of failing the cycle."""
        try:
            return await self._call(func, *args, **kwargs)
        except RenderAuthError:
            raise
        except RenderError as err:
            errors[what] = safe_error(err)
            _LOGGER.debug("Render %s failed: %s", what, errors[what])
            return None

    # ------------------------------------------------------------------ cycle

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self._update()
        except RenderAuthError as err:
            raise ConfigEntryAuthFailed("Render rejected the API key") from err

    async def _update(self) -> dict[str, Any]:
        now = dt_util.utcnow()
        day_start = dt_util.as_utc(dt_util.start_of_local_day())
        try:
            services = self.selected(await self._call(self.client.list_services, self.owner_id))
        except RenderAuthError:
            raise
        except RenderError as err:
            raise UpdateFailed(safe_error(err)) from err

        fast = await asyncio.gather(*(self._read_service(s, now) for s in services))

        metrics_due = (
            self.metrics_at is None
            or now - self.metrics_at >= self.metrics_interval
            or any(s["id"] not in self._metrics for s in services if s.get("type") not in NO_METRICS_TYPES)
        )
        if metrics_due:
            await self._read_metrics_and_logs(services, now)

        local_tz = dt_util.get_default_time_zone()
        result: dict[str, Any] = {}
        for service, (deploys, events, instances, errors) in zip(services, fast):
            sid = service["id"]
            seen = self._track_instances(sid, instances, day_start)
            snap = build_snapshot(
                service, deploys or [], events or [],
                now=now, day_start=day_start,
                metrics=self._metrics.get(sid),
                slow_times=self._logs["slow"].get(sid, []),
                slow_truncated=self._logs["slow_truncated"],
                slow_complete_from=self._logs.get("slow_since"),
                import_times=self._logs["import"].get(sid, []),
                seen_instance_ids=seen,
            )
            if sid in self._metric_errors:
                errors["metrics"] = self._metric_errors[sid]
            if self._logs["error"] and service.get("type") in HTTP_TYPES:
                errors["logs"] = self._logs["error"]
            snap["errors"] = errors
            snap["advice"] = build_advice(snap, local_tz)
            snap["needs_attention"] = needs_attention(snap["advice"]) or snap["status"] == "deploy_failed"
            result[sid] = snap
        return {"updated_at": now, "metrics_at": self.metrics_at, "services": result}

    async def _read_service(self, service: dict[str, Any], now: datetime):
        sid = service["id"]
        errors: dict[str, str] = {}
        deploys = await self._try(errors, "deploys", self.client.list_deploys, sid, max_items=DEPLOYS_READ)
        events = await self._try(
            errors, "events", self.client.list_events, sid,
            start=now - timedelta(hours=24), end=now, max_items=EVENTS_READ,
        )
        instances = None
        if service.get("type") not in NO_METRICS_TYPES:
            instances = await self._try(errors, "instances", self.client.list_instances, sid)
        # A failed read keeps the last good answer (the error is still shown on the service).
        if deploys is None:
            deploys = self._raw.get((sid, "deploys"))
        if events is None:
            events = self._raw.get((sid, "events"))
        self._raw[(sid, "deploys")], self._raw[(sid, "events")] = deploys, events
        return deploys, events, instances, errors

    def _track_instances(self, sid: str, instances: list[dict[str, Any]] | None, day_start: datetime) -> set[str]:
        day = day_start.isoformat()
        known_day, ids = self._instances.get(sid, (day, set()))
        if known_day != day:
            ids = set()
        for row in instances or []:
            created = parse_time(row.get("createdAt"))
            if row.get("id") and created and created >= day_start:
                ids.add(row["id"])
        self._instances[sid] = (day, ids)
        return ids

    async def _read_metrics_and_logs(self, services: list[dict[str, Any]], now: datetime) -> None:
        start = now - timedelta(hours=24)
        watched = [s for s in services if s.get("type") not in NO_METRICS_TYPES]
        metrics: dict[str, dict[str, list[dict[str, Any]]]] = {}
        errors: dict[str, str] = {}

        async def one(service: dict[str, Any]) -> None:
            sid = service["id"]
            names = ["memory", "memory-limit", "cpu", "cpu-limit"]
            if service.get("type") in HTTP_TYPES:
                names += ["http-latency", "http-requests"]
            disk = (service.get("serviceDetails") or {}).get("disk")
            if disk:
                names += ["disk-usage", "disk-capacity"]
            found: dict[str, list[dict[str, Any]]] = {}
            failures: dict[str, str] = {}
            for name in names:
                extra = {"quantile": 0.95} if name == "http-latency" else {}
                rows = await self._try(
                    failures, name, self.client.get_metric, name, sid,
                    start=start, end=now, resolution_seconds=RESOLUTION_SECONDS, **extra,
                )
                if rows is not None:
                    found[name] = rows
                elif name in (self._metrics.get(sid) or {}):  # keep the cached series
                    found[name] = self._metrics[sid][name]
            metrics[sid] = found
            if failures:
                errors[sid] = "; ".join(f"{k}: {v}" for k, v in failures.items())

        await asyncio.gather(*(one(s) for s in watched))
        self._metrics = metrics
        self._metric_errors = errors
        await self._read_logs([s["id"] for s in services if s.get("type") in HTTP_TYPES], start, now)
        self.metrics_at = now

    async def _read_logs(self, ids: list[str], start: datetime, now: datetime) -> None:
        """Two searches for all web services together: slow-request lines and import lines."""
        if not ids:
            self._logs = {"slow": {}, "import": {}, "slow_truncated": False, "slow_since": None, "error": None}
            return
        failures: dict[str, str] = {}
        slow = await self._try(
            failures, "slow logs", self.client.list_logs, self.owner_id, ids,
            start=start, end=now, text=[SLOW_LOG_TEXT], max_items=LOG_LINES,
        )
        imports = await self._try(
            failures, "import logs", self.client.list_logs, self.owner_id, ids,
            start=start, end=now, text=[IMPORT_LOG_TEXT], max_items=LOG_LINES,
        )
        self._logs = {
            "slow": logs_by_service(slow) if slow is not None else self._logs["slow"],
            "import": logs_by_service(imports) if imports is not None else self._logs["import"],
            # A failed search keeps the last answer, and with it whether that one was cut short.
            "slow_truncated": len(slow) >= LOG_LINES if slow is not None else self._logs["slow_truncated"],
            # the oldest line the capped search returned: hours from then on are complete
            "slow_since": _oldest(slow) if slow is not None else self._logs.get("slow_since"),
            "error": "; ".join(f"{k}: {v}" for k, v in failures.items()) or None,
        }
