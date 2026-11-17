"""Setup with a fake Render client: entities, caching of metrics, errors, new services, diagnostics, card."""
from __future__ import annotations

import logging
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.render_monitor.api import RenderApiError, RenderAuthError
from custom_components.render_monitor.diagnostics import async_get_config_entry_diagnostics

from .conftest import API_KEY, API_SERVICE, DOMAIN, OWNER_ID, SITE_SERVICE, WORKER_SERVICE, load, unwrap

SHOP = "srv-00000000000000000004"
NOW = "2026-11-16T20:00:00+00:00"


class FakeRender:
    """Answers like RenderClient from the fixtures; every method is an AsyncMock (call counts)."""

    def __init__(self) -> None:
        day = load("day_import.json")
        self.day = day
        self.services = unwrap("services.json", "service") + [day["service"]["service"]]
        deploys = {API_SERVICE: unwrap("deploys.json", "deploy"), SHOP: [r["deploy"] for r in day["deploys"]]}
        events = {API_SERVICE: unwrap("events.json", "event"), SHOP: [r["event"] for r in day["events"]]}
        self.list_services = AsyncMock(side_effect=lambda owner, **kw: list(self.services))
        self.list_deploys = AsyncMock(side_effect=lambda sid, **kw: deploys.get(sid, []))
        self.list_events = AsyncMock(side_effect=lambda sid, **kw: events.get(sid, []))
        self.list_instances = AsyncMock(side_effect=lambda sid: day["instances"] if sid == SHOP else load("instances.json"))
        self.get_metric = AsyncMock(side_effect=lambda name, sid, **kw: day["metrics"].get(name, []) if sid == SHOP else [])
        self.list_logs = AsyncMock(side_effect=self._logs)

    async def _logs(self, owner, resources, *, text=None, **kw):
        assert owner == OWNER_ID
        return self.day["logs_slow" if text == ["slow:"] else "logs_import"]["logs"]


@pytest.fixture
async def utc(hass, freezer):
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(NOW)
    return freezer


@pytest.fixture
def render():
    fake = FakeRender()
    with patch("custom_components.render_monitor.RenderClient", return_value=fake):
        yield fake


async def setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


def entity(hass, sid, key, platform="sensor"):
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{sid}_{key}")
    assert entity_id, f"no {platform} {key} for {sid}"
    return hass.states.get(entity_id)


