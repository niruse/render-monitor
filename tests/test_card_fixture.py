"""The card's browser test (tests_card/) runs on a fake hass built from REAL entity states.

This test sets the integration up on the replayed day (same fake Render as test_coordinator), and
compares what the card would see - states, entity registry display entries, devices, and every
advice text rendered by the Python side (English) - with tests_card/card_hass.json.
Run with RENDER_MONITOR_WRITE_CARD_FIXTURE=1 to rewrite that file after an intended change.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.json import JSONEncoder

from custom_components.render_monitor.advice import render

from .conftest import DOMAIN
from .test_coordinator import FakeRender, setup, utc  # noqa: F401  (utc is a fixture)

CARD_FIXTURE = Path(__file__).parent.parent / "tests_card" / "card_hass.json"
TEXTS = Path(__file__).parent.parent / "custom_components" / DOMAIN / "www" / "advice_texts.json"


def _plain(value):
    return json.loads(json.dumps(value, cls=JSONEncoder))


async def build(hass) -> dict:
    texts = json.loads(TEXTS.read_text(encoding="utf-8"))
    ents = sorted(
        (e for e in er.async_get(hass).entities.values() if e.platform == DOMAIN),
        key=lambda e: e.entity_id,
    )
    devices = dr.async_get(hass)
    service_of = {d.id: next(iter(d.identifiers))[1] for d in devices.devices.values()
                  if any(i[0] == DOMAIN for i in d.identifiers)}
    # registry ids are random: give the card stable ones ("device_<service order>")
    stable = {dev_id: f"device_{n}" for n, dev_id in enumerate(sorted(service_of, key=service_of.get), 1)}
    out: dict = {"states": {}, "entities": {}, "devices": {}, "expected_texts": {}}
    for entry in ents:
        state = hass.states.get(entry.entity_id)
        out["states"][entry.entity_id] = {
            "entity_id": entry.entity_id,
            "state": state.state,
            "attributes": _plain(dict(state.attributes)),
        }
        out["entities"][entry.entity_id] = {
            "entity_id": entry.entity_id,
            "device_id": stable[entry.device_id],
            "platform": entry.platform,
            "translation_key": entry.translation_key,
        }
        if entry.translation_key == "advice":
            out["expected_texts"][entry.entity_id] = [render(texts, a) for a in state.attributes["advice"]]
    for dev_id, name in stable.items():
        device = devices.async_get(dev_id)
        out["devices"][name] = {
            "id": name,
            "name": device.name,
            "model": device.model,
            "manufacturer": device.manufacturer,
            "configuration_url": device.configuration_url,
        }
    return out


async def test_card_fixture_matches_the_integration(hass, utc, make_entry, frontend_loaded):  # noqa: F811
    from unittest.mock import patch

    with patch("custom_components.render_monitor.RenderClient", return_value=FakeRender()):
        entry = make_entry()
        await setup(hass, entry)
        built = await build(hass)
        assert await hass.config_entries.async_unload(entry.entry_id)

    # the card fixture covers every kind of service and the advice the card has to show
    keys = {e["translation_key"] for e in built["entities"].values()}
    assert {"status", "memory_peak", "cpu_avg", "latency_p95", "disk_used", "advice", "needs_attention"} <= keys
    assert len(built["devices"]) == 4
    assert sum(len(v) for v in built["expected_texts"].values()) >= 6

    if os.environ.get("RENDER_MONITOR_WRITE_CARD_FIXTURE") == "1":
        CARD_FIXTURE.write_text(json.dumps(built, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    committed = json.loads(CARD_FIXTURE.read_text(encoding="utf-8"))
    assert committed == built, "card fixture is stale: rerun with RENDER_MONITOR_WRITE_CARD_FIXTURE=1"
