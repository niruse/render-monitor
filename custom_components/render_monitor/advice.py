"""Advice from fixed rules - no AI. Each rule is a pure function of one service summary.

An advice item is ``{"severity", "code", "text_key", "values"}``. ``text_key`` names a text in
www/advice_texts.json (English); ``values`` fill its {placeholders} and are plain
strings/numbers so they can be stored as entity attributes.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timedelta, tzinfo
from statistics import median
from typing import Any

from .analysis import iso, next_bigger_plan, parse_time, plan_info
from .const import (
    CAUSE_WINDOW_MINUTES,
    DISK_CRITICAL_PCT,
    DISK_HIGH_PCT,
    DISK_PRICE_PER_GB,
    LATENCY_SPIKE_MS,
    MANY_DEPLOYS_PER_DAY,
    MEMORY_HIGH_PCT,
    PRICES_DATE,
    PRICING_URL,
    SEVERITIES,
    SLOW_SPIKE_PER_HOUR,
)

Advice = dict[str, Any]
Snapshot = dict[str, Any]


def item(severity: str, code: str, text_key: str, **values: Any) -> Advice:
    return {"severity": severity, "code": code, "text_key": text_key, "values": values}


def _clock(moment: datetime | None, tz: tzinfo | None) -> str | None:
    if moment is None:
        return None
    return (moment.astimezone(tz) if tz else moment).strftime("%H:%M")


def _during_deploy(start: datetime, end: datetime, windows: list[tuple[datetime, datetime]]) -> bool:
    """Does [start, end] overlap a deploy (created .. finished + the cause window)?"""
    gap = timedelta(minutes=CAUSE_WINDOW_MINUTES)
    return any(d_start - gap <= end and start <= d_end + gap for d_start, d_end in windows)


IMPORT_GAP = timedelta(minutes=60)


def import_spans(times: list[datetime]) -> list[tuple[datetime, datetime]]:
    """Import log lines less than an hour apart are one running import (start line .. last line)."""
    spans: list[list[datetime]] = []
    for moment in sorted(times):
        if spans and moment - spans[-1][1] <= IMPORT_GAP:
            spans[-1][1] = moment
        else:
            spans.append([moment, moment])
    return [(a, b) for a, b in spans]


def cause_of(start: datetime, end: datetime, snap: Snapshot) -> str:
    """import / deploy / import_deploy / unknown for something that happened in [start, end]."""
    gap = timedelta(minutes=CAUSE_WINDOW_MINUTES)
    by_import = any(
        s_start - gap <= end and start <= s_end + gap
        for s_start, s_end in import_spans(snap.get("import_times") or [])
    )
    by_deploy = _during_deploy(start, end, snap.get("deploy_windows") or [])
    if by_import and by_deploy:
        return "import_deploy"
    if by_import:
        return "import"
    if by_deploy:
        return "deploy"
    return "unknown"


# ------------------------------------------------------------------ rules

def rule_memory(snap: Snapshot, tz: tzinfo | None = None) -> list[Advice]:
    """Memory peak >= 80% of the limit, or an out-of-memory kill: name the next instance type."""
    memory = snap.get("memory") or {}
    ooms: list[datetime] = snap.get("oom_times") or []
    peak = memory.get("peak_pct")
    if not ooms and (peak is None or peak < MEMORY_HIGH_PCT):
        return []
    plan = snap.get("plan")
    current = plan_info(plan)
    bigger = next_bigger_plan(plan)
    values: dict[str, Any] = {
        "plan": plan,
        "peak_pct": round(peak) if peak is not None else None,
        "peak_mb": memory.get("peak_mb"),
        "limit_mb": memory.get("limit_mb"),
        "oom_count": len(ooms),
        "prices_date": PRICES_DATE,
        "pricing_url": PRICING_URL,
    }
    # An OOM text says "last at {time}": that is the last kill, not the 24-hour memory peak,
    # and the cause (import / deploy) is looked up at that same moment.
    moment = max(ooms) if ooms else memory.get("peak_at")
    values["at"] = iso(moment)
    values["time"] = _clock(moment, tz)
    if bigger is None:
        key = "memory_high_max"
    else:
        values.update(
            next_plan=bigger["plan"],
            next_label=bigger.get("label") or bigger["plan"],
            next_memory_gb=bigger["memory_gb"],
            next_price=bigger["price"],
            price_delta=round(bigger["price"] - current["price"], 2) if current else None,
        )
        key = "memory_high"
    cause = cause_of(moment, moment, snap) if moment else "unknown"
    values["cause"] = cause
    if ooms:
        key = key.replace("memory_high", "memory_oom")
    elif cause in ("import", "import_deploy"):
        key = f"{key}_import"
    return [item("critical" if ooms else "warning", "memory", key, **values)]


def rule_failed_deploy(snap: Snapshot, tz: tzinfo | None = None) -> list[Advice]:
    """A failed deploy today (or the latest one failed): its commit and the reason."""
    failed = snap.get("failed_today") or []
    last = snap.get("last_deploy") or {}
    latest_failed = last.get("status") in ("build_failed", "update_failed", "pre_deploy_failed")
    target = last if latest_failed else (failed[0] if failed else None)
    if not target:
        return []
    values = {
        "status": target.get("status"),
        "commit": target.get("commit"),
        "message": target.get("message"),
        "reason": target.get("reason") or "unknown",
        "exit_code": target.get("exit_code"),
        "at": iso(target.get("created_at")),
        "time": _clock(target.get("created_at"), tz),
        "failed_count": max(len(failed), 1),
    }
    if latest_failed:
        return [item("warning", "deploy_failed", "deploy_failed", **values)]
    # "A later deploy is live" only when a deploy newer than the failed one really went live;
    # a later deploy that is still building, or was canceled, has not fixed anything yet.
    live_at = snap.get("live_since")
    failed_at = target.get("created_at")
    if live_at and failed_at and live_at > failed_at:
        return [item("info", "deploy_failed", "deploy_failed_recovered", **values)]
    return [item("warning", "deploy_failed", "deploy_failed_earlier", **values)]


def rule_many_deploys(snap: Snapshot, tz: tzinfo | None = None) -> list[Advice]:
    """Many deploys a day on a service with a disk: each one stops the service (no zero-downtime).

    Only deploys that reached the update stage count (``restarting_deploys_today``): a deploy
    that failed in the build, or was canceled before it, never stopped the running instance.
    ``deploy_min`` is the total length of those deploys (build included): Render does not say
    when the build ended, so this is deploy time, not measured downtime.
    """
    count = snap.get("restarting_deploys_today")
    if count is None:
        count = snap.get("deploys_today") or 0
    disk = snap.get("disk_gb") or 0
    if count < MANY_DEPLOYS_PER_DAY or not disk:
        return []
    durations = [d for d in snap.get("restarting_durations", snap.get("deploy_durations")) or []
                 if isinstance(d, (int, float))]
    return [item(
        "warning", "many_deploys", "many_deploys_disk",
        count=count,
        disk_gb=disk,
        deploy_min=round(sum(durations) / 60) if durations else None,
    )]


def _latency_spikes(snap: Snapshot) -> list[tuple[datetime, float]]:
    latency = snap.get("latency") or {}
    pts: list[tuple[datetime, float]] = latency.get("points") or []
    if not pts:
        return []
    mid = median(v for _, v in pts)
    spikes = [(t, v) for t, v in pts if v >= LATENCY_SPIKE_MS and v >= 2 * mid]
    # One spike per 30 minutes: keep the highest point of each burst.
    grouped: list[tuple[datetime, float]] = []
    for moment, value in spikes:
        if grouped and moment - grouped[-1][0] <= timedelta(minutes=30):
            if value > grouped[-1][1]:
                grouped[-1] = (moment, value)
            continue
        grouped.append((moment, value))
    return grouped


MIN_COMPLETE_HOURS = 6  # fewer fully read hours than this give no fair "normal hour"


def _slow_spikes(snap: Snapshot) -> list[tuple[datetime, int]]:
    """Hours with many more "slow:" lines than the median hour.

    The log search stops after a fixed number of lines (newest first, all services together).
    When it stopped early (``truncated``), only hours that start at or after the oldest line
    read (``complete_from``) are fully counted; the older hours would read as 0 and make every
    recent hour look like a spike, so they are left out. Unknown coverage: no advice.
    """
    slow = snap.get("slow") or {}
    hours = [(parse_time(start), n) for start, n in slow.get("per_hour") or []]
    hours = [(moment, n) for moment, n in hours if moment is not None]
    if slow.get("truncated"):
        complete_from = parse_time(slow.get("complete_from"))
        if complete_from is None:
            return []
        hours = [(moment, n) for moment, n in hours if moment >= complete_from]
        if len(hours) < MIN_COMPLETE_HOURS:
            return []
    counts = [n for _, n in hours]
    if not counts:
        return []
    mid = median(counts)
    return [(moment, n) for moment, n in hours
            if n >= SLOW_SPIKE_PER_HOUR and n >= 3 * max(mid, 1)]


def rule_spikes(snap: Snapshot, tz: tzinfo | None = None) -> list[Advice]:
    """p95 latency or slow-request spikes: say whether an import or a deploy was running then."""
    out: list[Advice] = []
    latency = _latency_spikes(snap)
    if latency:
        moment, value = max(latency, key=lambda p: p[1])
        cause = cause_of(moment - timedelta(minutes=5), moment, snap)
        out.append(item(
            "info" if cause == "unknown" else "warning", "latency_spike", f"latency_spike_{cause}",
            p95_ms=round(value), median_ms=round((snap.get("latency") or {}).get("median_ms") or 0),
            spikes=len(latency), cause=cause, at=iso(moment), time=_clock(moment, tz),
        ))
    slow = _slow_spikes(snap)
    if slow:
        moment, count = max(slow, key=lambda p: p[1])
        cause = cause_of(moment, moment + timedelta(hours=1), snap)
        out.append(item(
            "info" if cause == "unknown" else "warning", "slow_spike", f"slow_spike_{cause}",
            count=count, hours=len(slow), cause=cause, at=iso(moment), time=_clock(moment, tz),
        ))
    return out


def rule_disk(snap: Snapshot, tz: tzinfo | None = None) -> list[Advice]:
    disk = snap.get("disk") or {}
    pct = disk.get("used_pct")
    if pct is None or pct < DISK_HIGH_PCT:
        return []
    size = disk.get("size_gb") or snap.get("disk_gb") or 0
    suggest = math.ceil(size * 1.5) if size else None
    return [item(
        "critical" if pct >= DISK_CRITICAL_PCT else "warning", "disk_high", "disk_high",
        used_pct=round(pct), used_gb=disk.get("used_gb"), size_gb=size, suggest_gb=suggest,
        extra_cost=round((suggest - size) * DISK_PRICE_PER_GB, 2) if suggest else None,
        prices_date=PRICES_DATE, pricing_url=PRICING_URL,
    )]


def rule_suspended_or_idle(snap: Snapshot, tz: tzinfo | None = None) -> list[Advice]:
    cost = snap.get("monthly_cost")
    if snap.get("suspended"):
        # Instances are not billed while suspended; a disk keeps its data and still is.
        key = "suspended_disk" if snap.get("disk_gb") else "suspended"
        return [item("info", "suspended", key,
                     by=", ".join(snap.get("suspenders") or []) or None,
                     monthly_cost=cost,  # what it costs now: the disk, if there is one
                     running_cost=snap.get("running_cost"),
                     prices_date=PRICES_DATE, pricing_url=PRICING_URL)]
    if snap.get("type") == "web_service" and snap.get("requests_24h") == 0 and cost:
        return [item("info", "idle", "idle", monthly_cost=cost,
                     prices_date=PRICES_DATE, pricing_url=PRICING_URL)]
    return []


RULES: tuple[Callable[[Snapshot, tzinfo | None], list[Advice]], ...] = (
    rule_failed_deploy,
    rule_memory,
    rule_disk,
    rule_many_deploys,
    rule_spikes,
    rule_suspended_or_idle,
)


def build_advice(snap: Snapshot, tz: tzinfo | None = None) -> list[Advice]:
    """Every rule's advice, most severe first (stable within a severity)."""
    found: list[Advice] = []
    for rule in RULES:
        found.extend(rule(snap, tz))
    return sorted(found, key=lambda a: SEVERITIES.index(a["severity"]))


def needs_attention(advice: list[Advice]) -> bool:
    return any(a["severity"] in ("critical", "warning") for a in advice)


def render(texts: dict[str, str], advice: Advice) -> str:
    """Fill a text's {placeholders}; a missing value shows as a dash instead of failing."""
    template = texts.get(advice["text_key"]) or advice["text_key"]
    values = {
        k: "-" if v is None else int(v) if isinstance(v, float) and v.is_integer() else v
        for k, v in advice["values"].items()
    }
    if "reason" in values:  # a code such as no_port: shown as its own text
        values["reason"] = texts.get(f"reason_{values['reason']}", values["reason"])

    class _Default(dict):
        def __missing__(self, key: str) -> str:
            return "-"

    return template.format_map(_Default(values))
