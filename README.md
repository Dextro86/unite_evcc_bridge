# Unite EVCC Bridge

[![HACS: Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![GitHub release](https://img.shields.io/github/v/release/Dextro86/unite_evcc_bridge?display_name=tag)](https://github.com/Dextro86/unite_evcc_bridge/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-local%20polling-blue.svg)](https://www.home-assistant.io/)

Minimal Home Assistant custom integration to expose a Webasto Unite / Vestel EVC04 as an evcc Home Assistant charger.

This is intentionally small:

- Modbus TCP only
- evcc-compatible Home Assistant entities
- no solar logic
- no DLB
- no automatic phase switching
- optional adaptive 1P to 3P phase recovery
- optional web UI restart button
- no writes on unplug except normal keepalive

evcc should own charging logic. This bridge only adapts Home Assistant entities to the charger Modbus registers.

Available in **English and Dutch** — Home Assistant picks the user's language.

## Requirements

- A Webasto Unite with **Modbus TCP enabled** (in the charger's web UI).
- The charger's IP address. Default port `502`, Modbus unit id `255`.
- Only **one** Modbus master may talk to the charger at a time — do not point this
  bridge and another Modbus client (evcc's own Modbus charger, or the full Unite EV
  Charger integration) at the same wallbox simultaneously.

## Installation

### HACS

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Dextro86&repository=unite_evcc_bridge&category=integration)

1. HACS → ⋯ → *Custom repositories* → add this repository as an *Integration*
   (or use the button above).
2. Install **Unite EVCC Bridge**, then restart Home Assistant.

### Manual

Copy `custom_components/unite_evcc_bridge` into your Home Assistant
`config/custom_components/` directory and restart.

## Setup

1. *Settings → Devices & Services → Add Integration → Unite EVCC Bridge*.
2. Enter the charger's IP address (port and unit id are pre-filled).

## Configuration (Configure → Settings)

Open the integration options in Home Assistant to configure:

- **Charging**: maximum charge current and optional adaptive 1P to 3P recovery
- **Advanced**: polling interval, failsafe current and failsafe timeout
- **Restart (web UI)**: optional restart button using the charger web UI login
- **Connection**: charger IP address, port and Modbus unit ID

The integration also exposes a diagnostic **Connection** binary sensor with
reconnect counters, Modbus failure counters, timeout counters, heartbeat
failures and response timing.

The primary **Status** sensor interprets the charger state as one of:
`idle`, `connected`, `charging`, `phase_mismatch`, `recovery`, `restarting`,
`disconnected` or `fault`. The diagnostic **Phase mismatch** binary sensor turns
on when register `405` is set to 3P while measured current shows the vehicle is
still effectively charging on L1 only. The diagnostic **Last phase recovery**
sensor stores the timestamp and result of the latest adaptive recovery attempt.

When web UI restart is enabled, Home Assistant tests the web UI login before
saving the option. Different Unite firmware/interfaces expose different web UIs,
so the button **auto-detects** the right one: the modern JSON API over HTTPS (on
port `443` or `4443`, self-signed certificate) or the legacy "webconfig" portal
over HTTP. If the JSON API is present but has no restart endpoint on that firmware,
the button automatically falls back to the webconfig reset, so it works across
Unite variants. It performs a **hard reset** (restart immediately, regardless of
state) on every firmware, which **interrupts an active charging session** — it's a
deliberate manual action. Modbus control is not changed by the button; after the
charger restarts, the normal reconnect handshake claims the Modbus session again.
The restart button has a 300 second cooldown because the charger web UI can stay
offline for several minutes while Modbus is already back. The diagnostic
**Last restart** sensor stores the timestamp and result of the latest web UI
restart request without polling the REST API periodically.

## Phase switching

Whether a **live** 1→3 switch takes effect mid-session is **car-dependent**: some
cars pick up the extra phases immediately, others cache their 1p/3p choice for the
whole session and ignore a live upshift. The optional 1P→3P recovery below is for
those cars.

By default, phase switching is a direct evcc passthrough:

1. `select_option("1")` writes `phase = 1P` to register `405`.
2. `select_option("3")` writes `phase = 3P` to register `405`.
3. The select state reports the requested phase to evcc.

If **Enable 1P to 3P phase recovery** is turned on, the bridge first tries the
same live `405 = 1` switch. It then observes measured L1/L2/L3 current for the
configured observation time. If the car is still effectively charging on one
phase, the bridge temporarily takes over the command path:

1. hold `5004 = 0` for the configured recovery time,
2. restore the latest requested evcc current.

The second `405 = 1` write is intentionally not repeated during recovery. The
phase register was already set during the initial live switch; recovery only
forces a long enough charging pause for the vehicle to renegotiate when current
is restored.

evcc current and enable commands are buffered only during that recovery window.
Cars that switch to measured 3P during observation are not interrupted.

During recovery, evcc-facing entities report the last requested intent:

- `switch.<name>_charging_enabled` keeps reporting evcc's requested enabled state.
- `number.<name>_maximum_current` keeps reporting evcc's requested current.
- `select.<name>_phase_mode` keeps reporting evcc's requested phase.

The temporary hardware stop is exposed separately through diagnostics, including
the hardware current limit and the phase recovery status sensors.

> **Known Unite bug — stuck on 1-phase after a new session.** Separate from the
> car-dependent behaviour above, the Unite firmware itself sometimes gets stuck:
> after one charging session ends and a new one starts, the charger can begin on a
> single phase even though 3-phase is requested (`405 = 3`) and stay locked that
> way. A live phase switch does not clear it — the reliable fix is a
> **restart** of the charger, which you can trigger from the **Restart** button
> (enable *Restart (web UI)* in the options).

## Modbus ownership, failsafe & reconnect

The Vestel firmware expects the master to *own* the connection, and the bridge
follows that contract:

- **Ownership setup** — on every new Modbus connection it writes the failsafe
  timeout + current, the charging current and an **alive** heartbeat. If no evcc
  current intent is known yet, the charging current is initialised to `0 A`, so
  Home Assistant never starts charging by itself.
- **Heartbeat cadence** — the alive register must be refreshed faster than
  `failsafe_timeout / 2`, so the effective poll interval is clamped to
  `min(poll_interval, max(3 s, failsafe_timeout / 2))`, whatever you configure.
- **Phase re-assert on reconnect** — register `405` resets to its default on a
  Modbus TCP disconnect, so once evcc has provided a phase intent the bridge
  re-asserts the requested phase after each reconnect.
- **Failsafe fallback** — if Home Assistant, the network, or this integration
  stops writing the heartbeat, the charger drops to the configured failsafe
  current after the configured failsafe timeout.

## Use in evcc

This integration exposes the charger as an
[evcc Home Assistant charger](https://docs.evcc.io/en/chargers/home-assistant-charger/).
The entity IDs are **fixed and language-independent** (they don't change with your
Home Assistant language), so you can copy this straight into your `evcc.yaml`:

```yaml
chargers:
  - name: unite
    type: template
    template: homeassistant
    uri: http://homeassistant.local:8123     # or http://<HA-IP>:8123
    token: <long-lived-access-token>          # HA -> profile -> Long-lived access tokens
    status: sensor.unite_evcc_bridge_iec61851_status
    enabled: switch.unite_evcc_bridge_charging_enabled
    enable: switch.unite_evcc_bridge_charging_enabled
    setMaxCurrent: number.unite_evcc_bridge_maximum_current
    # optional telemetry:
    power: sensor.unite_evcc_bridge_active_power
    energy: sensor.unite_evcc_bridge_energy_total
    currentL1: sensor.unite_evcc_bridge_current_l1
    currentL2: sensor.unite_evcc_bridge_current_l2
    currentL3: sensor.unite_evcc_bridge_current_l3
    voltageL1: sensor.unite_evcc_bridge_voltage_l1
    voltageL2: sensor.unite_evcc_bridge_voltage_l2
    voltageL3: sensor.unite_evcc_bridge_voltage_l3
    # optional 1p/3p phase switching:
    phaseswitch: select.unite_evcc_bridge_phase_mode
```

The IDs above are what a single charger gets. If you added a **second** charger,
Home Assistant appends a suffix (`..._2`) — check yours under
*Developer Tools → States* (filter `unite_evcc_bridge`).

Full entity reference:

| evcc field | Entity ID |
|---|---|
| `status` | `sensor.unite_evcc_bridge_iec61851_status` |
| `enabled` / `enable` | `switch.unite_evcc_bridge_charging_enabled` |
| `setMaxCurrent` | `number.unite_evcc_bridge_maximum_current` |
| `power` | `sensor.unite_evcc_bridge_active_power` |
| `energy` | `sensor.unite_evcc_bridge_energy_total` |
| `currentL1` / `L2` / `L3` | `sensor.unite_evcc_bridge_current_l1` / `_l2` / `_l3` |
| `voltageL1` / `L2` / `L3` | `sensor.unite_evcc_bridge_voltage_l1` / `_l2` / `_l3` |
| `phaseswitch` | `select.unite_evcc_bridge_phase_mode` |

### RFID tag (session billing / vehicle identification)

The charger reports the RFID tag of the running session (Modbus `1516-1530`,
firmware from Vestel spec v1.9 / 2023 onward) as
`sensor.unite_evcc_bridge_session_rfid`. It is empty when charging freely, and
unavailable on older firmware. The integration only reads it while a vehicle is
connected, so it costs nothing when idle.

evcc uses such a tag through its `identify` field, to match a session to a
vehicle (see [vehicle identification](https://docs.evcc.io/en/reference/configuration/vehicles/)).
**The `homeassistant` charger template has no `identify` field**, so to use this
you have to define a `custom` charger instead of the template above, and add the
tag as an `identify` plugin:

```yaml
    identify:
      source: http
      uri: http://homeassistant.local:8123/api/states/sensor.unite_evcc_bridge_session_rfid
      headers:
        Authorization: Bearer <long-lived-access-token>
      jq: .state
```

Note that a `custom` charger means defining every other field (`status`,
`enabled`, `enable`, `maxcurrent`, …) as plugins too — see evcc's
[custom charger docs](https://docs.evcc.io/en/docs/devices/chargers#custom).
The snippet above is a starting point for the RFID part only; it has not been
validated end-to-end by this project.
