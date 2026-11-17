"""Pure functions that turn Render API answers into one summary per service.

Nothing here does I/O or imports Home Assistant, so every rule is unit tested with plain data.
Times are timezone-aware datetimes; Render sends ISO 8601 UTC strings.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from .const import (
    DEPLOY_FAILED,
    DEPLOY_RESTARTED,
    DEPLOY_RUNNING,
    DISK_PRICE_PER_GB,
    INSTANCE_TYPES,
)

GB = 1024 ** 3
MB = 1024 ** 2
SERIES_POINTS = 48  # points kept for the card's mini chart (30 min each over 24 h)
NO_METRICS_TYPES = frozenset({"static_site", "cron_job"})
HTTP_TYPES = frozenset({"web_service"})


# ------------------------------------------------------------------ basics

def parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    # Render sometimes sends 6+ fractional digits; fromisoformat accepts up to 6.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = "".join(ch for ch in tail if ch.isdigit())
        rest = tail[len(digits):]
        text = f"{head}.{digits[:6].ljust(6, '0')}{rest}"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def iso(moment: datetime | None) -> str | None:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def plan_info(plan: str | None) -> dict[str, Any] | None:
    for row in INSTANCE_TYPES:
        if row["plan"] == plan:
            return row
    return None


def next_bigger_plan(plan: str | None) -> dict[str, Any] | None:
    """The cheapest instance type with more memory than ``plan`` (and at least its CPU)."""
    current = plan_info(plan)
    if current is None:
        return None
    bigger = [
        row for row in INSTANCE_TYPES
        if row["memory_gb"] > current["memory_gb"] and row["cpu"] >= current["cpu"]
    ]
    return min(bigger, key=lambda row: row["price"]) if bigger else None


def details(service: dict[str, Any]) -> dict[str, Any]:
    value = service.get("serviceDetails")
    return value if isinstance(value, dict) else {}


def disk_size_gb(service: dict[str, Any]) -> float:
    disk = details(service).get("disk")
    if isinstance(disk, dict):
        try:
            return float(disk.get("sizeGB") or 0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def is_suspended(service: dict[str, Any]) -> bool:
    return service.get("suspended") == "suspended"


def instance_count(service: dict[str, Any]) -> int:
    try:
        return max(1, int(details(service).get("numInstances") or 1))
    except (TypeError, ValueError):
        return 1


def running_cost(service: dict[str, Any]) -> float | None:
    """USD per month while running: instances x plan + disk GB x $0.25. None when unknown."""
    kind = service.get("type")
    if kind == "static_site":
        return 0.0
    if kind == "cron_job":
        return None  # billed per second of run time
    info = plan_info(details(service).get("plan"))
    if info is None:
        return None
    return round(info["price"] * instance_count(service) + disk_size_gb(service) * DISK_PRICE_PER_GB, 2)


def monthly_cost(service: dict[str, Any]) -> float | None:
    """USD per month now. A suspended service is not billed for its instances, but a persistent
    disk keeps its data and is still billed, so only the disk counts then."""
    if is_suspended(service) and service.get("type") not in ("static_site", "cron_job"):
        return round(disk_size_gb(service) * DISK_PRICE_PER_GB, 2)
    return running_cost(service)


# ------------------------------------------------------------------ deploys and events

def failure_reason(reason: Any) -> str:
    """A short code for why a deploy or instance failed, from an event's ``reason`` object."""
    if not isinstance(reason, dict):
        return "unknown"
    if "buildFailed" in reason:
        return "build_failed"
    if "newDeploy" in reason or "newBuild" in reason:
        return "superseded"
    failure = reason.get("failure") if isinstance(reason.get("failure"), dict) else reason
    if failure.get("oomKilled"):
        return "oom"
    timed_out = str(failure.get("timedOutReason") or "").lower()
    if timed_out or failure.get("timedOutSeconds"):
        return "no_port" if "port" in timed_out else "timeout"
    if failure.get("unhealthy"):
        return "unhealthy"
    if failure.get("nonZeroExit"):
        return "exit_code"
    if failure.get("earlyExit"):
        return "early_exit"
    if failure.get("evicted"):
        return "evicted"
    return "unknown"


def exit_code(reason: Any) -> int | None:
    if not isinstance(reason, dict):
        return None
    failure = reason.get("failure") if isinstance(reason.get("failure"), dict) else reason
    code = failure.get("nonZeroExit")
    return code if isinstance(code, int) else None


