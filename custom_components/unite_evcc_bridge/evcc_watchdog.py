"""evcc liveness watchdog.

The charger's own failsafe only covers the Home Assistant <-> charger link:
Home Assistant writes the alive heartbeat on every poll, whether or not evcc
is still around. If evcc crashes or loses its network, the charger would keep
charging at evcc's last setpoint indefinitely.

This module watches an independent sign of life from evcc - its HTTP API or
an MQTT topic it publishes every cycle - and reports when that goes stale, so
the coordinator can cap the charge current at the failsafe current until evcc
is back. evcc's intent is kept untouched: evcc does not re-send an unchanged
setpoint, so on recovery the coordinator re-writes evcc's last intent itself.

HA-free on purpose (only lazy HA imports inside methods), so the state
machine stays unit-testable without Home Assistant.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

import aiohttp

_LOGGER = logging.getLogger(__name__)

WATCHDOG_OFF = "off"
WATCHDOG_API = "api"
WATCHDOG_MQTT = "mqtt"
WATCHDOG_MODES = (WATCHDOG_OFF, WATCHDOG_API, WATCHDOG_MQTT)

TRANSITION_LOST = "lost"
TRANSITION_RECOVERED = "recovered"

# evcc serves its full state as JSON here, without login. (There is no
# /api/health in evcc.) A reply only counts when it is JSON, so an unrelated
# web server on the same address can't pass for evcc.
API_STATE_PATH = "/api/state"
API_REQUEST_TIMEOUT_S = 5


class EvccApiError(Exception):
    """evcc's API could not be reached or did not answer like evcc."""


def normalize_evcc_url(url: str) -> str:
    """'192.168.1.20:7070' -> 'http://192.168.1.20:7070'; bare hosts get http."""
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = f"http://{url}"
    return url


