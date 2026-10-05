from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Generic, TypeVar


ROOT = Path(__file__).resolve().parents[1]
CUSTOM_COMPONENTS = ROOT / "custom_components"
INTEGRATION = CUSTOM_COMPONENTS / "unite_evcc_bridge"

custom_components = types.ModuleType("custom_components")
custom_components.__path__ = [str(CUSTOM_COMPONENTS)]
sys.modules.setdefault("custom_components", custom_components)

integration = types.ModuleType("custom_components.unite_evcc_bridge")
integration.__path__ = [str(INTEGRATION)]
sys.modules.setdefault("custom_components.unite_evcc_bridge", integration)


# --- Home Assistant stubs ---------------------------------------------------
# The coordinator imports HA for the update-coordinator base class, storage,
# const and aiohttp helpers. The pure-logic tests never execute HA, so these
# are inert stand-ins just rich enough to import the coordinator module and
# drive its control methods directly.

_T = TypeVar("_T")


class _DataUpdateCoordinator(Generic[_T]):
    def __init__(self, hass, logger, *, name, update_interval=None):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.data = None

    def async_update_listeners(self) -> None:
        pass

    async def async_request_refresh(self) -> None:
        pass


class _Store:
    def __init__(self, hass, version, key):
        self._hass = hass
        self._key = key

    async def async_load(self):
        return None

    async def async_save(self, data) -> None:
        pass


def _utcnow():
    return datetime.now(timezone.utc)


def _async_get_clientsession(hass):
    return None


_homeassistant = types.ModuleType("homeassistant")
_homeassistant.__path__ = []
sys.modules.setdefault("homeassistant", _homeassistant)

_ha_util = types.ModuleType("homeassistant.util")
_ha_util.__path__ = []
sys.modules.setdefault("homeassistant.util", _ha_util)

_ha_util_dt = types.ModuleType("homeassistant.util.dt")
_ha_util_dt.utcnow = _utcnow
_ha_util.dt = _ha_util_dt
sys.modules.setdefault("homeassistant.util.dt", _ha_util_dt)

_ha_helpers = types.ModuleType("homeassistant.helpers")
_ha_helpers.__path__ = []
sys.modules.setdefault("homeassistant.helpers", _ha_helpers)

_ha_update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")
_ha_update_coordinator.DataUpdateCoordinator = _DataUpdateCoordinator
_ha_helpers.update_coordinator = _ha_update_coordinator
sys.modules.setdefault("homeassistant.helpers.update_coordinator", _ha_update_coordinator)

_ha_const = types.ModuleType("homeassistant.const")
_ha_const.CONF_HOST = "host"
sys.modules.setdefault("homeassistant.const", _ha_const)

_ha_aiohttp = types.ModuleType("homeassistant.helpers.aiohttp_client")
_ha_aiohttp.async_get_clientsession = _async_get_clientsession
_ha_helpers.aiohttp_client = _ha_aiohttp
sys.modules.setdefault("homeassistant.helpers.aiohttp_client", _ha_aiohttp)

_ha_storage = types.ModuleType("homeassistant.helpers.storage")
_ha_storage.Store = _Store
_ha_helpers.storage = _ha_storage
sys.modules.setdefault("homeassistant.helpers.storage", _ha_storage)
