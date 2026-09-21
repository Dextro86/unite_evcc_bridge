from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
from time import monotonic
from typing import Any, TypeVar

from .models import ChargerSnapshot
from .control import RfidProbe
from .registers import (
    ACTIVE_POWER,
    CABLE_STATE,
    CHARGE_POINT_STATE,
    CHARGING_STATE,
    CURRENT_L1,
    CURRENT_L2,
    CURRENT_L3,
    CURRENT_LIMIT,
    ENERGY_TOTAL,
    EQUIPMENT_STATE,
    FAILSAFE_CURRENT,
    FAILSAFE_TIMEOUT,
    PHASE_CAPABILITY,
    SESSION_RFID,
    PHASE_SWITCH,
    Register,
    RegisterType,
    SESSION_DURATION,
    SESSION_ENERGY,
    VOLTAGE_L1,
    VOLTAGE_L2,
    VOLTAGE_L3,
    decode_modbus_string,
)

_LOGGER = logging.getLogger(__name__)
T = TypeVar("T")

TELEMETRY_BASE = CHARGE_POINT_STATE.address
TELEMETRY_COUNT = ENERGY_TOTAL.address + ENERGY_TOTAL.count - TELEMETRY_BASE
SESSION_BASE = SESSION_ENERGY.address
SESSION_COUNT = SESSION_DURATION.address + SESSION_DURATION.count - SESSION_BASE


class ModbusError(Exception):
    """Raised when charger Modbus communication fails."""


@dataclass(slots=True)
class ModbusStats:
    connected: bool = False
    connection_epoch: int = 0
    reconnects: int = 0
    read_failures: int = 0
    write_failures: int = 0
    timeouts: int = 0
    alive_failures: int = 0
    last_response_ms: int | None = None
    avg_response_ms: float | None = None
    last_ok: float | None = None
    last_error: str | None = None


def decode_u32(registers: list[int]) -> int:
    return int((registers[0] << 16) | registers[1])