async def test_entities_from_one_cycle(hass, utc, render, make_entry, frontend_loaded):
    entry = make_entry()
    await setup(hass, entry)

    shop = lambda key, p="sensor": entity(hass, SHOP, key, p)  # noqa: E731
    assert shop("status").state == "live"
    assert shop("last_deploy").state == "2026-11-16T18:54:00+00:00"
    assert shop("last_deploy").attributes["message"] == "Small change 1"
    assert shop("deploys_today").state == "15"
    assert shop("restarts_today").state == "14"
    assert float(shop("memory_peak").state) == pytest.approx(85.9, abs=0.1)
    assert shop("memory_peak").attributes["peak_mb"] == 440
    assert len(shop("memory_peak").attributes["series"]) == 48
    assert float(shop("cpu_avg").state) > 10 and float(shop("cpu_peak").state) == 90.0
    assert float(shop("latency_p95").state) == 120.0
    assert shop("latency_p95").attributes["peak_ms"] == 2400.0
    assert shop("slow_requests").state == "0"
    assert shop("slow_requests").attributes["total_24h"] == 13
    assert float(shop("disk_used").state) == 30.0
    assert float(shop("monthly_cost").state) == 7.25
    advice = shop("advice")
    assert advice.state == "5"
    assert advice.attributes["top_severity"] == "warning"
    assert [a["text_key"] for a in advice.attributes["advice"]][0] == "memory_high_import"
    assert advice.attributes["texts"][0].startswith("Memory usage reached 86% of the 512 MB limit at 03:20")
    assert shop("needs_attention", "binary_sensor").state == "on"
    assert shop("needs_attention", "binary_sensor").attributes["codes"] == [
        "memory", "many_deploys", "latency_spike", "slow_spike"]

    # demo-api: latest deploy failed in the build; OOM event today
    assert entity(hass, API_SERVICE, "status").state == "deploy_failed"
    assert entity(hass, API_SERVICE, "advice").attributes["advice"][0]["text_key"] == "memory_oom"
    # static site: no metric sensors, costs nothing, nothing to advise
    assert entity(hass, SITE_SERVICE, "monthly_cost").state == "0.0"
    assert entity(hass, SITE_SERVICE, "advice").state == "0"
    assert entity(hass, SITE_SERVICE, "needs_attention", "binary_sensor").state == "off"
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{SITE_SERVICE}_memory_peak") is None
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{WORKER_SERVICE}_latency_p95") is None
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{WORKER_SERVICE}_memory_peak")

    device = dr.async_get(hass).async_get_device({(DOMAIN, SHOP)})
    assert device.name == "demo-shop" and device.manufacturer == "Render"
    assert device.configuration_url == f"https://dashboard.render.com/web/{SHOP}"

    # logs: two searches for all web services of one region together, not one per service
    assert render.list_logs.await_count == 2
    resources = render.list_logs.await_args_list[0].args[1]
    assert sorted(resources) == [API_SERVICE, SHOP]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_metrics_are_cached_between_fast_cycles(hass, utc, render, make_entry, frontend_loaded):
    entry = make_entry()
    coordinator = await setup(hass, entry)
    metric_calls, log_calls = render.get_metric.await_count, render.list_logs.await_count
    assert render.list_services.await_count == 1

    utc.tick(timedelta(minutes=5))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert render.list_services.await_count == 2
    assert render.get_metric.await_count == metric_calls  # 5 min < 15 min: cached
    assert render.list_logs.await_count == log_calls
    assert entity(hass, SHOP, "memory_peak").attributes["peak_mb"] == 440

    utc.tick(timedelta(minutes=10))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert render.get_metric.await_count == 2 * metric_calls
    assert render.list_logs.await_count == 2 * log_calls
    assert coordinator.update_interval == timedelta(minutes=5)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_only_selected_services(hass, utc, render, make_entry, frontend_loaded):
    entry = make_entry(services=[SITE_SERVICE])
    coordinator = await setup(hass, entry)
    assert list(coordinator.data["services"]) == [SITE_SERVICE]
    assert render.get_metric.await_count == 0 and render.list_logs.await_count == 0
    assert render.list_instances.await_count == 0  # static site: no instances
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_new_service_gets_entities_without_reload(hass, utc, render, make_entry, frontend_loaded):
    extra = dict(render.services[2], id="srv-00000000000000000009", name="demo-new")
    entry = make_entry()
    await setup(hass, entry)
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, "srv-00000000000000000009_status") is None

    render.services.append(extra)
    utc.tick(timedelta(minutes=5))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert entity(hass, "srv-00000000000000000009", "status").state == "suspended"  # a copy of demo-worker
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_removed_service_becomes_unavailable(hass, utc, render, make_entry, frontend_loaded):
    entry = make_entry()
    await setup(hass, entry)
    render.services = [s for s in render.services if s["id"] != SITE_SERVICE]
    utc.tick(timedelta(minutes=5))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert entity(hass, SITE_SERVICE, "status").state == "unavailable"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_one_failed_call_is_shown_on_its_service(hass, utc, render, make_entry, frontend_loaded):
    render.list_events.side_effect = RenderApiError(503, f"/services/{API_SERVICE}/events")
    entry = make_entry()
    await setup(hass, entry)
    status = entity(hass, API_SERVICE, "status")
    assert status.state == "deploy_failed"  # deploys still read
    error = status.attributes["errors"]["events"]
    assert "503" in error and API_SERVICE not in error and "srv-" in error
    assert entity(hass, SHOP, "deploys_today").state == "15"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_failed_read_keeps_the_last_answer(hass, utc, render, make_entry, frontend_loaded):
    entry = make_entry()
    await setup(hass, entry)
    render.list_deploys.side_effect = RenderApiError(502, "/services/x/deploys")
    utc.tick(timedelta(minutes=5))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert entity(hass, SHOP, "deploys_today").state == "15"
    assert "deploys" in entity(hass, SHOP, "status").attributes["errors"]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_services_list_failure_retries_setup(hass, utc, render, make_entry, frontend_loaded):
    render.list_services.side_effect = RenderApiError(500, "/services")
    entry = make_entry()
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_refused_key_starts_reauth(hass, utc, render, make_entry, frontend_loaded):
    render.list_services.side_effect = RenderAuthError("Render rejected the API key (HTTP 401)")
    entry = make_entry()
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_refused_key_in_a_metric_call_also_starts_reauth(hass, utc, render, make_entry, frontend_loaded):
    render.get_metric.side_effect = RenderAuthError("Render rejected the API key (HTTP 401)")
    entry = make_entry()
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_diagnostics_hide_key_ids_names_and_commits(hass, utc, render, make_entry, frontend_loaded):
    entry = make_entry()
    await setup(hass, entry)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = repr(diag)
    for secret in (API_KEY, OWNER_ID, "Demo Workspace", "srv-0000", "demo-", "example.com",
                   "Read the import", "Small change", "dashboard.render.com"):
        assert secret not in text, secret
    assert diag["entry"]["data"]["api_key"] == "**REDACTED**"
    assert len(diag["services"]) == 4
    assert {s["service"] for s in diag["services"]} == {"service_1", "service_2", "service_3", "service_4"}
    assert any(s["memory_peak_pct"] for s in diag["services"])
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_api_key_never_logged(hass, utc, render, make_entry, frontend_loaded):
    records: list[logging.LogRecord] = []
    handler = logging.Handler(logging.DEBUG)
    handler.emit = records.append
    root = logging.getLogger("custom_components.render_monitor")
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        render.list_events.side_effect = RenderApiError(503, "/services/x/events")
        entry = make_entry()
        await setup(hass, entry)
        assert await hass.config_entries.async_unload(entry.entry_id)
    finally:
        root.removeHandler(handler)
    assert records, "expected debug lines"
    assert all(API_KEY not in r.getMessage() for r in records)


