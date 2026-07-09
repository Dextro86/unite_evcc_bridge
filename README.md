# Unite EVCC Bridge

[![HACS: Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![GitHub release](https://img.shields.io/github/v/release/Dextro86/unite_evcc_bridge?display_name=tag)](https://github.com/Dextro86/unite_evcc_bridge/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-local%20polling-blue.svg)](https://www.home-assistant.io/)

Minimal Home Assistant custom integration to expose a Webasto Unite / Ampure Unite / Vestel EVC04 as an EVCC Home Assistant charger.

This is intentionally small:

- Modbus TCP only
- EVCC-compatible Home Assistant entities
- no solar logic
- no DLB
- no automatic phase switching
- optional adaptive 1P to 3P phase recovery
- optional web UI restart button
- no writes on unplug except normal keepalive

EVCC should own charging logic. This bridge only adapts Home Assistant entities to the charger Modbus registers.

On every new Modbus TCP connection, the bridge performs the charger's expected
ownership setup: failsafe timeout, failsafe current, charging current and alive.
If no EVCC current intent is known yet, charging current is initialized to `0A`
so Home Assistant never starts charging by itself. The Vestel phase switch
register `405` resets to its default after a Modbus TCP disconnection, so the
bridge also reasserts the requested phase after reconnect once EVCC has provided
a phase intent. During each poll cycle the bridge continues to write the alive
heartbeat. The effective poll interval is clamped to
`min(poll_interval, max(3, failsafe_timeout / 2))` so the alive write always
stays within the cadence recommended by the Vestel specification. If Home
Assistant, the network, or this integration stops writing the heartbeat, the
charger falls back to the configured failsafe current after the configured
failsafe timeout.

## Installation

### HACS (recommended)

[![Open your Home Assistant instance and open this repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Dextro86&repository=unite_evcc_bridge&category=integration)

Or add it manually:

1. In HACS, open the three-dot menu and choose **Custom repositories**.
2. Add `https://github.com/Dextro86/unite_evcc_bridge` with category **Integration**.
3. Install **Unite EVCC Bridge** and restart Home Assistant.

### Manual

Copy the full integration directory to Home Assistant:

```text
custom_components/unite_evcc_bridge/
```

Keep the included `brand/` directory in place. It contains the EVCC logo and icon assets used by the integration:

- `brand/icon.png`
- `brand/icon@2x.png`
- `brand/logo.png`
- `brand/logo@2x.png`

Restart Home Assistant after copying or updating the integration. If the old icon is still shown, clear the browser/app cache.

## Settings

Open the integration options in Home Assistant to configure:

- **Charging**: maximum charge current and optional adaptive 1P to 3P recovery
- **Advanced**: polling interval, failsafe current and failsafe timeout
- **Restart (web UI)**: optional restart button using the charger web UI login

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
the button automatically falls back to the webconfig soft-reset, so it works across
Unite variants. Modbus control is not changed by the button; after the charger
restarts, the normal reconnect handshake claims the Modbus session again. The
restart button has a 300 second cooldown because the charger web UI can stay
offline for several minutes while Modbus is already back. The diagnostic
**Last restart** sensor stores the timestamp and result of the latest web UI
restart request without polling the REST API periodically.

## Phase switching

Whether a **live** 1→3 switch takes effect mid-session is **car-dependent**: some
cars pick up the extra phases immediately, others cache their 1p/3p choice for the
whole session and ignore a live upshift. The optional 1P→3P recovery below is for
those cars.

By default, phase switching is a direct EVCC passthrough:

1. `select_option("1")` writes `phase = 1P` to register `405`.
2. `select_option("3")` writes `phase = 3P` to register `405`.
3. The select state reports the requested phase to EVCC.

If **Enable 1P to 3P phase recovery** is turned on, the bridge first tries the
same live `405 = 1` switch. It then observes measured L1/L2/L3 current for the
configured observation time. If the car is still effectively charging on one
phase, the bridge temporarily takes over the command path:

1. hold `5004 = 0` for the configured recovery time,
2. restore the latest requested EVCC current.

The second `405 = 1` write is intentionally not repeated during recovery. The
phase register was already set during the initial live switch; recovery only
forces a long enough charging pause for the vehicle to renegotiate when current
is restored.

EVCC current and enable commands are buffered only during that recovery window.
Cars that switch to measured 3P during observation are not interrupted.

During recovery, EVCC-facing entities report the last requested intent:

- `switch.<name>_charging_enabled` keeps reporting EVCC's requested enabled state.
- `number.<name>_maximum_current` keeps reporting EVCC's requested current.
- `select.<name>_phase_mode` keeps reporting EVCC's requested phase.

The temporary hardware stop is exposed separately through diagnostics, including
the hardware current limit and the phase recovery status sensors.

> **Known Unite bug — stuck on 1-phase after a new session.** Separate from the
> car-dependent behaviour above, the Unite firmware itself sometimes gets stuck:
> after one charging session ends and a new one starts, the charger can begin on a
> single phase even though 3-phase is requested (`405 = 3`) and stay locked that
> way. A live phase switch does not clear it — the only reliable fix is a
> **soft reset** of the charger, which you can trigger from the **Restart** button
> (enable *Restart (web UI)* in the options).

## EVCC Home Assistant charger entities

Use these entities in EVCC (referred to here by their display name):

| EVCC field | Entity (by name) |
|---|---|
| status | `sensor` **evcc status** |
| enabled | `switch` **Charging** |
| enable | `switch` **Charging** |
| maxcurrent | `number` **Charge current** |
| power | `sensor` **Power** |
| energy | `sensor` **Total energy** |
| currentL1 / L2 / L3 | `sensor` **Current L1 / L2 / L3** |
| voltageL1 / L2 / L3 | `sensor` **Voltage L1 / L2 / L3** |
| phases1p3p | `select` **Phase** |

> **Finding the exact `entity_id`:** EVCC needs entity IDs, and Home Assistant
> generates those from the entity's display name **in your HA language** — so on a
> Dutch install the `status` entity is `sensor.<device>_evcc_status`, `Charging`
> becomes `switch.<device>_laden`, `Charge current` becomes
> `number.<device>_laadstroom`, and `Phase` becomes `select.<device>_fase`. Look up
> the real IDs under *Developer Tools → States* (filter on your device name) and
> paste those into EVCC.

## Register choices

The Modbus registers the bridge uses:

- `1004` input: cable status
- `1001` input: charging status
- `5004` holding: current limit
- `6000` holding: alive/keepalive
- `405` holding: phase switch, `0 = 1P`, `1 = 3P`
- `404` input: phase switching capability / phase support
- `1020` input uint32: active power W
- `1036` input uint32: total energy, scale `0.1 kWh`
