"""Pure summary helpers: times, prices, deploy reasons, restarts, metric units, slow lines."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.render_monitor import analysis as an

from .conftest import load, unwrap

DAY = datetime(2026, 11, 16, tzinfo=timezone.utc)
MB = 1024 * 1024


def test_parse_time_forms():
    assert an.parse_time("2026-11-16T10:00:00Z") == DAY + timedelta(hours=10)
    assert an.parse_time("2026-01-05T10:00:00.123456789Z") == datetime(2026, 1, 5, 10, 0, 0, 123456, timezone.utc)
    assert an.parse_time("2026-11-16T10:00:00") == DAY + timedelta(hours=10)
    assert an.parse_time("nonsense") is None and an.parse_time(None) is None


def test_next_bigger_plan():
    assert an.next_bigger_plan("starter")["plan"] == "standard"
    assert an.next_bigger_plan("standard")["plan"] == "pro"
    assert an.next_bigger_plan("pro")["plan"] == "2c-8g"  # cheapest with more memory and >= 2 CPU
    assert an.next_bigger_plan("pro_ultra") is None
    assert an.next_bigger_plan("legacy_plan") is None


def test_monthly_cost():
    services = {s["id"]: s for s in unwrap("services.json", "service")}
    assert an.monthly_cost(services["srv-00000000000000000001"]) == 27.5  # standard $25 + 10 GB disk
    assert an.monthly_cost(services["srv-00000000000000000002"]) == 0.0  # static site
    two = {"type": "web_service", "serviceDetails": {"plan": "pro", "numInstances": 2}}
    assert an.monthly_cost(two) == 170.0
    assert an.monthly_cost({"type": "web_service", "serviceDetails": {"plan": "custom"}}) is None
    assert an.monthly_cost({"type": "cron_job", "serviceDetails": {"plan": "starter"}}) is None


@pytest.mark.parametrize(("reason", "code"), [
    ({"buildFailed": {"id": "bld-1"}}, "build_failed"),
    ({"newDeploy": {"id": "dep-2"}}, "superseded"),
    ({"failure": {"evicted": False, "oomKilled": {"memoryLimit": "512Mi"}}}, "oom"),
    ({"oomKilled": {"memoryLimit": "2Gi"}}, "oom"),  # server_failed puts the failure fields at the top
    ({"failure": {"evicted": False, "timedOutSeconds": 900, "timedOutReason": "No open ports detected"}}, "no_port"),
    ({"failure": {"evicted": False, "timedOutSeconds": 900}}, "timeout"),
    ({"failure": {"evicted": False, "unhealthy": "GET /health returned 500"}}, "unhealthy"),
    ({"failure": {"evicted": False, "nonZeroExit": 1}}, "exit_code"),
    ({"failure": {"evicted": False, "earlyExit": True}}, "early_exit"),
    ({"failure": {"evicted": True}}, "evicted"),
    ({}, "unknown"),
    (None, "unknown"),
])
def test_failure_reason(reason, code):
    assert an.failure_reason(reason) == code


def test_deploy_view_and_status():
    deploys = unwrap("deploys.json", "deploy")
    events = [{"type": "deploy_ended", "details": {
        "deployId": "dep-00000000000000000003", "deployStatus": "failed",
        "reason": {"buildFailed": {"id": "bld-1"}}}}]
    view = an.deploy_view(deploys[0], events)
    assert view["status"] == "build_failed" and view["reason"] == "build_failed"
    assert view["commit"] == "0000000" and view["message"] == "Speed up the import job"
    assert view["duration_s"] == 220
    assert an.service_status({"suspended": "not_suspended"}, deploys) == "deploy_failed"
    assert an.service_status({"suspended": "suspended"}, deploys) == "suspended"
    assert an.service_status({}, []) == "not_deployed"
    assert an.service_status({}, [{"status": "build_in_progress"}]) == "deploying"
    assert an.service_status({}, deploys[1:]) == "live"


def test_restarts_today_counts_once():
    deploys = unwrap("deploys.json", "deploy")  # one live today, one failed today, one yesterday
    events = unwrap("events.json", "event")  # one OOM crash today
    out = an.restarts_today(deploys, events, DAY)
    assert out == {"total": 2, "deploys": 1, "crashes": 1, "restarts": 0, "instances_seen": 0}
    # Instance ids seen between polls are not added on top: the larger count wins.
    assert an.restarts_today(deploys, events, DAY, ["a", "b", "c"])["total"] == 3


def test_memory_units_and_limit_fallback():
    rows = load("metrics_memory.json")  # bytes, peak 1700 MB
    stats = an.memory_stats(rows, [], "standard", DAY, DAY + timedelta(hours=20))
    assert stats["peak_mb"] == 1700 and stats["limit_mb"] == 2048 and stats["peak_pct"] == 83.0
    in_gb = [{"labels": [], "values": [{"timestamp": "2026-11-16T08:00:00Z", "value": 0.25}], "unit": "GB"}]
    limit = [{"labels": [], "values": [{"timestamp": "2026-11-16T08:00:00Z", "value": 0.5}], "unit": "GB"}]
    stats = an.memory_stats(in_gb, limit, None, DAY, DAY + timedelta(hours=20))
    assert stats["peak_mb"] == 256 and stats["peak_pct"] == 50.0
    assert an.memory_stats([], [], "standard", DAY, DAY + timedelta(hours=1)) is None


def test_highest_instance_wins():
    rows = [
        {"labels": [], "values": [{"timestamp": "2026-11-16T08:00:00Z", "value": 100 * MB}], "unit": "bytes"},
        {"labels": [], "values": [{"timestamp": "2026-11-16T08:00:00Z", "value": 300 * MB}], "unit": "bytes"},
    ]
    stats = an.memory_stats(rows, [], "starter", DAY, DAY + timedelta(hours=20))
    assert stats["peak_mb"] == 300 and len(stats["series"]) == 1


def test_slow_stats_buckets():
    now = DAY + timedelta(hours=20, minutes=30)
    times = [DAY + timedelta(hours=20, minutes=5), DAY + timedelta(hours=19, minutes=50), DAY + timedelta(hours=3)]
    stats = an.slow_stats(times, now)
    assert stats["last_hour"] == 2 and stats["total_24h"] == 3
    assert stats["per_hour"][-1] == ["2026-11-16T20:00:00Z", 1]
    assert len(stats["per_hour"]) == 24


def test_logs_by_service():
    logs = load("logs.json")["logs"]
    assert {k: len(v) for k, v in an.logs_by_service(logs).items()} == {"srv-00000000000000000001": 2}
