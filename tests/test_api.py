"""The Render client against a fake aiohttp session (recorded-shape fixtures, invented data)."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp
import pytest

from custom_components.render_monitor import api

from .conftest import API_KEY, API_SERVICE, OWNER_ID, load


class FakeResponse:
    def __init__(self, status: int = 200, body: Any = None, headers: dict | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def json(self, content_type=None):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Has ``get`` and nothing else, so any other HTTP method would raise AttributeError."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url, *, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, FakeResponse) else FakeResponse(body=item)

    def params(self, n: int = -1) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for key, value in self.calls[n]["params"]:
            out.setdefault(key, []).append(value)
        return out


class Sleeps:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def client(session: FakeSession, sleep: Sleeps | None = None) -> api.RenderClient:
    return api.RenderClient(session, API_KEY, sleep=sleep or Sleeps())


def page(key: str, start: int, count: int) -> list[dict[str, Any]]:
    return [{key: {"id": f"srv-{i:020d}", "name": f"demo-{i}"}, "cursor": f"c{i}"} for i in range(start, start + count)]


# ---------------------------------------------------------------- transport


async def test_bearer_header_timeout_and_base_url():
    session = FakeSession(load("owners.json"))
    owners = await client(session).list_owners()
    call = session.calls[0]
    assert call["url"] == "https://api.render.com/v1/owners"
    assert call["headers"]["Authorization"] == f"Bearer {API_KEY}"
    assert call["timeout"] is api.TIMEOUT and api.TIMEOUT.total == 30
    assert owners[0]["id"] == OWNER_ID


async def test_pagination_follows_last_cursor_until_short_page():
    session = FakeSession(page("service", 0, 100), page("service", 100, 100), page("service", 200, 7))
    services = await client(session).list_services(OWNER_ID)
    assert len(services) == 207
    assert session.params(0)["limit"] == ["100"]
    assert "cursor" not in session.params(0)
    assert session.params(1)["cursor"] == ["c99"]
    assert session.params(2)["cursor"] == ["c199"]
    assert session.params(0)["ownerId"] == [OWNER_ID]
    assert session.params(0)["includePreviews"] == ["false"]


async def test_pagination_stops_on_empty_page():
    session = FakeSession(page("service", 0, 100), [])
    assert len(await client(session).list_services()) == 100
    assert len(session.calls) == 2


async def test_pagination_stops_when_cursor_repeats():
    same = page("service", 0, 100)
    session = FakeSession(same, same, same)
    await client(session).list_services()
    assert len(session.calls) == 2


async def test_pagination_has_a_page_cap(monkeypatch):
    monkeypatch.setattr(api, "MAX_PAGES", 3)
    session = FakeSession(*(page("service", i * 100, 100) for i in range(5)))
    assert len(await client(session).list_services()) == 300
    assert len(session.calls) == 3


async def test_max_items_asks_for_a_small_page_and_truncates():
    session = FakeSession(load("deploys.json"))
    deploys = await client(session).list_deploys(API_SERVICE, max_items=2)
    assert session.params()["limit"] == ["2"]
    assert [d["id"] for d in deploys] == ["dep-00000000000000000003", "dep-00000000000000000002"]


async def test_429_waits_for_retry_after_then_succeeds():
    sleeps = Sleeps()
    session = FakeSession(FakeResponse(429, headers={"Retry-After": "7"}), load("owners.json"))
    owners = await client(session, sleeps).list_owners()
    assert len(owners) == 1
    assert len(sleeps.waits) == 1 and 7 <= sleeps.waits[0] <= 7.5


async def test_429_uses_ratelimit_reset_epoch():
    headers = {"Ratelimit-Reset": str(int(time.time()) + 20)}
    wait = api._retry_after(headers, 0)
    assert 18 <= wait <= 21


async def test_429_wait_is_capped():
    assert api._retry_after({"Retry-After": "3600"}, 0) <= api.MAX_WAIT + 0.5


async def test_429_backoff_without_headers():
    assert 1 <= api._retry_after({}, 0) <= 1.5
    assert 4 <= api._retry_after({}, 2) <= 4.5


async def test_429_gives_up_after_retries():
    sleeps = Sleeps()
    session = FakeSession(*(FakeResponse(429) for _ in range(api.MAX_RETRIES + 1)))
    with pytest.raises(api.RenderRateLimited):
        await client(session, sleeps).list_owners()
    assert len(sleeps.waits) == api.MAX_RETRIES
    assert len(session.calls) == api.MAX_RETRIES + 1


@pytest.mark.parametrize("status", [401, 403])
async def test_refused_key_is_auth_error_without_the_key(status):
    session = FakeSession(FakeResponse(status, {"id": "x", "message": "unauthorized"}))
    with pytest.raises(api.RenderAuthError) as err:
        await client(session).list_owners()
    assert API_KEY not in str(err.value)


async def test_server_error_is_api_error():
    session = FakeSession(FakeResponse(503, {"message": "unavailable"}))
    with pytest.raises(api.RenderApiError) as err:
        await client(session).get_service(API_SERVICE)
    assert err.value.status == 503
    assert API_KEY not in str(err.value)


@pytest.mark.parametrize("exc", [aiohttp.ClientConnectionError("down"), asyncio.TimeoutError()])
async def test_network_failure_is_connection_error(exc):
    with pytest.raises(api.RenderConnectionError) as err:
        await client(FakeSession(exc)).list_owners()
    assert API_KEY not in str(err.value)


async def test_bad_json_is_api_error():
    with pytest.raises(api.RenderApiError):
        await client(FakeSession(FakeResponse(200, ValueError("not json")))).list_owners()


async def test_unexpected_shape_is_api_error():
    with pytest.raises(api.RenderApiError):
        await client(FakeSession({"not": "a list"})).list_services()


def test_repr_hides_the_key():
    assert API_KEY not in repr(api.RenderClient(FakeSession(), API_KEY))


async def test_ids_cannot_change_the_path():
    session = FakeSession([])
    await client(session).list_deploys("srv-1/../../owners?x=")
    assert session.calls[0]["url"] == "https://api.render.com/v1/services/srv-1%2F..%2F..%2Fowners%3Fx%3D/deploys"
    with pytest.raises(ValueError):
        await client(session).get_service("")


# ---------------------------------------------------------------- endpoints


async def test_services_fixture_shape():
    services = await client(FakeSession(load("services.json"))).list_services(OWNER_ID)
    assert [s["name"] for s in services] == ["demo-api", "demo-site", "demo-worker"]
    assert services[0]["serviceDetails"]["disk"]["sizeGB"] == 10
    assert services[2]["suspended"] == "suspended"


async def test_deploys_created_after():
    session = FakeSession(load("deploys.json"))
    since = datetime(2026, 11, 16, tzinfo=timezone.utc)
    deploys = await client(session).list_deploys(API_SERVICE, created_after=since)
    assert session.calls[0]["url"].endswith(f"/services/{API_SERVICE}/deploys")
    assert session.params()["createdAfter"] == ["2026-11-16T00:00:00Z"]
    assert deploys[0]["status"] == "build_failed"
    assert deploys[0]["commit"]["message"] == "Speed up the import job"


async def test_events_time_window_and_type():
    session = FakeSession(load("events.json"))
    start = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)
    end = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
    events = await client(session).list_events(API_SERVICE, start=start, end=end, event_type="server_failed")
    params = session.params()
    assert params["startTime"] == ["2026-11-15T10:00:00Z"]
    assert params["endTime"] == ["2026-11-16T10:00:00Z"]
    assert params["type"] == ["server_failed"]
    assert events[0]["details"]["reason"]["oomKilled"]


async def test_instances():
    session = FakeSession(load("instances.json"))
    instances = await client(session).list_instances(API_SERVICE)
    assert session.calls[0]["url"].endswith(f"/services/{API_SERVICE}/instances")
    assert instances[0]["createdAt"] == "2026-11-16T08:04:00Z"


async def test_metric_params():
    session = FakeSession(load("metrics_memory.json"))
    series = await client(session).get_metric(
        "memory", API_SERVICE, start=datetime(2026, 11, 15, 8, tzinfo=timezone.utc),
        resolution_seconds=900, aggregation="MAX",
    )
    assert session.calls[0]["url"] == "https://api.render.com/v1/metrics/memory"
    params = session.params()
    assert params == {
        "resource": [API_SERVICE],
        "startTime": ["2026-11-15T08:00:00Z"],
        "resolutionSeconds": ["900"],
        "aggregationMethod": ["MAX"],
    }
    assert series[0]["values"][1]["value"] == 1782579200


async def test_latency_quantile():
    session = FakeSession([])
    await client(session).get_metric("http-latency", API_SERVICE, quantile=0.95)
    assert session.params()["quantile"] == ["0.95"]


async def test_unknown_metric_or_aggregation_is_refused_before_any_request():
    session = FakeSession()
    with pytest.raises(ValueError):
        await client(session).get_metric("../services", API_SERVICE)
    with pytest.raises(ValueError):
        await client(session).get_metric("cpu", API_SERVICE, aggregation="SUM")
    assert session.calls == []


async def test_logs_filters_and_repeated_keys():
    session = FakeSession(load("logs.json"))
    logs = await client(session).list_logs(
        OWNER_ID, [API_SERVICE, "srv-00000000000000000002"], text=["slow:", "import"], level=["error"],
    )
    params = session.params()
    assert session.calls[0]["url"] == "https://api.render.com/v1/logs"
    assert params["ownerId"] == [OWNER_ID]
    assert params["resource"] == [API_SERVICE, "srv-00000000000000000002"]
    assert params["text"] == ["slow:", "import"]
    assert params["level"] == ["error"]
    assert params["direction"] == ["backward"]
    assert logs[0]["message"].startswith("slow:")


async def test_logs_follow_has_more_with_next_window():
    first = {"hasMore": True, "nextStartTime": "2026-11-16T06:00:00Z", "nextEndTime": "2026-11-16T07:00:00Z",
             "logs": [{"id": "1", "message": "a", "timestamp": "2026-11-16T07:30:00Z", "labels": []}]}
    second = load("logs.json")
    session = FakeSession(first, second)
    logs = await client(session).list_logs(OWNER_ID, [API_SERVICE])
    assert len(logs) == 3
    assert session.params(1)["startTime"] == ["2026-11-16T06:00:00Z"]
    assert session.params(1)["endTime"] == ["2026-11-16T07:00:00Z"]


async def test_logs_page_cap():
    more = {"hasMore": True, "nextStartTime": "2026-11-16T06:00:00Z", "nextEndTime": "2026-11-16T07:00:00Z",
            "logs": [{"id": "1", "message": "a", "timestamp": "2026-11-16T07:30:00Z", "labels": []}]}
    session = FakeSession(more, more, more, more)
    await client(session).list_logs(OWNER_ID, [API_SERVICE], max_pages=2)
    assert len(session.calls) == 2


# ---------------------------------------------------------------- review fixes (2026-11-16)


@pytest.mark.parametrize("exc", [
    aiohttp.ClientPayloadError("Response payload is not completed"),
    aiohttp.TooManyRedirects(None, ()),
    aiohttp.ClientResponseError(None, (), status=502),
])
async def test_other_aiohttp_errors_are_render_errors(exc):
    """A broken body (raised while .json() reads it) is a RenderError, never a bare aiohttp one."""
    with pytest.raises(api.RenderConnectionError) as err:
        await client(FakeSession(FakeResponse(200, exc))).list_owners()
    assert API_KEY not in str(err.value)
    with pytest.raises(api.RenderConnectionError):
        await client(FakeSession(exc)).get_service(API_SERVICE)


@pytest.mark.parametrize("call", [
    lambda c: c.list_deploys(API_SERVICE),
    lambda c: c.list_events(API_SERVICE),
    lambda c: c.list_instances(API_SERVICE),
    lambda c: c.get_metric("memory", API_SERVICE),
    lambda c: c.list_logs(OWNER_ID, [API_SERVICE]),
    lambda c: c.get_service(API_SERVICE),
])
async def test_403_on_a_per_service_read_is_not_an_auth_failure(call):
    with pytest.raises(api.RenderApiError) as err:
        await call(client(FakeSession(FakeResponse(403, {"message": "forbidden"}))))
    assert not isinstance(err.value, api.RenderAuthError)
    assert err.value.status == 403


async def test_403_on_the_lists_and_401_anywhere_are_auth_failures():
    with pytest.raises(api.RenderAuthError):
        await client(FakeSession(FakeResponse(403))).list_services(OWNER_ID)
    with pytest.raises(api.RenderAuthError):
        await client(FakeSession(FakeResponse(401))).get_metric("cpu", API_SERVICE)


async def test_max_retries_zero_gives_up_at_once():
    sleeps = Sleeps()
    session = FakeSession(FakeResponse(429, headers={"Retry-After": "60"}))
    with pytest.raises(api.RenderRateLimited):
        await api.RenderClient(session, API_KEY, sleep=sleeps, max_retries=0).list_owners()
    assert sleeps.waits == [] and len(session.calls) == 1


class Clock:
    """A fake monotonic clock that the fake sleep moves forward."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.waits: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


