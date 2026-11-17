"""Advice rules (pure functions) and their texts. No Home Assistant needed."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from custom_components.render_monitor import advice as adv
from custom_components.render_monitor.analysis import build_snapshot, logs_by_service, parse_time

from .conftest import load

TEXTS = json.loads(
    (Path(__file__).parent.parent / "custom_components" / "render_monitor" / "www" / "advice_texts.json")
    .read_text(encoding="utf-8")
)
PLACEHOLDER = re.compile(r"\{(\w+)\}")
NOW = datetime(2026, 11, 16, 20, 0, tzinfo=timezone.utc)
DAY = datetime(2026, 11, 16, tzinfo=timezone.utc)


def at(hour, minute=0):
    return DAY + timedelta(hours=hour, minutes=minute)


def codes(items):
    return [(a["code"], a["text_key"], a["severity"]) for a in items]


# ------------------------------------------------------------------ the replayed day

def day_snapshot():
    day = load("day_import.json")
    unwrap = lambda rows, key: [r[key] for r in rows]  # noqa: E731
    return build_snapshot(
        day["service"]["service"],
        unwrap(day["deploys"], "deploy"),
        unwrap(day["events"], "event"),
        now=parse_time(day["now"]),
        day_start=DAY,
        metrics=day["metrics"],
        slow_times=logs_by_service(day["logs_slow"]["logs"]).get("srv-00000000000000000004", []),
        import_times=logs_by_service(day["logs_import"]["logs"]).get("srv-00000000000000000004", []),
    )


def test_real_shaped_day():
    """512 MB instance peaking at 440 MB in an import, 15 deploys on a disk service, one deploy without a port."""
    snap = day_snapshot()
    assert snap["memory"]["peak_mb"] == 440
    assert snap["memory"]["limit_mb"] == 512
    assert snap["memory"]["peak_pct"] == pytest.approx(85.9, abs=0.1)
    assert snap["memory"]["peak_at"] == at(3, 20)
    assert snap["deploys_today"] == 15
    assert snap["status"] == "live"
    assert snap["monthly_cost"] == 7.25  # starter $7 + 1 GB disk $0.25
    assert snap["restarts"]["deploys"] == 14  # the failed one never replaced the instance
    assert snap["cpu"]["peak_pct"] == pytest.approx(90.0)
    assert snap["disk"]["used_pct"] == 30.0
    assert snap["slow"]["total_24h"] == 13

    items = adv.build_advice(snap)
    assert codes(items) == [
        ("memory", "memory_high_import", "warning"),
        ("many_deploys", "many_deploys_disk", "warning"),
        ("latency_spike", "latency_spike_import", "warning"),
        ("slow_spike", "slow_spike_import", "warning"),
        ("deploy_failed", "deploy_failed_recovered", "info"),
    ]
    memory = items[0]["values"]
    assert (memory["plan"], memory["next_plan"], memory["next_price"], memory["price_delta"]) == ("starter", "standard", 25.0, 18.0)
    assert memory["prices_date"] == "2026-11-16" and memory["pricing_url"] == "https://render.com/pricing"
    assert memory["time"] == "03:20"
    assert items[1]["values"]["count"] == 15 and items[1]["values"]["disk_gb"] == 1.0
    assert items[1]["values"]["deploy_min"] == 71  # 14 x 4 min + 1 x 15 min (all got past the build)
    assert snap["restarting_deploys_today"] == 15
    assert items[2]["values"]["p95_ms"] == 2400 and items[2]["values"]["median_ms"] == 120
    assert items[3]["values"]["count"] == 12 and items[3]["values"]["time"] == "03:00"
    failed = items[4]["values"]
    assert failed["reason"] == "no_port"
    assert failed["commit"] == "0000000"
    assert failed["message"] == "Read the import in batches"
    assert failed["time"] == "09:40"
    assert adv.needs_attention(items)

    en = [adv.render(TEXTS, a) for a in items]
    assert "move to Standard (2 GB at $25 per month, an additional $18 per month)" in en[0]
    assert "import" in en[0]
    assert "Reason: the service never opened its port." in en[4]


# ------------------------------------------------------------------ single rules

def snap(**kw):
    base = {"type": "web_service", "plan": "standard", "deploy_windows": [], "import_times": []}
    return {**base, **kw}


def test_memory_below_threshold_is_quiet():
    assert adv.rule_memory(snap(memory={"peak_pct": 79.9})) == []
    assert adv.rule_memory(snap(memory=None)) == []


def test_memory_high_names_next_type():
    [item] = adv.rule_memory(snap(memory={"peak_pct": 80.0, "peak_mb": 1638, "limit_mb": 2048, "peak_at": at(5)}))
    assert item["text_key"] == "memory_high"
    assert item["values"]["next_plan"] == "pro" and item["values"]["price_delta"] == 60.0


def test_oom_is_critical_even_with_low_peak():
    [item] = adv.rule_memory(snap(memory={"peak_pct": 40.0}, oom_times=[at(6)]))
    assert (item["severity"], item["text_key"], item["values"]["oom_count"]) == ("critical", "memory_oom", 1)


def test_memory_on_biggest_type():
    [item] = adv.rule_memory(snap(plan="pro_ultra", memory={"peak_pct": 95.0, "peak_at": at(5)}))
    assert item["text_key"] == "memory_high_max"
    [item] = adv.rule_memory(snap(plan="pro_ultra", memory={"peak_pct": 95.0}, oom_times=[at(5)]))
    assert item["text_key"] == "memory_oom_max"


def test_memory_unknown_plan_has_no_next_type():
    [item] = adv.rule_memory(snap(plan="custom", memory={"peak_pct": 90.0, "peak_at": at(5)}))
    assert item["text_key"] == "memory_high_max"


def test_failed_latest_deploy_is_a_warning():
    last = {"status": "build_failed", "commit": "abc1234", "message": "Bump", "reason": "build_failed", "created_at": at(9)}
    [item] = adv.rule_failed_deploy(snap(last_deploy=last, failed_today=[last]))
    assert (item["severity"], item["text_key"]) == ("warning", "deploy_failed")
    assert adv.rule_failed_deploy(snap(last_deploy={"status": "live"}, failed_today=[])) == []


def test_many_deploys_only_with_a_disk():
    assert adv.rule_many_deploys(snap(deploys_today=9, disk_gb=0)) == []
    assert adv.rule_many_deploys(snap(deploys_today=4, disk_gb=5)) == []
    [item] = adv.rule_many_deploys(snap(deploys_today=5, disk_gb=5, deploy_durations=[60] * 5))
    assert item["values"] == {"count": 5, "disk_gb": 5, "deploy_min": 5}


def test_latency_spike_during_a_deploy():
    pts = [(at(h), 100.0) for h in range(0, 20)] + [(at(12, 2), 3000.0)]
    pts.sort()
    s = snap(latency={"points": pts, "median_ms": 100.0}, deploy_windows=[(at(11, 55), at(12, 0))])
    [item] = adv.rule_spikes(s)
    assert item["text_key"] == "latency_spike_deploy"


def test_latency_spike_with_import_and_deploy():
    pts = sorted([(at(h), 100.0) for h in range(0, 20)] + [(at(12, 2), 3000.0)])
    s = snap(latency={"points": pts}, deploy_windows=[(at(11, 55), at(12, 0))], import_times=[at(12)])
    [item] = adv.rule_spikes(s)
    assert item["text_key"] == "latency_spike_import_deploy"


def test_latency_spike_without_cause():
    pts = sorted([(at(h), 100.0) for h in range(0, 20)] + [(at(12, 2), 3000.0)])
    [item] = adv.rule_spikes(snap(latency={"points": pts}))
    assert (item["text_key"], item["severity"]) == ("latency_spike_unknown", "info")


def test_slow_but_steady_latency_is_not_a_spike():
    pts = [(at(h), 1500.0) for h in range(0, 20)]  # always slow: not a spike
    assert adv.rule_spikes(snap(latency={"points": pts})) == []


def test_slow_spike_needs_enough_lines():
    hours = [[(DAY + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ"), 4 if h == 3 else 0] for h in range(24)]
    assert adv.rule_spikes(snap(slow={"per_hour": hours})) == []


def test_disk_high_and_critical():
    assert adv.rule_disk(snap(disk={"used_pct": 79.0})) == []
    [item] = adv.rule_disk(snap(disk={"used_pct": 85.0, "used_gb": 8.5, "size_gb": 10}))
    assert (item["severity"], item["values"]["suggest_gb"], item["values"]["extra_cost"]) == ("warning", 15, 1.25)
    [item] = adv.rule_disk(snap(disk={"used_pct": 96.0, "used_gb": 9.6, "size_gb": 10}))
    assert item["severity"] == "critical"


def test_suspended_and_idle():
    [item] = adv.rule_suspended_or_idle(snap(suspended=True, suspenders=["user"], monthly_cost=25.0))
    assert item["text_key"] == "suspended" and item["values"]["by"] == "user"
    [item] = adv.rule_suspended_or_idle(snap(requests_24h=0, monthly_cost=25.0))
    assert item["text_key"] == "idle"
    assert adv.rule_suspended_or_idle(snap(requests_24h=None, monthly_cost=25.0)) == []  # unknown is not idle
    assert adv.rule_suspended_or_idle(snap(requests_24h=0, monthly_cost=0.0)) == []  # free costs nothing


def test_sorted_most_severe_first():
    s = snap(suspended=True, memory={"peak_pct": 20.0}, oom_times=[at(1)], disk={"used_pct": 85.0, "size_gb": 4})
    assert [a["severity"] for a in adv.build_advice(s)] == ["critical", "warning", "info"]


def test_render_fills_missing_values_with_a_dash():
    item = adv.item("info", "x", "deploy_failed", status="build_failed", commit=None)
    text = adv.render(TEXTS, item)
    assert "{" not in text and "Commit: - " in text


# ------------------------------------------------------------------ texts

def all_text_keys():
    keys = set()
    for name in ("memory_high", "memory_oom"):
        for suffix in ("", "_import", "_max", "_max_import"):
            keys.add(name + suffix)
    keys.discard("memory_oom_import")
    keys.discard("memory_oom_max_import")
    keys |= {"deploy_failed", "deploy_failed_recovered", "deploy_failed_earlier", "many_deploys_disk",
             "disk_high", "suspended", "suspended_disk", "idle"}
    for kind in ("latency_spike", "slow_spike"):
        keys |= {f"{kind}_{c}" for c in ("import", "deploy", "import_deploy", "unknown")}
    keys |= {f"reason_{r}" for r in ("build_failed", "superseded", "oom", "no_port", "timeout",
                                       "unhealthy", "exit_code", "early_exit", "evicted", "unknown")}
    return keys


def test_every_text_exists_in_english_only():
    """One flat English table: no language codes, every key the rules use, plain ASCII."""
    assert set(TEXTS) == all_text_keys()
    for key, text in TEXTS.items():
        assert isinstance(text, str) and text.strip(), key
        assert text.isascii(), key


def test_advice_texts_are_complete_sentences():
    for key, text in TEXTS.items():
        if key.startswith("reason_"):  # fills "Reason: {reason}." inside a sentence
            assert not text.endswith("."), key
            continue
        assert text[0].isupper() or text.startswith("{"), key
        assert text.endswith("."), key


def test_every_advice_of_the_day_renders():
    for item in adv.build_advice(day_snapshot()):
        text = adv.render(TEXTS, item)
        assert "{" not in text and text != item["text_key"]
        assert set(PLACEHOLDER.findall(TEXTS[item["text_key"]])) <= set(item["values"]), item["text_key"]


# ------------------------------------------------------------------ review fixes (2026-11-16)

def test_oom_text_shows_the_last_oom_not_the_memory_peak():
    s = snap(memory={"peak_pct": 60.0, "peak_at": at(3, 20)}, oom_times=[at(1), at(6)],
             import_times=[at(3, 10)])
    [item] = adv.rule_memory(s)
    assert item["text_key"] == "memory_oom"
    assert item["values"]["time"] == "06:00" and item["values"]["at"] == "2026-11-16T06:00:00Z"
    assert item["values"]["cause"] == "unknown"  # the import ran at 03:10, not at the 06:00 OOM


def test_memory_high_still_uses_the_peak_time():
    [item] = adv.rule_memory(snap(memory={"peak_pct": 90.0, "peak_at": at(3, 20)}))
    assert item["values"]["time"] == "03:20"


def test_next_type_is_shown_by_its_label():
    [item] = adv.rule_memory(snap(plan="pro", memory={"peak_pct": 90.0, "peak_at": at(5)}))
    assert item["values"]["next_plan"] == "2c-8g"  # the guessed id stays a value, not the text
    assert item["values"]["next_label"] == "2 CPU / 8 GB"
    assert "2c-8g" not in adv.render(TEXTS, item)
    assert "move to 2 CPU / 8 GB (8 GB at $135 per month, an additional $50 per month)" in adv.render(TEXTS, item)


def _deploy(n, status, minutes=4):
    created = at(n)
    return {"id": f"dep-{n:020d}", "status": status, "trigger": "new_commit",
            "commit": {"id": "0" * 40, "message": f"Change {n}"},
            "createdAt": created.isoformat(), "finishedAt": (created + timedelta(minutes=minutes)).isoformat()}


def _service(**kw):
    base = {"id": "srv-00000000000000000009", "name": "demo-api", "type": "web_service",
            "suspended": "not_suspended",
            "serviceDetails": {"plan": "starter", "numInstances": 1, "disk": {"sizeGB": 1}}}
    return {**base, **kw}


def test_build_failures_do_not_count_as_disk_restarts():
    deploys = [_deploy(n, "build_failed") for n in (9, 8, 7)] + [_deploy(n, "live") for n in (6, 5, 4, 3)]
    s = build_snapshot(_service(), deploys, [], now=NOW, day_start=DAY)
    assert s["deploys_today"] == 7 and s["restarting_deploys_today"] == 4
    assert adv.rule_many_deploys(s) == []  # 4 restarts: below 5
    deploys.insert(0, _deploy(10, "live"))
    s = build_snapshot(_service(), deploys, [], now=NOW, day_start=DAY)
    [item] = adv.rule_many_deploys(s)
    assert item["values"]["count"] == 5 and item["values"]["deploy_min"] == 20


def test_recovered_only_when_a_later_deploy_went_live():
    failed = _deploy(9, "update_failed")
    # a later deploy is still building: not recovered, still a warning
    s = build_snapshot(_service(), [_deploy(10, "build_in_progress"), failed, _deploy(3, "deactivated")],
                       [], now=NOW, day_start=DAY)
    [item] = adv.rule_failed_deploy(s)
    assert (item["severity"], item["text_key"]) == ("warning", "deploy_failed_earlier")
    # a later deploy was canceled: same
    s = build_snapshot(_service(), [_deploy(10, "canceled"), failed], [], now=NOW, day_start=DAY)
    assert adv.rule_failed_deploy(s)[0]["text_key"] == "deploy_failed_earlier"
    # live after the failure, even with a newer one building now: recovered
    s = build_snapshot(_service(), [_deploy(11, "build_in_progress"), _deploy(10, "live"), failed],
                       [], now=NOW, day_start=DAY)
    [item] = adv.rule_failed_deploy(s)
    assert (item["severity"], item["text_key"]) == ("info", "deploy_failed_recovered")


def _hours(counts):
    return [[(DAY + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ"), n] for h, n in enumerate(counts)]


def test_truncated_slow_search_does_not_invent_spikes():
    # The capped search only reached back to 17:30: older hours read 0 although they were busy.
    counts = [0] * 18 + [10, 9, 11, 10, 0, 0]
    busy = {"per_hour": _hours(counts), "truncated": True, "complete_from": "2026-11-16T17:30:00Z"}
    assert adv.rule_spikes(snap(slow=busy)) == []  # only 18..23 are complete: no spike among them
    # the same counts without truncation do look like spikes (median 0)
    assert adv.rule_spikes(snap(slow={**busy, "truncated": False}))
    # unknown coverage: no advice at all
    assert adv.rule_spikes(snap(slow={**busy, "complete_from": None})) == []


def test_truncated_slow_search_still_finds_a_spike_in_complete_hours():
    counts = [0] * 12 + [1, 1, 1, 1, 1, 1, 30, 1, 1, 1, 1, 1]
    slow = {"per_hour": _hours(counts), "truncated": True, "complete_from": "2026-11-16T11:59:00Z"}
    [item] = adv.rule_spikes(snap(slow=slow))
    assert item["values"]["count"] == 30 and item["values"]["hours"] == 1


def test_too_few_complete_hours_give_no_slow_advice():
    counts = [0] * 20 + [1, 30, 1, 1]
    slow = {"per_hour": _hours(counts), "truncated": True, "complete_from": "2026-11-16T19:10:00Z"}
    assert adv.rule_spikes(snap(slow=slow)) == []


def test_suspended_service_costs_only_its_disk():
    s = build_snapshot(_service(suspended="suspended", suspenders=["user"]), [_deploy(3, "live")], [],
                       now=NOW, day_start=DAY)
    assert s["monthly_cost"] == 0.25 and s["running_cost"] == 7.25
    [item] = adv.rule_suspended_or_idle(s)
    assert item["text_key"] == "suspended_disk"
    assert item["values"]["monthly_cost"] == 0.25 and item["values"]["running_cost"] == 7.25
    text = adv.render(TEXTS, item)
    assert "$0.25 per month at present, $7.25 per month when running" in text


def test_suspended_service_without_a_disk_costs_nothing():
    service = _service(suspended="suspended", suspenders=["user"],
                       serviceDetails={"plan": "starter", "numInstances": 1})
    s = build_snapshot(service, [], [], now=NOW, day_start=DAY)
    assert s["monthly_cost"] == 0.0 and s["running_cost"] == 7.0
    [item] = adv.rule_suspended_or_idle(s)
    assert item["text_key"] == "suspended"
    assert "($7 per month when running)" in adv.render(TEXTS, item)
