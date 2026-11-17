"""Render Monitor: read-only summary and advice for Render.com services."""
from __future__ import annotations

import json
import logging
import os

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import RenderClient
from .const import CONF_API_KEY, DOMAIN
from .coordinator import RenderCoordinator

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

WWW = os.path.join(os.path.dirname(__file__), "www")
STATIC_URL = "/render_monitor_static"
CARD_FILE = "render-monitor-card.js"
TEXTS_FILE = "advice_texts.json"

RenderConfigEntry = ConfigEntry[RenderCoordinator]


def _read_texts() -> dict[str, str]:
    with open(os.path.join(WWW, TEXTS_FILE), encoding="utf-8") as handle:
        return json.load(handle)


def _card_version() -> int | None:
    path = os.path.join(WWW, CARD_FILE)
    return int(os.path.getmtime(path)) if os.path.isfile(path) else None


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Advice texts, and the bundled dashboard card (served from www/, cache-busted by mtime)."""
    hass.data.setdefault(DOMAIN, {})["texts"] = await hass.async_add_executor_job(_read_texts)
    if hass.http is None:  # no web server (tests, safe mode): nothing to serve
        return True
    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.components.http import StaticPathConfig

    await hass.http.async_register_static_paths([StaticPathConfig(STATIC_URL, WWW, False)])
    version = await hass.async_add_executor_job(_card_version)
    if version is not None:
        add_extra_js_url(hass, f"{STATIC_URL}/{CARD_FILE}?v={version}")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: RenderConfigEntry) -> bool:
    client = RenderClient(async_get_clientsession(hass), entry.data[CONF_API_KEY])
    coordinator = RenderCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RenderConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: RenderConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
