"""Sensors per Render service: status, deploys, restarts, memory, CPU, latency, cost, advice."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .advice import render
from .analysis import HTTP_TYPES, NO_METRICS_TYPES, iso
from .const import DOMAIN, PRICES_DATE, PRICING_URL, STATUS_OPTIONS
from .entity import RenderEntity, add_service_entities

PARALLEL_UPDATES = 0

Snap = dict[str, Any]


def _get(snap: Snap, *path: str) -> Any:
    value: Any = snap
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _deploy_attrs(snap: Snap) -> dict[str, Any]:
    last = snap.get("last_deploy") or {}
    return {
        "status": last.get("status"),
        "trigger": last.get("trigger"),
        "commit": last.get("commit"),
        "message": last.get("message"),
        "created_at": iso(last.get("created_at")),
        "finished_at": iso(last.get("finished_at")),
        "duration_s": last.get("duration_s"),
        "reason": last.get("reason"),
    }


@dataclass(frozen=True, kw_only=True)
class RenderSensorDescription(SensorEntityDescription):
    value: Callable[[Snap], Any]
    attrs: Callable[[Snap], dict[str, Any]] = lambda snap: {}
    applies: Callable[[Snap], bool] = lambda snap: True


def _has_metrics(snap: Snap) -> bool:
    return snap.get("type") not in NO_METRICS_TYPES


def _is_http(snap: Snap) -> bool:
    return snap.get("type") in HTTP_TYPES


SENSORS: tuple[RenderSensorDescription, ...] = (
    RenderSensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=STATUS_OPTIONS,
        value=lambda s: s.get("status"),
        attrs=lambda s: {
            "service_type": s.get("type"),
            "plan": s.get("plan"),
            "region": s.get("region"),
            "instances": s.get("instances"),
            "suspenders": s.get("suspenders"),
            "errors": s.get("errors") or None,
        },
    ),
    RenderSensorDescription(
        key="last_deploy",
        translation_key="last_deploy",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda s: _get(s, "last_deploy", "finished_at") or _get(s, "last_deploy", "created_at"),
        attrs=_deploy_attrs,
    ),
    RenderSensorDescription(
        key="deploys_today",
        translation_key="deploys_today",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=lambda s: s.get("deploys_today"),
        attrs=lambda s: {"failed_today": len(s.get("failed_today") or [])},
    ),
    RenderSensorDescription(
        key="restarts_today",
        translation_key="restarts_today",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=lambda s: _get(s, "restarts", "total"),
        attrs=lambda s: {k: v for k, v in (s.get("restarts") or {}).items() if k != "total"},
        applies=_has_metrics,
    ),
    RenderSensorDescription(
        key="memory_peak",
        translation_key="memory_peak",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda s: _get(s, "memory", "peak_pct"),
        attrs=lambda s: {
            "peak_mb": _get(s, "memory", "peak_mb"),
            "limit_mb": _get(s, "memory", "limit_mb"),
            "peak_at": iso(_get(s, "memory", "peak_at")),
            "series": _get(s, "memory", "series") or [],
        },
        applies=_has_metrics,
    ),
    RenderSensorDescription(
        key="cpu_avg",
        translation_key="cpu_avg",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda s: _get(s, "cpu", "avg_pct"),
        attrs=lambda s: {"series": _get(s, "cpu", "series") or []},
        applies=_has_metrics,
    ),
    RenderSensorDescription(
        key="cpu_peak",
        translation_key="cpu_peak",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda s: _get(s, "cpu", "peak_pct"),
        applies=_has_metrics,
    ),
    RenderSensorDescription(
        key="latency_p95",
        translation_key="latency_p95",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda s: _get(s, "latency", "p95_ms"),
        attrs=lambda s: {
            "peak_ms": _get(s, "latency", "peak_ms"),
            "peak_at": iso(_get(s, "latency", "peak_at")),
            "median_ms": _get(s, "latency", "median_ms"),
            # Render's reason when the workspace plan has no latency metrics (e.g. Hobby)
            "not_in_plan": s.get("latency_not_in_plan"),
        },
        applies=_is_http,
    ),
    RenderSensorDescription(
        key="slow_requests",
        translation_key="slow_requests",
        native_unit_of_measurement="requests/h",
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda s: _get(s, "slow", "last_hour"),
        attrs=lambda s: {
            "total_24h": _get(s, "slow", "total_24h"),
            "truncated": _get(s, "slow", "truncated"),
            "complete_from": _get(s, "slow", "complete_from"),
            "per_hour": _get(s, "slow", "per_hour") or [],
        },
        applies=_is_http,
    ),
    RenderSensorDescription(
        key="disk_used",
        translation_key="disk_used",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda s: _get(s, "disk", "used_pct"),
        attrs=lambda s: {"used_gb": _get(s, "disk", "used_gb"), "size_gb": _get(s, "disk", "size_gb") or s.get("disk_gb")},
        applies=lambda s: bool(s.get("disk_gb")) and _has_metrics(s),
    ),
    RenderSensorDescription(
        key="monthly_cost",
        translation_key="monthly_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="USD",
        suggested_display_precision=2,
        value=lambda s: s.get("monthly_cost"),
        attrs=lambda s: {
            "plan": s.get("plan"),
            "instances": s.get("instances"),
            "disk_gb": s.get("disk_gb"),
            "prices_date": PRICES_DATE,
            "pricing_url": PRICING_URL,
        },
    ),
)

ADVICE = RenderSensorDescription(
    key="advice",
    translation_key="advice",
    value=lambda s: len(s.get("advice") or []),
)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data

    def make(sid: str, snap: Snap) -> list[RenderEntity]:
        out: list[RenderEntity] = [RenderSensor(coordinator, sid, d) for d in SENSORS if d.applies(snap)]
        out.append(RenderAdviceSensor(coordinator, sid, ADVICE))
        return out

    entry.async_on_unload(add_service_entities(coordinator, async_add_entities, make))


class RenderSensor(RenderEntity, SensorEntity):
    entity_description: RenderSensorDescription

    # Home Assistant reads this per class (Entity.__init_subclass__), never per instance:
    # the chart series and the hourly table change every update and stay out of the recorder.
    _unrecorded_attributes = frozenset({"series", "per_hour"})

    @property
    def native_value(self) -> Any:
        snap = self.snap
        return self.entity_description.value(snap) if snap else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        snap = self.snap
        return self.entity_description.attrs(snap) if snap else None


class RenderAdviceSensor(RenderSensor):
    """State: how many advice items. Attributes: the items, and their English texts."""

    _unrecorded_attributes = frozenset({"advice", "texts"})

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        snap = self.snap
        if not snap:
            return None
        advice = snap.get("advice") or []
        texts: dict[str, str] = self.hass.data.get(DOMAIN, {}).get("texts") or {}
        return {
            "top_severity": advice[0]["severity"] if advice else None,
            "advice": advice,
            "texts": [render(texts, a) for a in advice],
        }
