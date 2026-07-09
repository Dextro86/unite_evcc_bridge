from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector

from .const import (
    CONF_FAILSAFE_CURRENT,
    CONF_FAILSAFE_TIMEOUT,
    CONF_MAX_CURRENT,
    CONF_PHASE_RECOVERY_DWELL,
    CONF_PHASE_RECOVERY_ENABLED,
    CONF_PHASE_RECOVERY_OBSERVE,
    CONF_POLL_INTERVAL,
    CONF_REST_ENABLED,
    CONF_REST_PASSWORD,
    CONF_REST_USERNAME,
    CONF_SCAN_INTERVAL,
    CONF_UNIT_ID,
    DEFAULT_FAILSAFE_CURRENT_A,
    DEFAULT_FAILSAFE_TIMEOUT_S,
    DEFAULT_MAX_CURRENT,
    DEFAULT_PHASE_RECOVERY_DWELL_S,
    DEFAULT_PHASE_RECOVERY_ENABLED,
    DEFAULT_PHASE_RECOVERY_OBSERVE_S,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_REST_ENABLED,
    DEFAULT_REST_USERNAME,
    DEFAULT_UNIT_ID,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)
from .modbus import ModbusError, WebastoBridgeClient
from .registers import CHARGING_STATE
from .rest_client import UniteRestAuthError, UniteRestError, async_build_rest_client


async def _validate_input(hass: HomeAssistant, data: dict[str, Any]) -> None:
    client = WebastoBridgeClient(
        data[CONF_HOST],
        int(data[CONF_PORT]),
        int(data[CONF_UNIT_ID]),
    )
    try:
        async with asyncio.timeout(10):
            await client.read(CHARGING_STATE)
    finally:
        await client.async_close()


def _num(minv: float, maxv: float, step: float = 1, unit: str | None = None) -> selector.NumberSelector:
    """A clean number input box with an optional unit."""
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minv,
            max=maxv,
            step=step,
            unit_of_measurement=unit,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _connection_schema(defaults: dict[str, Any], *, include_name: bool) -> vol.Schema:
    fields: dict[Any, Any] = {}
    if include_name:
        fields[vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "Unite EVCC Bridge"))] = str
    fields.update(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Optional(CONF_PORT, default=int(defaults.get(CONF_PORT, DEFAULT_PORT))): int,
            vol.Optional(CONF_UNIT_ID, default=int(defaults.get(CONF_UNIT_ID, DEFAULT_UNIT_ID))): int,
        }
    )
    return vol.Schema(fields)


class WebastoEvccBridgeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_HOST])
            self._abort_if_unique_id_configured()
            try:
                await _validate_input(self.hass, user_input)
            except (ModbusError, OSError, asyncio.TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=user_input.get(CONF_NAME) or "Unite EVCC Bridge",
                    data=user_input,
                )

        schema = _connection_schema(user_input or {}, include_name=True)
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> config_entries.OptionsFlow:
        return UniteEvccBridgeOptionsFlow(config_entry)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            try:
                await _validate_input(self.hass, data)
            except (ModbusError, OSError, asyncio.TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates=user_input,
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_connection_schema(entry.data, include_name=False),
            errors=errors,
        )


class UniteEvccBridgeOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        self.options: dict[str, Any] = dict(config_entry.options)

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return self.async_show_menu(
            step_id="init",
            menu_options=["connection", "charge", "advanced", "reboot", "save"],
        )

    async def async_step_save(self, user_input: dict[str, Any] | None = None):
        return self.async_create_entry(title="", data=self.options)

    async def async_step_connection(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**self._entry.data, **user_input}
            try:
                await _validate_input(self.hass, data)
            except (ModbusError, OSError, asyncio.TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                self.options.update(user_input)
                return await self.async_step_init()
        schema = _connection_schema(self._suggested_options(), include_name=False)
        return self.async_show_form(
            step_id="connection",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_charge(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self.options.update(user_input)
            return await self.async_step_init()
        schema = vol.Schema(
            {
                vol.Required(CONF_MAX_CURRENT, default=DEFAULT_MAX_CURRENT): _num(6, 32, 1, "A"),
                vol.Required(
                    CONF_PHASE_RECOVERY_ENABLED,
                    default=DEFAULT_PHASE_RECOVERY_ENABLED,
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_PHASE_RECOVERY_OBSERVE,
                    default=DEFAULT_PHASE_RECOVERY_OBSERVE_S,
                ): _num(20, 120, 1, "s"),
                vol.Required(
                    CONF_PHASE_RECOVERY_DWELL,
                    default=DEFAULT_PHASE_RECOVERY_DWELL_S,
                ): _num(60, 300, 1, "s"),
            }
        )
        return self.async_show_form(
            step_id="charge",
            data_schema=self.add_suggested_values_to_schema(schema, self._suggested_options()),
        )

    async def async_step_advanced(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self.options.update(user_input)
            return await self.async_step_init()
        schema = vol.Schema(
            {
                vol.Required(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL): _num(
                    MIN_POLL_INTERVAL,
                    MAX_POLL_INTERVAL,
                    1,
                    "s",
                ),
                vol.Required(CONF_FAILSAFE_CURRENT, default=DEFAULT_FAILSAFE_CURRENT_A): _num(0, 32, 1, "A"),
                vol.Required(CONF_FAILSAFE_TIMEOUT, default=DEFAULT_FAILSAFE_TIMEOUT_S): _num(10, 120, 1, "s"),
            }
        )
        return self.async_show_form(
            step_id="advanced",
            data_schema=self.add_suggested_values_to_schema(schema, self._suggested_options()),
        )

    async def async_step_reboot(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(CONF_REST_ENABLED):
                host = self.options.get(CONF_HOST, self._entry.data.get(CONF_HOST, ""))
                try:
                    client = await async_build_rest_client(
                        async_get_clientsession(self.hass),
                        host,
                        str(user_input.get(CONF_REST_USERNAME, DEFAULT_REST_USERNAME)),
                        str(user_input.get(CONF_REST_PASSWORD, "")),
                    )
                    await client.test_connection()
                except UniteRestAuthError:
                    errors["base"] = "invalid_auth"
                except (UniteRestError, OSError, asyncio.TimeoutError):
                    errors["base"] = "rest_cannot_connect"
                except Exception:  # noqa: BLE001
                    errors["base"] = "unknown"
                else:
                    self.options.update(user_input)
                    return await self.async_step_init()
            else:
                self.options.update(user_input)
                return await self.async_step_init()

        schema = vol.Schema(
            {
                vol.Required(CONF_REST_ENABLED, default=DEFAULT_REST_ENABLED): selector.BooleanSelector(),
                vol.Required(CONF_REST_USERNAME, default=DEFAULT_REST_USERNAME): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                ),
                vol.Required(CONF_REST_PASSWORD, default=""): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(
            step_id="reboot",
            data_schema=self.add_suggested_values_to_schema(schema, self._suggested_options()),
            errors=errors,
        )

    def _suggested_options(self) -> dict[str, Any]:
        return {
            CONF_MAX_CURRENT: int(self._entry.data.get(CONF_MAX_CURRENT, DEFAULT_MAX_CURRENT)),
            CONF_POLL_INTERVAL: int(
                self._entry.data.get(
                    CONF_POLL_INTERVAL,
                    self._entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_POLL_INTERVAL),
                )
            ),
            CONF_FAILSAFE_CURRENT: DEFAULT_FAILSAFE_CURRENT_A,
            CONF_FAILSAFE_TIMEOUT: DEFAULT_FAILSAFE_TIMEOUT_S,
            CONF_PHASE_RECOVERY_ENABLED: DEFAULT_PHASE_RECOVERY_ENABLED,
            CONF_PHASE_RECOVERY_OBSERVE: DEFAULT_PHASE_RECOVERY_OBSERVE_S,
            CONF_PHASE_RECOVERY_DWELL: DEFAULT_PHASE_RECOVERY_DWELL_S,
            CONF_REST_ENABLED: DEFAULT_REST_ENABLED,
            CONF_REST_USERNAME: DEFAULT_REST_USERNAME,
            CONF_REST_PASSWORD: "",
            **self.options,
            CONF_HOST: self.options.get(CONF_HOST, self._entry.data.get(CONF_HOST, "")),
            CONF_PORT: int(self.options.get(CONF_PORT, self._entry.data.get(CONF_PORT, DEFAULT_PORT))),
            CONF_UNIT_ID: int(self.options.get(CONF_UNIT_ID, self._entry.data.get(CONF_UNIT_ID, DEFAULT_UNIT_ID))),
        }
