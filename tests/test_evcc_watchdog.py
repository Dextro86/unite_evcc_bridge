"""Tests for the opt-in evcc watchdog: cap at the failsafe current while evcc
itself is silent, keep evcc's intent, restore it when evcc is back."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import custom_components.unite_evcc_bridge.coordinator as coordinator_module
from custom_components.unite_evcc_bridge.coordinator import WebastoEvccCoordinator
from custom_components.unite_evcc_bridge.evcc_watchdog import (
    EvccApiError,
    EvccLiveness,
    EvccWatchdog,
    TRANSITION_LOST,
    TRANSITION_RECOVERED,
    async_probe_evcc_api,
    normalize_evcc_url,
    probe_interval_s,
)
from custom_components.unite_evcc_bridge.models import ChargerSnapshot


class FakeClient:
    def __init__(self) -> None:
        self.writes: list[tuple[str, int]] = []
        self.phase_raw: int | None = None
        self.stats = SimpleNamespace(connected=True, connection_epoch=0)
        self.snapshot: ChargerSnapshot | None = None

    async def write(self, register, value) -> None:
        self.writes.append((register.name, value))

    async def read(self, register):
        return self.phase_raw

    async def read_snapshot(self) -> ChargerSnapshot:
        assert self.snapshot is not None
        return self.snapshot

    async def read_optional_string_once(self, register):
        return None


class FakeHass:
    def async_create_task(self, coro):
        return asyncio.ensure_future(coro)


def _coordinator(client: FakeClient, **overrides) -> WebastoEvccCoordinator:
    hass = FakeHass()
    entry = SimpleNamespace(entry_id="test", options={}, data={"host": "192.0.2.1"})
    kwargs = {
        "poll_interval": 10,
        "max_current": 16,
        "failsafe_current": 6,
        "failsafe_timeout": 30,
        "phase_recovery_enabled": False,
        "phase_recovery_observe": 0,
        "phase_recovery_dwell": 0,
    }
    kwargs.update(overrides)
    return WebastoEvccCoordinator(hass, entry=entry, client=client, **kwargs)


def _watchdog(**overrides) -> tuple[EvccWatchdog, list[float]]:
    now = [1000.0]
    kwargs = {"mode": "api", "timeout_s": 120, "api_url": "http://192.0.2.20:7070"}
    kwargs.update(overrides)
    return EvccWatchdog(FakeHass(), clock=lambda: now[0], **kwargs), now  # type: ignore[arg-type]


def _idle_snapshot() -> ChargerSnapshot:
    return ChargerSnapshot(available=True, cable_state=0, charging_state=0)


# --- pure helpers ------------------------------------------------------------


def test_normalize_evcc_url_adds_scheme_and_strips_slash() -> None:
    assert normalize_evcc_url("192.168.1.20:7070") == "http://192.168.1.20:7070"
    assert normalize_evcc_url("http://192.168.1.20:7070/") == "http://192.168.1.20:7070"
    assert normalize_evcc_url("  192.168.1.20:7070  ") == "http://192.168.1.20:7070"
    assert normalize_evcc_url("https://evcc.local:7070") == "https://evcc.local:7070"
    assert normalize_evcc_url("") == ""


def test_probe_interval_is_a_few_checks_per_window() -> None:
    assert probe_interval_s(120) == 15
    assert probe_interval_s(60) == 15
    assert probe_interval_s(30) == 7
    assert probe_interval_s(900) == 15


def test_liveness_grace_lost_and_recovered() -> None:
    live = EvccLiveness(timeout_s=120, last_seen=1000.0)
    assert live.evaluate(1000.0 + 119.9) is None  # grace while both boot
    assert live.evaluate(1000.0 + 120.0) == TRANSITION_LOST
    assert live.evaluate(1000.0 + 500.0) is None  # no repeat while lost
    live.mark_alive(1000.0 + 500.0)
    assert live.evaluate(1000.0 + 500.0) == TRANSITION_RECOVERED
    assert live.evaluate(1000.0 + 500.0) is None  # no repeat while fresh


def test_liveness_mark_alive_never_moves_backwards() -> None:
    live = EvccLiveness(timeout_s=120, last_seen=1000.0)
    live.mark_alive(900.0)
    assert live.seconds_since_seen(1000.0) == 0.0


def test_status_line_reports_state() -> None:
    watchdog, now = _watchdog()
    assert "off" not in watchdog.status_line()
    assert "seen" in watchdog.status_line()
    now[0] += 500.0
    assert "silent" in watchdog.status_line()


def test_status_line_flags_missing_config() -> None:
    watchdog, _now = _watchdog(api_url="  ")
    assert "No evcc address" in watchdog.status_line()
    mqtt_watchdog, _now2 = _watchdog(mode="mqtt", mqtt_topic="  ")
    assert "No MQTT topic" in mqtt_watchdog.status_line()


def test_unsupported_mode_rejected() -> None:
    try:
        EvccWatchdog(FakeHass(), mode="off", timeout_s=120)
    except ValueError:
        return
    raise AssertionError("off must be rejected (the coordinator never builds it)")


# --- API probe against a local server ----------------------------------------


def _serve(handler):
    from aiohttp import web

    async def main():
        app = web.Application()
        app.router.add_get("/api/state", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        sockets = getattr(site, "_server").sockets  # type: ignore[attr-defined]
        port = sockets[0].getsockname()[1]
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                return await _probe(session, port)
        finally:
            await runner.cleanup()

    async def _probe(session, port):
        try:
            await async_probe_evcc_api(session, f"http://127.0.0.1:{port}")
            return None
        except EvccApiError as err:
            return str(err)

    return asyncio.run(main())


def test_probe_accepts_evcc_json() -> None:
    from aiohttp import web

    async def handler(request):
        return web.json_response({"loadpoints": []})

    assert _serve(handler) is None


def test_probe_rejects_non_json() -> None:
    from aiohttp import web

    async def handler(request):
        return web.Response(text="<html>router login</html>")

    error = _serve(handler)
    assert error is not None and "no JSON" in error


def test_probe_rejects_json_list() -> None:
    from aiohttp import web

    async def handler(request):
        return web.json_response([1, 2, 3])

    assert _serve(handler) is not None


def test_probe_rejects_http_error() -> None:
    from aiohttp import web

    async def handler(request):
        return web.Response(status=404)

    assert "404" in (_serve(handler) or "")


def test_probe_rejects_unreachable_host() -> None:
    import aiohttp

    async def main():
        async with aiohttp.ClientSession() as session:
            try:
                await async_probe_evcc_api(session, "http://127.0.0.1:9")
            except EvccApiError as err:
                return str(err)
            raise AssertionError("unreachable host must raise")

    assert asyncio.run(main())


# --- MQTT signs of life -------------------------------------------------------


def test_mqtt_live_message_counts_retained_does_not() -> None:
    watchdog, now = _watchdog(mode="mqtt", mqtt_topic="evcc/updated")
    watchdog.handle_mqtt_message(SimpleNamespace(retain=True))  # broker replay on subscribe
    # A retained replay must not move last_seen: advance the clock past the
    # timeout and confirm it still counts as stale.
    now[0] += 500.0
    assert watchdog.is_stale()
    watchdog.handle_mqtt_message(SimpleNamespace(retain=False, topic="evcc/updated"))
    assert not watchdog.is_stale()


# --- coordinator capping ------------------------------------------------------


def test_cap_follows_throttle_and_never_exceeds_failsafe() -> None:
    coord = _coordinator(FakeClient())
    assert coord.cap_current(16) == 16  # watchdog off: untouched
    coord._evcc_throttled = True
    assert coord.cap_current(16) == 6
    assert coord.cap_current(0) == 0
    assert coord.cap_current(3) == 3  # below the cap stays as-is


def test_set_current_is_capped_but_intent_is_kept() -> None:
    async def main():
        client = FakeClient()
        coord = _coordinator(client)
        coord._evcc_throttled = True
        await coord.async_set_current(16)
        return client.writes, coord.current_intent

    writes, intent = asyncio.run(main())
    assert ("current_limit", 6) in writes
    assert intent == 16  # the entity keeps showing evcc's wish


def test_lost_transition_caps_and_recovered_restores() -> None:
    async def main():
        client = FakeClient()
        coord = _coordinator(client)
        coord.current_intent = 16
        watchdog, now = _watchdog()
        coord.evcc_watchdog = watchdog
        now[0] += 500.0  # evcc goes quiet
        assert coord._evaluate_evcc_watchdog() == TRANSITION_LOST
        await coord._async_apply_evcc_transition(TRANSITION_LOST)
        assert coord.evcc_throttled
        assert ("current_limit", 6) in client.writes
        assert coord.current_intent == 16
        watchdog.mark_alive()  # evcc answers again
        assert coord._evaluate_evcc_watchdog() == TRANSITION_RECOVERED
        await coord._async_apply_evcc_transition(TRANSITION_RECOVERED)
        assert not coord.evcc_throttled
        return client.writes

    writes = asyncio.run(main())
    assert ("current_limit", 16) in writes  # intent put back, not waited for


def test_evcc_command_counts_as_sign_of_life() -> None:
    async def main():
        client = FakeClient()
        coord = _coordinator(client)
        watchdog, now = _watchdog()
        coord.evcc_watchdog = watchdog
        now[0] += 500.0
        assert watchdog.is_stale()
        await coord.async_set_current(10)  # evcc reached us -> alive
        return watchdog.is_stale()

    assert asyncio.run(main()) is False


def test_full_poll_programs_capped_current_on_reconnect() -> None:
    async def main():
        client = FakeClient()
        coord = _coordinator(client)
        coord.current_intent = 16
        coord._vehicle_was_connected = False
        coord.data = _idle_snapshot()
        watchdog, now = _watchdog()
        coord.evcc_watchdog = watchdog
        now[0] += 500.0  # silent longer than the timeout
        client.stats.connection_epoch = 1  # force the ownership handshake
        client.snapshot = _idle_snapshot()

        async def _noop_heartbeat(_client) -> None:
            return None

        import custom_components.unite_evcc_bridge.coordinator as coordinator_module

        orig_heartbeat = coordinator_module.write_heartbeat
        coordinator_module.write_heartbeat = _noop_heartbeat
        try:
            await coord._async_update_data()
        finally:
            coordinator_module.write_heartbeat = orig_heartbeat
        return [v for (k, v) in client.writes if k == "current_limit"]

    currents = asyncio.run(main())
    assert currents, "ownership must program a current on reconnect"
    assert all(v <= 6 for v in currents), f"every write capped while lost: {currents}"


def test_no_watchdog_no_behaviour_change() -> None:
    async def main():
        client = FakeClient()
        coord = _coordinator(client)
        coord.current_intent = 16
        await coord.async_set_current(16)
        return client.writes

    assert ("current_limit", 16) in asyncio.run(main())