def deploy_view(deploy: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """The fields the sensors and card show for one deploy, plus the failure reason from events."""
    commit = deploy.get("commit") if isinstance(deploy.get("commit"), dict) else {}
    created, finished = parse_time(deploy.get("createdAt")), parse_time(deploy.get("finishedAt"))
    status = deploy.get("status") or "unknown"
    reason = None
    code = None
    if status in DEPLOY_FAILED:
        reason = "build_failed" if status == "build_failed" else "unknown"
        for event in events:
            det = event.get("details") if isinstance(event.get("details"), dict) else {}
            if event.get("type") == "deploy_ended" and det.get("deployId") == deploy.get("id"):
                found = failure_reason(det.get("reason"))
                if found != "unknown":
                    reason = found
                code = exit_code(det.get("reason"))
    message = str(commit.get("message") or "").strip().splitlines()
    return {
        "id": deploy.get("id"),
        "status": status,
        "trigger": deploy.get("trigger"),
        "commit": str(commit.get("id") or "")[:7] or None,
        "message": (message[0][:120] if message else None),
        "created_at": created,
        "finished_at": finished,
        "duration_s": int((finished - created).total_seconds()) if created and finished else None,
        "reason": reason,
        "exit_code": code,
    }


def service_status(service: dict[str, Any], deploys: list[dict[str, Any]]) -> str:
    """live / deploying / deploy_failed / suspended / not_deployed (deploys newest first)."""
    if is_suspended(service):
        return "suspended"
    if not deploys:
        return "not_deployed"
    latest = deploys[0].get("status")
    if latest in DEPLOY_RUNNING:
        return "deploying"
    if latest in DEPLOY_FAILED:
        return "deploy_failed"
    return "live"


def today(rows: Iterable[dict[str, Any]], day_start: datetime, key: str) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        moment = parse_time(row.get(key))
        if moment and moment >= day_start:
            out.append(row)
    return out


def restarts_today(
    deploys: list[dict[str, Any]],
    events: list[dict[str, Any]],
    day_start: datetime,
    seen_instance_ids: Iterable[str] = (),
) -> dict[str, int]:
    """New instances today. Each finished deploy and each crash/restart replaces an instance.

    ``seen_instance_ids`` are instance ids created today that the coordinator saw between polls;
    the larger of the two counts is used so nothing is counted twice.
    """
    live = sum(
        1 for d in today(deploys, day_start, "createdAt")
        if d.get("status") in ("live", "deactivated") and d.get("finishedAt")
    )
    crashes = sum(1 for e in today(events, day_start, "timestamp") if e.get("type") == "server_failed")
    restarts = sum(1 for e in today(events, day_start, "timestamp") if e.get("type") == "server_restarted")
    seen = len(set(seen_instance_ids))
    return {
        "total": max(live + crashes + restarts, seen),
        "deploys": live,
        "crashes": crashes,
        "restarts": restarts,
        "instances_seen": seen,
    }


def oom_events(events: list[dict[str, Any]], since: datetime) -> list[datetime]:
    out = []
    for event in events:
        det = event.get("details") if isinstance(event.get("details"), dict) else {}
        moment = parse_time(event.get("timestamp"))
        if moment is None or moment < since:
            continue
        if event.get("type") == "server_failed" and failure_reason(det.get("reason")) == "oom":
            out.append(moment)
        elif event.get("type") == "deploy_ended" and failure_reason(det.get("reason")) == "oom":
            out.append(moment)
    return out


# ------------------------------------------------------------------ metrics

def to_bytes(value: float, unit: str | None) -> float:
    unit = (unit or "").lower()
    if unit in ("gb", "gib"):
        return value * GB
    if unit in ("mb", "mib"):
        return value * MB
    if unit in ("kb", "kib"):
        return value * 1024
    return value


def points(rows: list[dict[str, Any]], *, memory: bool = False) -> list[tuple[datetime, float]]:
    """Every (time, value) of every series (instance), in time order."""
    out: list[tuple[datetime, float]] = []
    for row in rows or []:
        unit = row.get("unit")
        for value in row.get("values") or []:
            if not isinstance(value, dict):
                continue
            moment, number = parse_time(value.get("timestamp")), value.get("value")
            if moment is None or not isinstance(number, (int, float)) or isinstance(number, bool):
                continue
            if isinstance(number, float) and math.isnan(number):
                continue
            out.append((moment, to_bytes(float(number), unit) if memory else float(number)))
    out.sort(key=lambda p: p[0])
    return out


def max_by_time(pts: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    """Several instances report the same timestamps: keep the highest one."""
    best: dict[datetime, float] = {}
    for moment, value in pts:
        best[moment] = max(value, best.get(moment, value))
    return sorted(best.items())


def downsample(pts: list[tuple[datetime, float]], start: datetime, end: datetime,
               n: int = SERIES_POINTS) -> list[list[Any]]:
    """[[iso, max value in bucket]] with n buckets; empty buckets are left out."""
    if not pts or end <= start:
        return []
    step = (end - start) / n
    buckets: dict[int, float] = {}
    for moment, value in pts:
        if moment < start or moment > end:
            continue
        index = min(n - 1, int((moment - start) / step))
        buckets[index] = max(value, buckets.get(index, value))
    return [[iso(start + step * i), round(v, 1)] for i, v in sorted(buckets.items())]


def _limit(rows: list[dict[str, Any]], *, memory: bool) -> float | None:
    values = [v for _, v in points(rows, memory=memory) if v > 0]
    return max(values) if values else None


def memory_stats(memory_rows, limit_rows, plan: str | None, start: datetime, end: datetime) -> dict[str, Any] | None:
    pts = [p for p in max_by_time(points(memory_rows, memory=True)) if start <= p[0] <= end]
    if not pts:
        return None
    limit = _limit(limit_rows, memory=True)
    if limit is None:
        info = plan_info(plan)
        limit = info["memory_gb"] * GB if info else None
    peak_at, peak = max(pts, key=lambda p: p[1])
    out: dict[str, Any] = {
        "peak_mb": round(peak / MB),
        "peak_at": peak_at,
        "limit_mb": round(limit / MB) if limit else None,
        "peak_pct": round(peak / limit * 100, 1) if limit else None,
        "series": [],
    }
    if limit:
        out["series"] = downsample([(t, v / limit * 100) for t, v in pts], start, end)
    return out


def cpu_stats(cpu_rows, limit_rows, plan: str | None, start: datetime, end: datetime) -> dict[str, Any] | None:
    pts = [p for p in max_by_time(points(cpu_rows)) if start <= p[0] <= end]
    if not pts:
        return None
    limit = _limit(limit_rows, memory=False)
    if limit is None:
        info = plan_info(plan)
        limit = info["cpu"] if info else None
    if not limit:
        return None
    pct = [(t, v / limit * 100) for t, v in pts]
    return {
        "avg_pct": round(sum(v for _, v in pct) / len(pct), 1),
        "peak_pct": round(max(v for _, v in pct), 1),
        "limit": limit,
        "series": downsample(pct, start, end),
    }


def latency_stats(rows, start: datetime, end: datetime) -> dict[str, Any] | None:
    pts = [p for p in max_by_time(points(rows)) if start <= p[0] <= end]
    if not pts:
        return None
    peak_at, peak = max(pts, key=lambda p: p[1])
    return {
        "p95_ms": round(pts[-1][1], 1),
        "peak_ms": round(peak, 1),
        "peak_at": peak_at,
        "median_ms": round(median(v for _, v in pts), 1),
        "points": pts,
    }


def requests_total(rows) -> float | None:
    pts = points(rows)
    return sum(v for _, v in pts) if pts else None


def disk_stats(usage_rows, capacity_rows, size_gb: float) -> dict[str, Any] | None:
    used = points(usage_rows, memory=True)
    if not used:
        return None
    capacity = _limit(capacity_rows, memory=True) or (size_gb * GB if size_gb else None)
    if not capacity:
        return None
    latest = used[-1][1]
    return {
        "used_gb": round(latest / GB, 2),
        "size_gb": round(capacity / GB, 2),
        "used_pct": round(latest / capacity * 100, 1),
    }


# ------------------------------------------------------------------ logs

def log_resource(row: dict[str, Any]) -> str | None:
    for label in row.get("labels") or []:
        if isinstance(label, dict) and label.get("name") == "resource":
            return label.get("value")
    return None


def logs_by_service(rows: list[dict[str, Any]]) -> dict[str, list[datetime]]:
    out: dict[str, list[datetime]] = {}
    for row in rows or []:
        resource, moment = log_resource(row), parse_time(row.get("timestamp"))
        if resource and moment:
            out.setdefault(resource, []).append(moment)
    for times in out.values():
        times.sort()
    return out


def slow_stats(
    times: list[datetime], now: datetime, *, truncated: bool = False,
    complete_from: datetime | None = None,
) -> dict[str, Any]:
    """Slow-request log lines per hour over the last 24 h (oldest hour first).

    ``truncated``: the search stopped at its line cap, so older hours may be missing lines;
    ``complete_from``: the time of the oldest line that search returned (hours from then on
    are complete).
    """
    end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    start = end - timedelta(hours=24)
    per_hour = [0] * 24
    for moment in times:
        if start <= moment < end:
            per_hour[int((moment - start).total_seconds() // 3600)] += 1
    last_hour = sum(1 for moment in times if now - timedelta(hours=1) <= moment <= now)
    return {
        "last_hour": last_hour,
        "per_hour": [[iso(start + timedelta(hours=i)), n] for i, n in enumerate(per_hour)],
        "total_24h": sum(per_hour),
        "truncated": truncated,
        "complete_from": iso(complete_from) if truncated else None,
    }


# ------------------------------------------------------------------ one service

def _dashboard_url(service: dict[str, Any]) -> str | None:
    url = service.get("dashboardUrl")
    return url if isinstance(url, str) and url.startswith("https://dashboard.render.com/") else None


def build_snapshot(
    service: dict[str, Any],
    deploys: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    now: datetime,
    day_start: datetime,
    metrics: dict[str, list[dict[str, Any]]] | None = None,
    slow_times: list[datetime] | None = None,
    slow_truncated: bool = False,
    slow_complete_from: datetime | None = None,
    import_times: list[datetime] | None = None,
    seen_instance_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Everything the entities, the card and the advice rules need for one service.

    ``deploys`` are newest first (as Render sends them); ``metrics`` maps a metric name to
    GET /metrics rows covering the last 24 h; ``*_times`` are log line times for this service.
    """
    det = details(service)
    plan = det.get("plan")
    kind = service.get("type")
    start = now - timedelta(hours=24)
    metrics = metrics or {}
    views = [deploy_view(d, events) for d in deploys]
    todays = [v for v in views if v["created_at"] and v["created_at"] >= day_start]
    # Deploys that reached the update stage: on a service with a disk each one stopped it.
    restarting = [v for v in todays if v["status"] in DEPLOY_RESTARTED]
    live_times = [v["created_at"] for v in views if v["status"] in ("live", "deactivated") and v["created_at"]]
    windows = [
        (v["created_at"], v["finished_at"] or now)
        for v in views if v["created_at"] and v["created_at"] >= start
    ]
    snap: dict[str, Any] = {
        "id": service.get("id"),
        "name": service.get("name"),
        "type": kind,
        "dashboard_url": _dashboard_url(service),
        "plan": plan,
        "region": det.get("region"),
        "instances": instance_count(service),
        "suspended": is_suspended(service),
        "suspenders": [s for s in service.get("suspenders") or [] if isinstance(s, str)],
        "disk_gb": disk_size_gb(service),
        "monthly_cost": monthly_cost(service),
        "running_cost": running_cost(service),
        "status": service_status(service, deploys),
        "last_deploy": views[0] if views else None,
        "deploys_today": len(todays),
        "failed_today": [v for v in todays if v["status"] in DEPLOY_FAILED],
        "deploy_durations": [v["duration_s"] for v in todays if v["duration_s"] is not None],
        "restarting_deploys_today": len(restarting),
        "restarting_durations": [v["duration_s"] for v in restarting if v["duration_s"] is not None],
        "live_since": max(live_times) if live_times else None,
        "deploy_windows": windows,
        "restarts": restarts_today(deploys, events, day_start, seen_instance_ids),
        "oom_times": oom_events(events, start),
        "import_times": sorted(import_times or []),
        "memory": None,
        "cpu": None,
        "latency": None,
        "slow": None,
        "requests_24h": None,
        "disk": None,
    }
    if kind not in NO_METRICS_TYPES:
        snap["memory"] = memory_stats(metrics.get("memory"), metrics.get("memory-limit"), plan, start, now)
        snap["cpu"] = cpu_stats(metrics.get("cpu"), metrics.get("cpu-limit"), plan, start, now)
        if snap["disk_gb"]:
            snap["disk"] = disk_stats(metrics.get("disk-usage"), metrics.get("disk-capacity"), snap["disk_gb"])
    if kind in HTTP_TYPES:
        snap["latency"] = latency_stats(metrics.get("http-latency"), start, now)
        if "http-requests" in metrics:
            snap["requests_24h"] = requests_total(metrics.get("http-requests"))
        snap["slow"] = slow_stats(
            slow_times or [], now, truncated=slow_truncated, complete_from=slow_complete_from,
        )
    return snap