async def test_card_and_texts_are_served(hass):
    """async_setup serves www/ and adds the card when it exists (cache-busted by its mtime)."""
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    hass.config.components.add("frontend")
    hass.config.components.add("http")
    with (
        patch("custom_components.render_monitor._card_version", return_value=1234),
        patch("homeassistant.components.frontend.add_extra_js_url") as add_js,
    ):
        assert await async_setup_component(hass, DOMAIN, {})
    [configs] = hass.http.async_register_static_paths.await_args.args
    assert configs[0].url_path == "/render_monitor_static"
    assert configs[0].path.endswith("www")
    add_js.assert_called_once_with(hass, "/render_monitor_static/render-monitor-card.js?v=1234")
    texts = hass.data[DOMAIN]["texts"]
    assert "memory_high" in texts and all(isinstance(v, str) for v in texts.values())  # one English table


async def test_no_card_file_no_js_url(hass):
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    hass.config.components.add("frontend")
    hass.config.components.add("http")
    with (
        patch("custom_components.render_monitor._card_version", return_value=None),
        patch("homeassistant.components.frontend.add_extra_js_url") as add_js,
    ):
        assert await async_setup_component(hass, DOMAIN, {})
    add_js.assert_not_called()


async def test_big_attributes_stay_out_of_the_recorder(hass, utc, render, make_entry, frontend_loaded):
    """HA takes unrecorded attributes per class; check what the state machine actually got."""
    await setup(hass, make_entry())
    expect = {
        "memory_peak": {"series"},
        "cpu_avg": {"series"},
        "slow_requests": {"per_hour"},
        "advice": {"advice", "texts"},
    }
    for key, names in expect.items():
        state = entity(hass, SHOP, key)
        assert names <= set(state.attributes), key  # the attributes are there for the card
        assert names <= set(state.state_info["unrecorded_attributes"]), key  # but not recorded
    # small attributes are still recorded
    assert "peak_mb" not in entity(hass, SHOP, "memory_peak").state_info["unrecorded_attributes"]


