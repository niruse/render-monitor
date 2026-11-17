"""needs_attention: on when a service has warning/critical advice or its latest deploy failed."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import RenderEntity, add_service_entities

PARALLEL_UPDATES = 0

NEEDS_ATTENTION = BinarySensorEntityDescription(
    key="needs_attention",
    translation_key="needs_attention",
    device_class=BinarySensorDeviceClass.PROBLEM,
)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    entry.async_on_unload(add_service_entities(
        coordinator, async_add_entities,
        lambda sid, snap: [RenderNeedsAttention(coordinator, sid, NEEDS_ATTENTION)],
    ))


class RenderNeedsAttention(RenderEntity, BinarySensorEntity):
    @property
    def is_on(self) -> bool | None:
        snap = self.snap
        return bool(snap.get("needs_attention")) if snap else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        snap = self.snap
        if not snap:
            return None
        return {"codes": [a["code"] for a in snap.get("advice") or [] if a["severity"] != "info"]}