class WebastoBridgeClient:
    def __init__(self, host: str, port: int, unit_id: int) -> None:
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self._client = None
        self._lock = asyncio.Lock()
        self._unit_kwarg: str | None = None
        self._ever_connected = False
        self.stats = ModbusStats()
        self._rfid_probe = RfidProbe()
        self._rfid_probe_epoch = 0

    async def async_close(self) -> None:
        async with self._lock:
            self._disconnect_locked()

    async def read(self, register: Register) -> int | float:
        values = await self._read_registers(register)
        return self._decode(register, values)

    async def write(self, register: Register, value: int) -> None:
        if register.register_type != RegisterType.HOLDING:
            raise ValueError(f"Register {register.name} is not writable")
        await self._call_with_retry(
            lambda: self._write_once(register, value),
            f"write {register.name}",
            is_write=True,
        )

    async def read_snapshot(self) -> ChargerSnapshot:
        try:
            telemetry = await self.read_input_block(TELEMETRY_BASE, TELEMETRY_COUNT)
            session = await self.read_input_block(SESSION_BASE, SESSION_COUNT)

            current_limit = await self._optional_int(CURRENT_LIMIT)
            phase_mode = await self._optional_int(PHASE_SWITCH)
            phase_capability = await self._optional_int(PHASE_CAPABILITY)
            failsafe_current = await self._optional_int(FAILSAFE_CURRENT)
            failsafe_timeout = await self._optional_int(FAILSAFE_TIMEOUT)
            # Only meaningful while a vehicle is connected, and absent on
            # firmware older than spec v1.9 - hence gated and optional. Probed
            # once per connection (see RfidProbe): a failed probe never affects
            # the rest of the snapshot and never drops the connection.
            cable_state = int(self._decode_from_block(CABLE_STATE, telemetry, TELEMETRY_BASE))
            session_rfid = (
                (await self.try_read_optional_string(SESSION_RFID))[0]
                if cable_state >= 2
                else None
            )

            return ChargerSnapshot(
                available=True,
                charge_point_state=int(self._decode_from_block(CHARGE_POINT_STATE, telemetry, TELEMETRY_BASE)),
                charging_state=int(self._decode_from_block(CHARGING_STATE, telemetry, TELEMETRY_BASE)),
                equipment_state=int(self._decode_from_block(EQUIPMENT_STATE, telemetry, TELEMETRY_BASE)),
                cable_state=cable_state,
                current_limit_a=current_limit,
                phase_mode_raw=phase_mode,
                phase_capability_raw=phase_capability,
                active_power_w=float(self._decode_from_block(ACTIVE_POWER, telemetry, TELEMETRY_BASE)),
                energy_total_kwh=float(self._decode_from_block(ENERGY_TOTAL, telemetry, TELEMETRY_BASE)),
                session_energy_kwh=float(self._decode_from_block(SESSION_ENERGY, session, SESSION_BASE)),
                session_duration_s=int(self._decode_from_block(SESSION_DURATION, session, SESSION_BASE)),
                session_rfid=session_rfid,
                current_l1_a=float(self._decode_from_block(CURRENT_L1, telemetry, TELEMETRY_BASE)),
                current_l2_a=float(self._decode_from_block(CURRENT_L2, telemetry, TELEMETRY_BASE)),
                current_l3_a=float(self._decode_from_block(CURRENT_L3, telemetry, TELEMETRY_BASE)),
                voltage_l1_v=float(self._decode_from_block(VOLTAGE_L1, telemetry, TELEMETRY_BASE)),
                voltage_l2_v=float(self._decode_from_block(VOLTAGE_L2, telemetry, TELEMETRY_BASE)),
                voltage_l3_v=float(self._decode_from_block(VOLTAGE_L3, telemetry, TELEMETRY_BASE)),
                failsafe_current_a=failsafe_current,
                failsafe_timeout_s=failsafe_timeout,
            )
        except Exception as err:  # noqa: BLE001
            return ChargerSnapshot(available=False, last_error=str(err))

    async def read_input_block(self, address: int, count: int) -> list[int]:
        return await self._read_block(RegisterType.INPUT, address, count)

    async def read_holding_block(self, address: int, count: int) -> list[int]:
        return await self._read_block(RegisterType.HOLDING, address, count)

    async def _optional_int(self, register: Register) -> int | None:
        try:
            return int(self._decode(register, await self._read_once(register)))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Optional register %s unavailable: %s", register.name, err)
            return None

    async def _read_once(self, register: Register) -> list[int]:
        """Single read attempt: no retries, no sleep, never disconnects.

        Used by optional reads, where a missing register is routine (old
        firmware) rather than an outage. A genuine outage still surfaces
        through the mandatory block reads, which keep their retry path.
        """
        async with self._lock:
            await self._ensure_connected_locked()
            assert self._client is not None
            method = (
                self._client.read_input_registers
                if register.register_type == RegisterType.INPUT
                else self._client.read_holding_registers
            )
            try:
                response = await self._request(
                    method, address=register.address, count=register.count
                )
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                self.stats.read_failures += 1
                if isinstance(err, asyncio.TimeoutError):
                    self.stats.timeouts += 1
                self.stats.last_error = str(err)
                raise ModbusError(f"read {register.name} failed: {err}") from err
            self._raise_for_error(
                response, f"read {register.register_type.value}@{register.address}"
            )
            self.stats.connected = True
            self.stats.last_ok = monotonic()
            self.stats.last_error = None
            return list(response.registers)

    async def _optional_string(self, register: Register) -> str | None:
        """Read an ASCII string register; None when unavailable or empty."""
        try:
            registers = await self._read_registers(register)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Optional register %s unavailable: %s", register.name, err)
            return None
        raw = [int(r) for r in registers]
        return decode_modbus_string(raw) or None

    async def try_read_optional_string(self, register: Register) -> tuple[str | None, bool]:
        """Best-effort string read of an optional register.

        Single attempt, no retries, never disconnects. Returns
        ``(value, transport_error)``: value None with transport_error False
        means a clean refusal (unsupported firmware); True means a timeout or
        connection failure, in which case the probe latch may disable further
        reads so a crashy firmware is left alone.
        """
        if self.stats.connection_epoch != self._rfid_probe_epoch:
            self._rfid_probe_epoch = self.stats.connection_epoch
            self._rfid_probe.reset_on_reconnect()
        if not self._rfid_probe.want_probe:
            return None, False
        async with self._lock:
            try:
                await self._ensure_connected_locked()
                assert self._client is not None
                method = (
                    self._client.read_input_registers
                    if register.register_type == RegisterType.INPUT
                    else self._client.read_holding_registers
                )
                response = await self._request(
                    method, address=register.address, count=register.count
                )
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                self.stats.read_failures += 1
                if isinstance(err, asyncio.TimeoutError):
                    self.stats.timeouts += 1
                self.stats.last_error = str(err)
                if self._rfid_probe.note_transport_error():
                    _LOGGER.warning(
                        "RFID probe keeps losing the connection; RFID reads "
                        "disabled until the integration is reloaded"
                    )
                return None, True
            is_error = getattr(response, "isError", None)
            if response is None or (callable(is_error) and is_error()):
                # Clean protocol refusal: the wallbox is alive, it just does
                # not serve this register.
                self.stats.read_failures += 1
                self.stats.last_error = f"Optional register {register.name} refused"
                self._rfid_probe.note_unsupported()
                _LOGGER.info(
                    "Charger does not serve the RFID registers; "
                    "skipping RFID reads on this connection"
                )
                return None, False
            raw = [int(r) for r in response.registers]
            self._rfid_probe.note_ok()
            self.stats.connected = True
            self.stats.last_ok = monotonic()
            self.stats.last_error = None
            return decode_modbus_string(raw) or None, False

    async def _read_registers(self, register: Register) -> list[int]:
        return await self._read_block(register.register_type, register.address, register.count)

    async def _read_block(self, register_type: RegisterType, address: int, count: int) -> list[int]:
        return await self._call_with_retry(
            lambda: self._read_block_once(register_type, address, count),
            f"read {register_type.value}@{address}",
            is_write=False,
        )

    async def _read_block_once(self, register_type: RegisterType, address: int, count: int) -> list[int]:
        async with self._lock:
            await self._ensure_connected_locked()
            assert self._client is not None
            method = (
                self._client.read_input_registers
                if register_type == RegisterType.INPUT
                else self._client.read_holding_registers
            )
            response = await self._request(method, address=address, count=count)
            self._raise_for_error(response, f"read {register_type.value}@{address}")
            return list(response.registers)

    async def _write_once(self, register: Register, value: int) -> None:
        async with self._lock:
            await self._ensure_connected_locked()
            assert self._client is not None
            response = await self._request(self._client.write_register, address=register.address, value=value)
            self._raise_for_error(response, f"write {register.name}")

    async def _ensure_connected_locked(self) -> None:
        if self._client is not None and getattr(self._client, "connected", False):
            return
        try:
            from pymodbus.client import AsyncModbusTcpClient
        except Exception as err:  # noqa: BLE001
            raise ModbusError(f"pymodbus unavailable: {err}") from err

        client = AsyncModbusTcpClient(self.host, port=self.port, timeout=3, retries=0)
        connected = await client.connect()
        if not connected or not getattr(client, "connected", False):
            self.stats.connected = False
            raise ModbusError(f"Could not connect to {self.host}:{self.port}")

        if self._ever_connected:
            self.stats.reconnects += 1
        self._ever_connected = True
        self._client = client
        self.stats.connected = True
        self.stats.connection_epoch += 1

    def _disconnect_locked(self) -> None:
        client = self._client
        self._client = None
        self.stats.connected = False
        if client is None:
            return
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    async def _request(self, method: Callable[..., Awaitable[Any]], **kwargs: Any) -> Any:
        if self._unit_kwarg is not None:
            return await self._timed_request(method, **kwargs, **{self._unit_kwarg: self.unit_id})
        errors = []
        for unit_kwarg in ("device_id", "slave", "unit"):
            try:
                response = await self._timed_request(method, **kwargs, **{unit_kwarg: self.unit_id})
            except TypeError as err:
                errors.append(err)
                if "unexpected keyword argument" in str(err) and unit_kwarg in str(err):
                    continue
            else:
                self._unit_kwarg = unit_kwarg
                return response
        try:
            return await self._timed_request(method, **kwargs)
        except Exception as err:  # noqa: BLE001
            if errors:
                raise ModbusError(str(errors[-1])) from err
            raise

    async def _timed_request(self, method: Callable[..., Awaitable[Any]], **kwargs: Any) -> Any:
        t0 = monotonic()
        response = await method(**kwargs)
        dt = (monotonic() - t0) * 1000
        self.stats.last_response_ms = round(dt)
        self.stats.avg_response_ms = (
            dt
            if self.stats.avg_response_ms is None
            else (0.8 * self.stats.avg_response_ms) + (0.2 * dt)
        )
        return response

    @staticmethod
    def _raise_for_error(response: Any, description: str) -> None:
        if response is None:
            raise ModbusError(f"Modbus returned no response during {description}")
        is_error = getattr(response, "isError", None)
        if callable(is_error) and is_error():
            raise ModbusError(f"Modbus error during {description}: {response}")

    async def _call_with_retry(
        self,
        func: Callable[[], Awaitable[T]],
        description: str,
        *,
        is_write: bool,
    ) -> T:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                result = await func()
                self.stats.connected = True
                self.stats.last_ok = monotonic()
                self.stats.last_error = None
                return result
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError as err:
                last_error = err
                self.stats.timeouts += 1
                if is_write:
                    self.stats.write_failures += 1
                else:
                    self.stats.read_failures += 1
                self.stats.last_error = str(err)
                _LOGGER.debug("%s timed out, attempt %s/3: %s", description, attempt, err)
                async with self._lock:
                    self._disconnect_locked()
                if attempt < 3:
                    await asyncio.sleep(float(attempt))
            except Exception as err:  # noqa: BLE001
                last_error = err
                if is_write:
                    self.stats.write_failures += 1
                else:
                    self.stats.read_failures += 1
                self.stats.last_error = str(err)
                _LOGGER.debug("%s failed, attempt %s/3: %s", description, attempt, err)
                async with self._lock:
                    self._disconnect_locked()
                if attempt < 3:
                    await asyncio.sleep(float(attempt))
        assert last_error is not None
        raise ModbusError(f"{description} failed: {last_error}") from last_error

    @staticmethod
    def _decode(register: Register, values: list[int]) -> int | float:
        raw = decode_u32(values) if register.count == 2 else int(values[0])
        return raw * register.scale

    def _decode_from_block(self, register: Register, block: list[int], base: int) -> int | float:
        offset = register.address - base
        if offset < 0 or offset + register.count > len(block):
            raise ModbusError(f"Register {register.name} is outside block @{base}")
        return self._decode(register, block[offset : offset + register.count])
