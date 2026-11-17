"""Setup, reauth and options flows with the Render client mocked (invented workspace and services)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from custom_components.render_monitor.api import (
    RenderAuthError,
    RenderConnectionError,
    RenderRateLimited,
)

from .conftest import (
    API_KEY,
    API_SERVICE,
    DOMAIN,
    OWNER_ID,
    SITE_SERVICE,
    WORKER_SERVICE,
    unwrap,
)

FLOW = "custom_components.render_monitor.config_flow"
SETUP = "custom_components.render_monitor.async_setup_entry"

OWNERS = unwrap("owners.json", "owner")
SERVICES = unwrap("services.json", "service")
SECOND_OWNER = {"id": "tea-00000000000000000002", "name": "Second Demo", "email": "two@example.com", "type": "team"}


@pytest.fixture
def render(frontend_loaded):
    """RenderClient as the flow sees it; set .owners/.services side effects per test."""
    fake = MagicMock()
    fake.list_owners = AsyncMock(return_value=OWNERS)
    fake.list_services = AsyncMock(return_value=SERVICES)
    with (
        patch(f"{FLOW}.RenderClient", return_value=fake) as cls,
        patch(SETUP, return_value=True),
    ):
        fake.cls = cls
        yield fake


async def start(hass):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})


async def test_one_workspace_all_services(hass, render):
    result = await start(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    assert "dashboard.render.com" in result["description_placeholders"]["api_keys_url"]

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"api_key": f"  {API_KEY} "})
    assert result["step_id"] == "services"
    render.list_services.assert_awaited_with(OWNER_ID)
    # every service is offered and ticked by default
    schema = result["data_schema"].schema
    key = next(k for k in schema if k == "services")
    assert sorted(key.default()) == sorted([API_SERVICE, SITE_SERVICE, WORKER_SERVICE])

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"services": [API_SERVICE, SITE_SERVICE, WORKER_SERVICE]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Render: Demo Workspace"
    assert result["data"] == {"api_key": API_KEY, "owner_id": OWNER_ID, "owner_name": "Demo Workspace"}
    # all ticked is stored as "all", so services added later are watched too
    assert result["options"] == {"services": [], "scan_interval_minutes": 5, "metrics_interval_minutes": 15}
    assert result["result"].unique_id == OWNER_ID
    render.cls.assert_called_with(render.cls.call_args.args[0], API_KEY, max_retries=0)


async def test_two_workspaces_and_a_subset(hass, render):
    render.list_owners.return_value = [*OWNERS, SECOND_OWNER]
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"api_key": API_KEY})
    assert result["step_id"] == "workspace"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"owner_id": SECOND_OWNER["id"]})
    assert result["step_id"] == "services"
    render.list_services.assert_awaited_with(SECOND_OWNER["id"])
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"services": [API_SERVICE]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["owner_id"] == SECOND_OWNER["id"]
    assert result["options"]["services"] == [API_SERVICE]


async def test_no_service_ticked_is_an_error(hass, render):
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"api_key": API_KEY})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"services": []})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_services_selected"}


async def test_empty_workspace_is_created_watching_all(hass, render):
    render.list_services.return_value = []
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"api_key": API_KEY})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"]["services"] == []


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (RenderAuthError("refused"), "invalid_auth"),
        (RenderConnectionError("down"), "cannot_connect"),
        (RenderRateLimited("slow down"), "rate_limited"),
    ],
)
async def test_key_errors(hass, render, exc, error):
    render.list_owners.side_effect = exc
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"api_key": API_KEY})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


async def test_no_workspace_aborts(hass, render):
    render.list_owners.return_value = []
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"api_key": API_KEY})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "no_workspaces"


async def test_services_read_failure_aborts(hass, render):
    render.list_services.side_effect = RenderConnectionError("down")
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"api_key": API_KEY})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "cannot_connect"


async def test_same_workspace_twice_is_refused(hass, render, make_entry):
    make_entry()
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"api_key": API_KEY})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_reauth_saves_new_key(hass, render, make_entry):
    entry = make_entry()
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"api_key": "rnd_NEWTESTKEY"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data["api_key"] == "rnd_NEWTESTKEY"


async def test_reauth_key_from_another_workspace_is_refused(hass, render, make_entry):
    entry = make_entry()
    render.list_owners.return_value = [SECOND_OWNER]
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"api_key": "rnd_OTHER"})
    assert result["errors"] == {"base": "wrong_workspace"}
    assert entry.data["api_key"] == API_KEY


async def test_reauth_refused_key(hass, render, make_entry):
    entry = make_entry()
    render.list_owners.side_effect = RenderAuthError("refused")
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"api_key": "rnd_BAD"})
    assert result["errors"] == {"base": "invalid_auth"}


async def test_options_change_services_and_intervals(hass, render, make_entry):
    entry = make_entry()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"services": [SITE_SERVICE], "scan_interval_minutes": 10, "metrics_interval_minutes": 30},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"services": [SITE_SERVICE], "scan_interval_minutes": 10, "metrics_interval_minutes": 30}


async def test_options_all_ticked_means_all(hass, render, make_entry):
    entry = make_entry(services=[API_SERVICE])
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"services": [API_SERVICE, SITE_SERVICE, WORKER_SERVICE], "scan_interval_minutes": 5,
         "metrics_interval_minutes": 15},
    )
    assert entry.options["services"] == []


async def test_options_nothing_ticked_is_an_error(hass, render, make_entry):
    entry = make_entry()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"services": [], "scan_interval_minutes": 5, "metrics_interval_minutes": 15},
    )
    assert result["errors"] == {"base": "no_services_selected"}


async def test_options_interval_minimums(hass, render, make_entry):
    entry = make_entry()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {"services": [API_SERVICE], "scan_interval_minutes": 0, "metrics_interval_minutes": 15},
        )


async def test_options_abort_when_render_unreachable(hass, render, make_entry):
    entry = make_entry()
    render.list_services.side_effect = RenderConnectionError("down")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "cannot_connect"


async def test_flow_clients_do_not_wait_on_429(hass, render, make_entry):
    """A 429 in a dialog shows rate_limited at once: no 3 x 60 s retries inside the step."""
    result = await start(hass)
    await hass.config_entries.flow.async_configure(result["flow_id"], {"api_key": API_KEY})
    assert render.cls.call_args.kwargs.get("max_retries") == 0
    render.cls.reset_mock()
    entry = make_entry()
    await hass.config_entries.options.async_init(entry.entry_id)
    assert render.cls.call_args.kwargs.get("max_retries") == 0


async def test_scan_interval_minimum_is_two_minutes(hass, render, make_entry):
    entry = make_entry()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {"services": [API_SERVICE], "scan_interval_minutes": 1, "metrics_interval_minutes": 15},
        )
