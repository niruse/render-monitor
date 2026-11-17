"""Config flow: paste a Render API key, pick the workspace, pick the services."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import RenderAuthError, RenderClient, RenderError, RenderRateLimited
from .const import (
    API_KEYS_URL,
    CONF_API_KEY,
    CONF_METRICS_INTERVAL,
    CONF_OWNER_ID,
    CONF_OWNER_NAME,
    CONF_SCAN_INTERVAL,
    CONF_SERVICES,
    DEFAULT_METRICS_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_METRICS_INTERVAL,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

KEY_SCHEMA = vol.Schema({
    vol.Required(CONF_API_KEY): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
})


def _error_key(err: Exception) -> str:
    if isinstance(err, RenderAuthError):
        return "invalid_auth"
    if isinstance(err, RenderRateLimited):
        return "rate_limited"
    return "cannot_connect"


def _service_options(services: list[dict[str, Any]]) -> list[SelectOptionDict]:
    return [
        SelectOptionDict(value=s["id"], label=f"{s.get('name') or s['id']} ({s.get('type', '?')})")
        for s in sorted(services, key=lambda s: (s.get("name") or "").lower())
        if s.get("id")
    ]


def _services_selector(services: list[dict[str, Any]]) -> SelectSelector:
    return SelectSelector(SelectSelectorConfig(
        options=_service_options(services), multiple=True, mode=SelectSelectorMode.LIST,
    ))


def _stored_services(selected: list[str], services: list[dict[str, Any]]) -> list[str]:
    """Every service ticked is stored as [] ("all"), so services added later are picked up too."""
    all_ids = {s["id"] for s in services if s.get("id")}
    return [] if set(selected) >= all_ids else [s for s in selected if s in all_ids]


def _minutes(minimum: int) -> NumberSelector:
    return NumberSelector(NumberSelectorConfig(
        min=minimum, max=1440, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="min",
    ))


def _flow_client(hass, api_key: str) -> RenderClient:
    """A client for a dialog: a 429 shows "rate limited" at once instead of waiting minutes."""
    return RenderClient(async_get_clientsession(hass), api_key, max_retries=0)


class RenderMonitorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Render Monitor."""

    VERSION = 1

    def __init__(self) -> None:
        self._api_key: str | None = None
        self._owners: list[dict[str, Any]] = []
        self._owner: dict[str, Any] | None = None
        self._services: list[dict[str, Any]] = []

    def _client(self, api_key: str) -> RenderClient:
        return _flow_client(self.hass, api_key)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            try:
                owners = await self._client(api_key).list_owners()
            except RenderError as err:
                errors["base"] = _error_key(err)
                _LOGGER.debug("Render key check failed: %s", type(err).__name__)
            else:
                if not owners:
                    return self.async_abort(reason="no_workspaces")
                self._api_key = api_key
                self._owners = owners
                if len(owners) == 1:
                    return await self._async_use_owner(owners[0])
                return await self.async_step_workspace()
        return self.async_show_form(
            step_id="user",
            data_schema=KEY_SCHEMA,
            errors=errors,
            description_placeholders={"api_keys_url": API_KEYS_URL},
        )

    async def async_step_workspace(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            owner = next((o for o in self._owners if o["id"] == user_input[CONF_OWNER_ID]), None)
            if owner is not None:
                return await self._async_use_owner(owner)
        options = [
            SelectOptionDict(value=o["id"], label=o.get("name") or o["id"])
            for o in self._owners if o.get("id")
        ]
        return self.async_show_form(
            step_id="workspace",
            data_schema=vol.Schema({
                vol.Required(CONF_OWNER_ID): SelectSelector(SelectSelectorConfig(
                    options=options, mode=SelectSelectorMode.LIST,
                )),
            }),
        )

    async def _async_use_owner(self, owner: dict[str, Any]) -> ConfigFlowResult:
        await self.async_set_unique_id(owner["id"])
        self._abort_if_unique_id_configured()
        self._owner = owner
        assert self._api_key is not None
        try:
            self._services = await self._client(self._api_key).list_services(owner["id"])
        except RenderError as err:
            return self.async_abort(reason=_error_key(err))
        if not self._services:
            return self._async_create([])
        return await self.async_step_services()

    async def async_step_services(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            selected = user_input.get(CONF_SERVICES) or []
            if selected:
                return self._async_create(_stored_services(selected, self._services))
            errors["base"] = "no_services_selected"
        all_ids = [s["id"] for s in self._services if s.get("id")]
        return self.async_show_form(
            step_id="services",
            data_schema=vol.Schema({
                vol.Required(CONF_SERVICES, default=all_ids): _services_selector(self._services),
            }),
            errors=errors,
            description_placeholders={"workspace": (self._owner or {}).get("name", "")},
        )

    @callback
    def _async_create(self, services: list[str]) -> ConfigFlowResult:
        assert self._owner is not None
        name = self._owner.get("name") or self._owner["id"]
        return self.async_create_entry(
            title=f"Render: {name}",
            data={
                CONF_API_KEY: self._api_key,
                CONF_OWNER_ID: self._owner["id"],
                CONF_OWNER_NAME: name,
            },
            options={
                CONF_SERVICES: services,
                CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                CONF_METRICS_INTERVAL: DEFAULT_METRICS_INTERVAL,
            },
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            try:
                owners = await self._client(api_key).list_owners()
            except RenderError as err:
                errors["base"] = _error_key(err)
            else:
                if any(o.get("id") == entry.data[CONF_OWNER_ID] for o in owners):
                    return self.async_update_reload_and_abort(
                        entry, data_updates={CONF_API_KEY: api_key}
                    )
                errors["base"] = "wrong_workspace"
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=KEY_SCHEMA,
            errors=errors,
            description_placeholders={
                "api_keys_url": API_KEYS_URL,
                "workspace": entry.data.get(CONF_OWNER_NAME, ""),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return RenderMonitorOptionsFlow()


class RenderMonitorOptionsFlow(OptionsFlow):
    """Change the services read and how often."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        client = _flow_client(self.hass, entry.data[CONF_API_KEY])
        try:
            services = await client.list_services(entry.data[CONF_OWNER_ID])
        except RenderError as err:
            return self.async_abort(reason=_error_key(err))

        errors: dict[str, str] = {}
        if user_input is not None:
            selected = user_input.get(CONF_SERVICES) or []
            if selected or not services:
                return self.async_create_entry(data={
                    CONF_SERVICES: _stored_services(selected, services),
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_METRICS_INTERVAL: int(user_input[CONF_METRICS_INTERVAL]),
                })
            errors["base"] = "no_services_selected"

        options = entry.options
        all_ids = [s["id"] for s in services if s.get("id")]
        current = options.get(CONF_SERVICES) or all_ids
        current = [s for s in current if s in all_ids] or all_ids
        schema: dict[Any, Any] = {}
        if services:
            schema[vol.Required(CONF_SERVICES, default=current)] = _services_selector(services)
        schema[vol.Required(
            CONF_SCAN_INTERVAL, default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )] = _minutes(MIN_SCAN_INTERVAL)
        schema[vol.Required(
            CONF_METRICS_INTERVAL, default=options.get(CONF_METRICS_INTERVAL, DEFAULT_METRICS_INTERVAL)
        )] = _minutes(MIN_METRICS_INTERVAL)
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema), errors=errors)
