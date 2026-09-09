"""Failsafe and heartbeat handling for the EVCC bridge."""
from __future__ import annotations

import logging

from .const import (
    DEFAULT_FAILSAFE_CURRENT_A,
    DEFAULT_FAILSAFE_TIMEOUT_S,
    HEARTBEAT_ALIVE_VALUE,
)
from .modbus import WebastoBridgeClient
from .registers import ALIVE, CURRENT_LIMIT, FAILSAFE_CURRENT, FAILSAFE_TIMEOUT, PHASE_SWITCH

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


# --- Baseline capture + restore -------------------------------------------
# Register 2000 (failsafe current) is persistent user-visible configuration:
# it survives a Modbus disconnect and even a power cycle, and a stale value
# actively drives behaviour (the charger overwrites 5004 with it once Alive
# lapses). So before our first write we capture what is there, and on exit we
# put it back. The baseline answers "before us", never "factory default".

# Keys used in the stored baseline dict.
BASELINE_CURRENT_LIMIT = "current_limit"
BASELINE_FAILSAFE_CURRENT = "failsafe_current"
BASELINE_FAILSAFE_TIMEOUT = "failsafe_timeout"
BASELINE_PHASE_SWITCH = "phase_switch"


async def capture_baseline(client: WebastoBridgeClient) -> dict[str, int | None]:
    """Read the registers we are about to manage, before writing any of them.

    Best-effort per register: a missing register becomes None and is simply
    skipped at restore time. Performs no writes. Runs at most once per
    session (the result is stored), so even a slow register costs nothing
    in steady state.
    """
    baseline: dict[str, int | None] = {}
    for key, reg in (
        (BASELINE_CURRENT_LIMIT, CURRENT_LIMIT),
        (BASELINE_FAILSAFE_CURRENT, FAILSAFE_CURRENT),
        (BASELINE_FAILSAFE_TIMEOUT, FAILSAFE_TIMEOUT),
        (BASELINE_PHASE_SWITCH, PHASE_SWITCH),
    ):
        try:
            baseline[key] = int(await client.read(reg))
        except Exception:  # noqa: BLE001 - any failure means "not capturable"
            baseline[key] = None
    return baseline


async def restore_baseline(
    client: WebastoBridgeClient, baseline: dict[str, int | None]
) -> list[str]:
    """Write captured values back and verify by read-back.

    Returns the keys that could not be restored (write or verify failed), so
    the caller can log them for manual recovery. Never raises.
    """
    failed: list[str] = []
    for key, reg in (
        (BASELINE_CURRENT_LIMIT, CURRENT_LIMIT),
        (BASELINE_FAILSAFE_CURRENT, FAILSAFE_CURRENT),
        (BASELINE_FAILSAFE_TIMEOUT, FAILSAFE_TIMEOUT),
        (BASELINE_PHASE_SWITCH, PHASE_SWITCH),
    ):
        value = baseline.get(key)
        if value is None:
            continue
        try:
            await client.write(reg, int(value))
            if int(await client.read(reg)) != int(value):
                failed.append(key)
        except Exception:  # noqa: BLE001
            failed.append(key)
    return failed
