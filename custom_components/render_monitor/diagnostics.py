"""Diagnostics safe to paste in a public issue: no key, no ids, no names, no URLs, no commit text."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_API_KEY, CONF_OWNER_ID, CONF_OWNER_NAME, CONF_SERVICES

TO_REDACT = {CONF_API_KEY, CONF_OWNER_ID, CONF_OWNER_NAME, CONF_SERVICES}


def _service(index: int, snap: dict[str, Any]) -> dict[str, Any]:
    last = snap.get("last_deploy") or {}
    return {
        "service": f"service_{index}",
        "type": snap.get("type"),
        "plan": snap.get("plan"),
        "region": snap.get("region"),
        "instances": snap.get("instances"),
        "disk_gb": snap.get("disk_gb"),
        "status": snap.get("status"),
        "last_deploy_status": last.get("status"),
        "last_deploy_reason": last.get("reason"),
        "deploys_today": snap.get("deploys_today"),
        "restarts": snap.get("restarts"),
        "memory_peak_pct": (snap.get("memory") or {}).get("peak_pct"),
        "cpu_avg_pct": (snap.get("cpu") or {}).get("avg_pct"),
        "p95_ms": (snap.get("latency") or {}).get("p95_ms"),
        "slow_last_hour": (snap.get("slow") or {}).get("last_hour"),
        "monthly_cost": snap.get("monthly_cost"),
        "advice": [{"severity": a["severity"], "text_key": a["text_key"]} for a in snap.get("advice") or []],
        "errors": snap.get("errors") or {},  # already stripped of ids by the coordinator
    }


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry) -> dict[str, Any]:
    coordinator = getattr(entry, "runtime_data", None)
    data = (coordinator.data if coordinator else None) or {}
    services = list((data.get("services") or {}).values())
    return {
        "entry": {
            "title": "**REDACTED**",
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "last_update_success": coordinator.last_update_success if coordinator else None,
        "metrics_read": data.get("metrics_at") is not None,
        "services": [_service(i, s) for i, s in enumerate(services, 1)],
    }