async def test_403_on_one_metric_is_shown_not_reauth(hass, utc, render, make_entry, frontend_loaded):
    """A read the key may not do (403 on a metric) stays on that service; the entry keeps working."""
    render.get_metric.side_effect = RenderApiError(403, "/metrics/memory")
    entry = make_entry()
    await setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert not hass.config_entries.flow.async_progress()
    assert "403" in entity(hass, SHOP, "status").attributes["errors"]["metrics"]
    assert entity(hass, SHOP, "deploys_today").state == "15"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_capped_slow_search_marks_where_counts_are_complete(hass, utc, render, make_entry, frontend_loaded):
    with patch("custom_components.render_monitor.coordinator.LOG_LINES", 13):  # the day has 13 slow lines
        entry = make_entry()
        await setup(hass, entry)
    slow = entity(hass, SHOP, "slow_requests").attributes
    oldest = min(row["timestamp"] for row in render.day["logs_slow"]["logs"])
    assert slow["truncated"] is True
    assert slow["complete_from"] == oldest.replace(".000000", "")[:19] + "Z"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_log_searches_are_split_by_region(hass, utc, render, make_entry, frontend_loaded):
    """Render refuses a log search over resources of different regions (HTTP 400)."""
    shop = next(s for s in render.services if s["id"] == SHOP)
    shop["serviceDetails"]["region"] = "virginia"
    plain = render.list_logs.side_effect

    async def only_the_shop(owner, resources, **kw):  # the day's lines all belong to the shop
        return await plain(owner, resources, **kw) if SHOP in resources else []

    render.list_logs.side_effect = only_the_shop
    entry = make_entry()
    await setup(hass, entry)

    searches = [(c.args[1], c.kwargs["text"]) for c in render.list_logs.await_args_list]
    assert len(searches) == 4
    assert sorted(tuple(ids) for ids, _ in searches) == [(API_SERVICE,), (API_SERVICE,), (SHOP,), (SHOP,)]
    # the shop's lines still count: same totals as with one search
    assert entity(hass, SHOP, "slow_requests").attributes["total_24h"] == 13
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_one_region_failing_keeps_the_last_answer(hass, utc, render, make_entry, frontend_loaded):
    shop = next(s for s in render.services if s["id"] == SHOP)
    shop["serviceDetails"]["region"] = "virginia"

    async def logs(owner, resources, *, text=None, **kw):
        if resources == [API_SERVICE]:
            raise RenderApiError(400, "/logs", "bad request")
        return render.day["logs_slow" if text == ["slow:"] else "logs_import"]["logs"]

    render.list_logs.side_effect = logs
    entry = make_entry()
    await setup(hass, entry)
    status = entity(hass, SHOP, "status")
    assert "slow logs (frankfurt): Render API returned HTTP 400 for /logs: bad request" in str(status.attributes["errors"])
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_latency_not_in_plan_is_not_an_error(hass, utc, render, make_entry, frontend_loaded):
    """A Hobby workspace gets a 400 for latency: say so on the sensor, ask again only a day later."""
    plain = render.get_metric.side_effect

    async def metric(name, sid, **kw):
        if name == "http-latency":
            raise RenderApiError(400, "/metrics/http-latency", "query is not allowed for plan: Hobby")
        return plain(name, sid, **kw)

    render.get_metric.side_effect = metric
    entry = make_entry()
    coordinator = await setup(hass, entry)
    p95 = entity(hass, SHOP, "latency_p95")
    assert p95.state == "unknown"
    assert p95.attributes["not_in_plan"] == "query is not allowed for plan: Hobby"
    assert "errors" not in entity(hass, SHOP, "status").attributes or not entity(hass, SHOP, "status").attributes["errors"]
    asked = sum(c.args[0] == "http-latency" for c in render.get_metric.await_args_list)
    assert asked >= 1  # the first refusal stops the other services asking too
    assert entity(hass, API_SERVICE, "latency_p95").attributes["not_in_plan"]

    utc.tick(timedelta(hours=1))
    coordinator.metrics_at = None
    await coordinator.async_refresh()
    assert sum(c.args[0] == "http-latency" for c in render.get_metric.await_args_list) == asked
    assert entity(hass, SHOP, "latency_p95").attributes["not_in_plan"]

    utc.tick(timedelta(hours=24))
    coordinator.metrics_at = None
    render.get_metric.side_effect = plain  # the plan was upgraded
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert sum(c.args[0] == "http-latency" for c in render.get_metric.await_args_list) > asked
    assert entity(hass, SHOP, "latency_p95").attributes["not_in_plan"] is None
    assert await hass.config_entries.async_unload(entry.entry_id)
