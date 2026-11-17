"""Base entity: one device per Render service, values read from the coordinator's summary."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RenderCoordinator


class RenderEntity(CoordinatorEntity[RenderCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: RenderCoordinator, service_id: str, description: EntityDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self.service_id = service_id
        self._attr_unique_id = f"{service_id}_{description.key}"
        snap = self.snap or {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, service_id)},
            name=snap.get("name") or service_id,
            manufacturer="Render",
            model=snap.get("type"),
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=snap.get("dashboard_url"),
        )

    @property
    def snap(self) -> dict[str, Any] | None:
        return ((self.coordinator.data or {}).get("services") or {}).get(self.service_id)

    @property
    def available(self) -> bool:
        return super().available and self.snap is not None


def add_service_entities(
    coordinator: RenderCoordinator,
    async_add_entities: AddEntitiesCallback,
    make: Callable[[str, dict[str, Any]], list[RenderEntity]],
) -> Callable[[], None]:
    """Add entities for every service now, and for services that appear later."""
    known: set[str] = set()

    @callback
    def _add() -> None:
        services = (coordinator.data or {}).get("services") or {}
        new = [sid for sid in services if sid not in known]
        entities: list[RenderEntity] = []
        for sid in new:
            known.add(sid)
            entities += make(sid, services[sid])
        if entities:
            async_add_entities(entities)

    _add()
    return coordinator.async_add_listener(_add)