async def test_request_budget_waits_instead_of_bursting():
    clock = Clock()
    session = FakeSession(*([[]] * 7))
    render = api.RenderClient(session, API_KEY, sleep=clock.sleep, clock=clock, per_minute=5)
    for _ in range(5):
        await render.list_instances(API_SERVICE)
    assert clock.waits == []
    await render.list_instances(API_SERVICE)  # 6th in the same minute waits for the 1st to age out
    assert clock.waits == [60.0] and len(session.calls) == 6
    await render.list_instances(API_SERVICE)  # the first five aged out together: room again
    assert clock.waits == [60.0] and len(session.calls) == 7


async def test_logs_have_their_own_smaller_budget():
    clock = Clock()
    logs = {"logs": [], "hasMore": False}
    session = FakeSession(logs, logs, [], logs)
    render = api.RenderClient(session, API_KEY, sleep=clock.sleep, clock=clock, logs_per_minute=2)
    await render.list_logs(OWNER_ID, [API_SERVICE])
    await render.list_logs(OWNER_ID, [API_SERVICE])
    await render.list_instances(API_SERVICE)  # other paths are not held back by the logs budget
    assert clock.waits == []
    await render.list_logs(OWNER_ID, [API_SERVICE])
    assert clock.waits == [60.0]


async def test_concurrent_callers_cannot_overbook_the_budget():
    clock = Clock()
    session = FakeSession(*([[]] * 4))
    render = api.RenderClient(session, API_KEY, sleep=clock.sleep, clock=clock, per_minute=2)
    await asyncio.gather(*(render.list_instances(API_SERVICE) for _ in range(4)))
    assert len(session.calls) == 4
    assert clock.waits and clock.now >= 1060  # the 3rd and 4th went out a minute after the first two


async def test_api_error_carries_renders_reason():
    session = FakeSession(FakeResponse(400, {"id": "bad", "message": "invalid   resource\tlist " + "x" * 300}))
    with pytest.raises(api.RenderApiError) as err:
        await client(session).list_logs(OWNER_ID, [API_SERVICE])
    assert err.value.status == 400
    assert err.value.message.startswith("invalid resource list x") and len(err.value.message) == 160
    assert str(err.value).startswith("Render API returned HTTP 400 for /logs: invalid resource list")


async def test_api_error_without_a_body_has_no_reason():
    session = FakeSession(FakeResponse(500, "oops"))
    with pytest.raises(api.RenderApiError) as err:
        await client(session).get_metric("cpu", API_SERVICE)
    assert err.value.message is None and str(err.value) == "Render API returned HTTP 500 for /metrics/cpu"
