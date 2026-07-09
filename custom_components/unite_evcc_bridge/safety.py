"""Failsafe and heartbeat handling for the EVCC bridge."""
from __future__ import annotations

import logging

from .const import (
    DEFAULT_FAILSAFE_CURRENT_A,
    DEFAULT_FAILSAFE_TIMEOUT_S,
    HEARTBEAT_ALIVE_VALUE,
)
from .modbus import WebastoBridgeClient
from .registers import ALIVE, CURRENT_LIMIT, FAILSAFE_CURRENT, FAILSAFE_TIMEOUT

_LOGGER = logging.getLogger(__name__)


async def write_heartbeat(client: WebastoBridgeClient) -> None:
    """Write the alive register."""
    try:
        await client.write(ALIVE, HEARTBEAT_ALIVE_VALUE)
    except Exception:
        client.stats.alive_failures += 1
        raise


async def program_connection_ownership(
    client: WebastoBridgeClient,
    *,
    failsafe_current_a: int = DEFAULT_FAILSAFE_CURRENT_A,
    failsafe_timeout_s: int = DEFAULT_FAILSAFE_TIMEOUT_S,
    charge_current_a: int,
) -> None:
    """Initialize a new Modbus master connection as expected by the charger."""
    try:
        await client.write(FAILSAFE_TIMEOUT, failsafe_timeout_s)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not write failsafe timeout during Modbus ownership setup: %s", err)

    try:
        await client.write(FAILSAFE_CURRENT, failsafe_current_a)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not write failsafe current during Modbus ownership setup: %s", err)

    await client.write(CURRENT_LIMIT, charge_current_a)
    await write_heartbeat(client)
    _LOGGER.debug(
        "Programmed Modbus ownership: failsafe %sA/%ss, current %sA",
        failsafe_current_a,
        failsafe_timeout_s,
        charge_current_a,
    )
