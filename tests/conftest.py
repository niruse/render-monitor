"""Shared fixtures. Every id, name, domain and key here is invented (demo-*, example.com, srv-0000...)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

DOMAIN = "render_monitor"
FIXTURES = Path(__file__).parent / "fixtures"

API_KEY = "rnd_TESTKEY0000000000000000000000"
OWNER_ID = "tea-00000000000000000001"
API_SERVICE = "srv-00000000000000000001"
SITE_SERVICE = "srv-00000000000000000002"
WORKER_SERVICE = "srv-00000000000000000003"

ENTRY_DATA = {"api_key": API_KEY, "owner_id": OWNER_ID, "owner_name": "Demo Workspace"}
ENTRY_OPTIONS = {"services": [], "scan_interval_minutes": 5, "metrics_interval_minutes": 15}


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def unwrap(name: str, key: str) -> list[dict[str, Any]]:
    """A list endpoint's items without the {key, cursor} wrapper, as the client returns them."""
    return [row[key] for row in load(name)]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def frontend_loaded(hass):
    """The manifest depends on frontend/http (for the card); the card registration is not under test here."""
    hass.config.components.add("frontend")
    hass.config.components.add("http")


@pytest.fixture
def make_entry(hass):
    def _make(**options):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data=dict(ENTRY_DATA),
            options={**ENTRY_OPTIONS, **options},
            unique_id=OWNER_ID,
            title="Render: Demo Workspace",
        )
        entry.add_to_hass(hass)
        return entry
    return _make