def probe_interval_s(timeout_s: int) -> int:
    """Check a few times per timeout window, but not more often than every 5 s."""
    return max(5, min(15, int(timeout_s) // 4))


def _describe(err: BaseException) -> str:
    """A short, human reason for a failed request."""
    if isinstance(err, asyncio.TimeoutError):
        return "no answer (timeout)"
    if isinstance(err, aiohttp.ClientConnectorError):
        return f"cannot connect ({err.os_error.strerror or err.os_error})"
    if isinstance(err, aiohttp.InvalidURL):
        return "not a valid address"
    text = str(err).strip()
    return f"{type(err).__name__}: {text}" if text else type(err).__name__


async def async_probe_evcc_api(
    session: aiohttp.ClientSession,
    base_url: str,
    *,
    timeout_s: float = API_REQUEST_TIMEOUT_S,
) -> None:
    """Return when evcc answers /api/state with JSON. Raise EvccApiError otherwise.

    The exception message is meant for people: it says what went wrong.
    """
    base = normalize_evcc_url(base_url)
    if not base:
        raise EvccApiError("no address entered")
    url = f"{base}{API_STATE_PATH}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_s)) as resp:
            if resp.status in (401, 403):
                raise EvccApiError(f"HTTP {resp.status}: evcc asks for a login")
            if resp.status != 200:
                raise EvccApiError(f"HTTP {resp.status} from {url}")
            try:
                data = await resp.json(content_type=None)
            except ValueError as err:
                raise EvccApiError(
                    "the address answers, but not with evcc data (no JSON) - "
                    "is this the evcc port?"
                ) from err
            if not isinstance(data, dict):
                raise EvccApiError("the address answers, but not with evcc data")
    except EvccApiError:
        raise
    except Exception as err:  # noqa: BLE001 - any failure means "not alive"
        raise EvccApiError(_describe(err)) from err


@dataclass
class EvccLiveness:
    """Pure stale/fresh bookkeeping on a monotonic clock.

    ``last_seen`` starts at construction time, so a missing evcc is only
    declared lost after one full timeout (a grace period while both boot).
    """

    timeout_s: float
    last_seen: float
    lost: bool = False

    def mark_alive(self, now: float) -> None:
        self.last_seen = max(self.last_seen, now)

    def is_stale(self, now: float) -> bool:
        return now - self.last_seen >= self.timeout_s

    def seconds_since_seen(self, now: float) -> float:
        return max(0.0, now - self.last_seen)

    def evaluate(self, now: float) -> str | None:
        """Return TRANSITION_LOST / TRANSITION_RECOVERED on an edge, else None."""
        stale = self.is_stale(now)
        if stale and not self.lost:
            self.lost = True
            return TRANSITION_LOST
        if not stale and self.lost:
            self.lost = False
            return TRANSITION_RECOVERED
        return None


class EvccWatchdog:
    """Feeds EvccLiveness from evcc's HTTP API or from an MQTT topic."""

    def __init__(
        self,
        hass: Any,
        *,
        mode: str,
        timeout_s: int,
        api_url: str | None = None,
        mqtt_topic: str | None = None,
        session: aiohttp.ClientSession | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if mode not in (WATCHDOG_API, WATCHDOG_MQTT):
            raise ValueError(f"unsupported evcc watchdog mode: {mode}")
        self.hass = hass
        self.mode = mode
        self.timeout_s = int(timeout_s)
        self.api_url = normalize_evcc_url(api_url or "")
        self.mqtt_topic = (mqtt_topic or "").strip()
        self._session = session
        self._clock = clock
        self.liveness = EvccLiveness(timeout_s=self.timeout_s, last_seen=clock())
        self.last_error: str | None = None
        self._unsub_timer: Callable[[], None] | None = None
        self._unsub_mqtt: Callable[[], None] | None = None
        self._stopped = False

    # -- status --------------------------------------------------------------

    @property
    def source(self) -> str:
        return self.api_url if self.mode == WATCHDOG_API else self.mqtt_topic

    def is_stale(self) -> bool:
        return self.liveness.is_stale(self._clock())

    def seconds_since_seen(self) -> float:
        return self.liveness.seconds_since_seen(self._clock())

    def status_line(self) -> str:
        """One line for the settings screen: alive, stale, or misconfigured."""
        if self.mode == WATCHDOG_API and not self.api_url:
            return "No evcc address entered yet."
        if self.mode == WATCHDOG_MQTT and not self.mqtt_topic:
            return "No MQTT topic entered yet."
        seen = int(self.seconds_since_seen())
        state = "silent" if self.is_stale() else "seen"
        line = f"evcc {state} ({seen} s ago, via {self.source})."
        if self.last_error is not None:
            line += f" Last check: {self.last_error}."
        return line

    def evaluate(self) -> str | None:
        return self.liveness.evaluate(self._clock())

    def mark_alive(self) -> None:
        self.liveness.mark_alive(self._clock())

    # -- lifecycle -----------------------------------------------------------

    async def async_start(self) -> None:
        from homeassistant.helpers.event import async_track_time_interval

        self._unsub_timer = async_track_time_interval(
            self.hass,
            self._async_tick,
            timedelta(seconds=probe_interval_s(self.timeout_s)),
        )
        await self._async_tick()

    async def async_stop(self) -> None:
        self._stopped = True
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None
        if self._unsub_mqtt is not None:
            self._unsub_mqtt()
            self._unsub_mqtt = None

    async def _async_tick(self, _now: Any = None) -> None:
        if self._stopped:
            return
        try:
            if self.mode == WATCHDOG_API:
                await self.async_probe_api()
            elif self._unsub_mqtt is None:
                # (Re)try until Home Assistant's MQTT client is up.
                await self._async_subscribe_mqtt()
        except Exception as err:  # noqa: BLE001 - a probe must never kill the timer
            self.last_error = str(err)
            _LOGGER.debug("evcc watchdog tick failed: %s", err)

    # -- API source ----------------------------------------------------------

    async def async_probe_api(self) -> bool:
        if self._session is None:
            from homeassistant.helpers.aiohttp_client import async_get_clientsession

            self._session = async_get_clientsession(self.hass)
        try:
            await async_probe_evcc_api(self._session, self.api_url)
        except EvccApiError as err:
            self.last_error = str(err)
            _LOGGER.debug("evcc API probe failed: %s", err)
            return False
        self.last_error = None
        self.mark_alive()
        return True

    # -- MQTT source ---------------------------------------------------------

    async def _async_subscribe_mqtt(self) -> None:
        from homeassistant.components import mqtt
        from homeassistant.core import callback

        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            self.last_error = "Home Assistant's MQTT integration is not available"
            _LOGGER.debug(self.last_error)
            return

        @callback
        def _on_message(msg: Any) -> None:  # runs in the event loop
            self.handle_mqtt_message(msg)

        self._unsub_mqtt = await mqtt.async_subscribe(self.hass, self.mqtt_topic, _on_message, qos=0)
        self.last_error = None
        _LOGGER.debug("evcc watchdog subscribed to MQTT topic %s", self.mqtt_topic)

    def handle_mqtt_message(self, msg: Any) -> None:
        """Any live message on the topic counts as a sign of life.

        Retained messages are ignored: the broker replays the last value on
        (re)subscribe even when evcc has been dead for hours. Per the MQTT
        spec a message forwarded live to an existing subscriber never carries
        the retain flag.
        """
        if getattr(msg, "retain", False):
            return
        self.mark_alive()
