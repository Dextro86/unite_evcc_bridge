from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from . import control as ctrl
from .const import CONF_REST_PASSWORD, CONF_REST_USERNAME, DOMAIN
from .coordinator import WebastoEvccCoordinator


def _iso(value: Any) -> Any:
    return value.isoformat() if value is not None else None


TO_REDACT = {
    CONF_HOST,
    CONF_REST_USERNAME,
    CONF_REST_PASSWORD,
    "host",
    "last_error",
    "rest_username",
    "rest_password",
    "serial",
    "serial_number",
    "session_rfid",
    "unique_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    coordinator: WebastoEvccCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    diagnostics: dict[str, Any] = {
        "entry": {
            "domain": entry.domain,
            "title": entry.title,
            "data": dict(entry.data),
            "options": dict(entry.options),
            "version": entry.version,
        }
    }

    if coordinator is not None:
        stats = coordinator.client.stats
        data = coordinator.data
        diagnostics["runtime"] = {
            "configured_poll_interval": coordinator.configured_poll_interval,
            "effective_poll_interval": coordinator.effective_poll_interval,
            "phase_recovery_enabled": coordinator.phase_recovery_enabled,
            "recovery_status": coordinator.recovery_status,
            "recovery_remaining_s": coordinator.recovery_remaining_s,
            "recovery_active": coordinator.recovery_active,
            "rest_restarting": coordinator.rest_restarting,
            "requested_phase": coordinator.requested_phase,
            "stats": asdict(stats),
            "snapshot": asdict(data) if data is not None else None,
        }
        diagnostics["last_recovery"] = {
            "at": _iso(coordinator.last_recovery_at),
            "result": coordinator.last_recovery_result,
            "reason": coordinator.last_recovery_reason,
        }
        diagnostics["last_restart"] = {
            "at": _iso(coordinator.last_rest_restart_at),
            "result": coordinator.last_rest_restart_result,
        }

        # Interpreted state (the State Inspector), so a bug report reads on its own.
        mismatch = coordinator.phase_mismatch(data)
        diagnostics["interpreted"] = {
            "charger_state": ctrl.derive_state(
                connection_ok=bool(coordinator.last_update_success and data and data.available),
                restarting=coordinator.rest_restarting,
                faulted=bool(data and data.available and data.faulted),
                vehicle_connected=bool(data and data.available and data.vehicle_connected),
                charging=bool(data and data.available and data.charging_active),
                phase_mismatch=mismatch,
                recovery_active=coordinator.recovery_active,
            ),
            "phase_mismatch": mismatch,
        }

    return async_redact_data(diagnostics, TO_REDACT)
