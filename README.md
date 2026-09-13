# Unite EVCC Bridge

[![HACS: Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![GitHub release](https://img.shields.io/github/v/release/Dextro86/unite_evcc_bridge?display_name=tag)](https://github.com/Dextro86/unite_evcc_bridge/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-local%20polling-blue.svg)](https://www.home-assistant.io/)

Minimal Home Assistant custom integration to expose a Webasto Unite / Vestel EVC04 as an evcc Home Assistant charger.

> Built for stability: block reads, one persistent connection, and a
> heartbeat/failsafe watchdog — plus firmware-tolerant handling of optional
> registers, so old wallboxes stay online too.

**What it does:** expose the charger to evcc (template or custom charger),
optional 1-phase help, Unite-bug restore and web-UI reboot. When the bridge
leaves, it puts the charger's registers back the way it found them.

**What it does not do:** solar logic, DLB and automatic phase switching
natively; evcc owns all charging logic! Cloud and OCPP; it talks only to the
charger on your LAN, and targets the Vestel EVC04 family (Webasto Unite).

Available in **English and Dutch** — Home Assistant picks the user's language.

## Features

- **Monitoring** — status, power, per-phase current & voltage, session energy &
  duration, total energy, session RFID tag, plus diagnostics (connection, raw
  registers 404/405).
- **evcc passthrough** — charging on/off, current and phases exactly as evcc
  expects them (template or custom charger); evcc owns all decisions.
- **Help for cars stuck on 1 phase** *(opt-in, off by default)* — observes and,
  only if the car stays single-phase, forces a pause so it re-negotiates on
  3 phases.
- **Restore for Unite bug (stuck on 1 phase)** *(opt-in, off by default)* —
  re-applies the 3-phase config after every unplug; needs the web UI login and
  a three-phase connection.
- **Restart button (web UI)** *(opt-in)* — reboot the wallbox over its local
  web UI.
- **Safety** — failsafe current/timeout, alive heartbeat, and a register
  baseline captured before the first write and restored on exit;
  firmware-tolerant RFID probing.

## Requirements

- A Webasto Unite with **Modbus TCP enabled** (in the charger's web UI).
- The charger's IP address. Default port `502`, Modbus unit id `255`.
- Only **one** Modbus master may talk to the charger at a time — do not point this
  bridge and another Modbus client (evcc's own Modbus charger, or the full Unite EV
  Charger integration) at the same wallbox simultaneously.
- Older firmware is supported: registers your firmware lacks (such as the RFID
  tag) are detected once and then left alone. See
  [Firmware differences](#firmware-differences).

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

Everything else is configured afterwards via **Configure**, screen by screen below.

## Settings

### Charging

How the charger charges. evcc decides; this only passes it on.

- **Maximum current** — normally 16 A (about 11 kW on 3 phases).
- **Charger connection** — single- or three-phase, as your electrician wired
  it. This also keeps the phase fixes below from running on the wrong
  installation.
- **Help cars stuck on 1 phase** — for cars that don't pick up 3 phases by
  themselves after switching; briefly interrupts charging, so off by default.
- **Restore for Unite bug (stuck on 1 phase)** — pushes the 3-phase setting to
  the charger again after every unplug. Requires the web UI login and a
  three-phase connection.

### Advanced

Only change these if you know why. Wrong values can stall charging.

- **Check-in frequency** — how often the charger is polled (normally 10 s).
- **Backup current if contact is lost** — what the charger falls back to after
  silence (normally 6 A; 0 A stops charging entirely).
- **Waiting time before backup** — how long the silence may last (normally 30 s).
- **Watching time before 1-phase help** (normally 60 s), **pause length for
  1-phase help** (normally 121 s) and **waiting time after unplugging**
  (normally 5 s) — these only apply when their switch on the Charging screen
  is on.

### Web UI

Logs in to the charger's web interface. This adds a Restart button — only use
it when the charger is stuck, it stops an ongoing charging session. The login
is also required for *Restore for Unite bug* on the Charging screen. The
password is stored locally only.

### Status & diagnostics

The integration also exposes a diagnostic **Connection** binary sensor with
reconnect counters, Modbus failure counters, timeout counters, heartbeat
failures and response timing.

The primary **Status** sensor interprets the charger state as one of:
`idle`, `connected`, `charging`, `phase_mismatch`, `recovery`, `restarting`,
`disconnected` or `fault`. The diagnostic **Phase mismatch** binary sensor turns
on when 3-phase was explicitly requested while measured current shows the
vehicle is still effectively charging on L1 only. The diagnostic **Last phase
recovery** sensor stores the timestamp and result of the latest recovery attempt.

## Phase switching

Whether a **live** 1→3 switch takes effect mid-session is **car-dependent**: some
cars pick up the extra phases immediately, others cache their 1p/3p choice for the
whole session and ignore a live upshift. The optional 1-phase help below is for
those cars.

By default, phase switching is a direct evcc passthrough:

1. `select_option("1")` writes `phase = 1P` to register `405`.
2. `select_option("3")` writes `phase = 3P` to register `405`.
3. The select state reports the requested phase to evcc.

If **Help cars stuck on 1 phase** is turned on, the bridge first tries the
same live switch. It then observes measured L1/L2/L3 current for the
configured watching time. If the car is still effectively charging on one
phase, the bridge temporarily takes over the command path:

1. hold `5004 = 0` for the configured pause length,
2. restore the latest requested evcc current.

The phase register was already set during the initial live switch; recovery only
forces a long enough charging pause for the vehicle to renegotiate when current
is restored.

evcc current and enable commands are buffered only during that recovery window.
Cars that switch to measured 3P during observation are not interrupted.

During recovery, evcc-facing entities report the last requested intent:

- `switch.<name>_charging_enabled` keeps reporting evcc's requested enabled state.
- `number.<name>_maximum_current` keeps reporting evcc's requested current.
- `select.<name>_phase_mode` keeps reporting evcc's requested phase.

The temporary hardware stop is exposed separately through diagnostics.

## Restore for Unite bug (stuck on 1 phase)

Separate from the car-dependent behaviour above, the Unite firmware itself
sometimes gets stuck: after one charging session ends and a new one starts, the
charger can begin on a single phase even though 3-phase is requested and stay
locked that way. A live phase switch does not clear it.

When enabled, the bridge re-applies the installation phase config after
**every** unplug: it waits a few seconds (so the charger can finish the session;
plugging back in within this time cancels the restore), then briefly sets the
charger to 1 phase and back to 3 phases over the web UI, forcing the firmware
to apply its own default. This runs over the **web UI** (Modbus has no such
register), so it needs the Web UI login, and it never runs on a genuine
1-phase installation.

If the charger itself is thoroughly stuck (a plain switch doesn't clear it),
the reliable fix remains a **restart** — see [Web UI details](#web-ui-details).

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

## Leaving the integration

Register `2000` (failsafe current) is persistent user-visible configuration:
it survives a Modbus disconnect and even a power cycle, and a stale value
actively drives behaviour — the charger overwrites the charge current with it
once Alive lapses. Whatever an integration leaves in `2000` is what the
charger applies on every future communication loss, indefinitely.

So before its first write, this bridge captures the registers it manages
(charge current, failsafe current/timeout, phase selection) and stores them
durably. On unload, removal and Home Assistant shutdown it writes them back
and verifies by read-back. Anything that cannot be restored is logged with its
value for manual recovery. The baseline answers "before us", never a guessed
factory default — and changing your settings later does not touch it.

## Web UI details

Modbus has no reboot register, so a restart goes over the charger's local **web
UI**. Different Unite firmware/interfaces expose different web UIs, so the button
**auto-detects** the right one: the modern JSON API over HTTPS (on port `443` or
`4443`, self-signed certificate) or the legacy "webconfig" portal over HTTP. If
the JSON API is present but has no restart endpoint on that firmware, it
automatically falls back to the webconfig reset — so it works across Unite
variants.

It performs a **hard reset** (restart immediately, regardless of state) on every
firmware, which **interrupts an active charging session** — it's a deliberate
manual action. When web UI restart is enabled, Home Assistant tests the web UI
login before saving the option.

It is **opt-in**: enable it under *Settings → Web UI* and enter the web-UI
username (usually `admin`) and password; only then does the **Restart** button
appear. Modbus control is not changed by the button; after the charger restarts,
the normal reconnect handshake claims the Modbus session again. The restart
button has a 300 second cooldown because the charger web UI can stay offline for
several minutes while Modbus is already back. The diagnostic **Last restart**
sensor stores the timestamp and result of the latest web UI restart request
without polling the REST API periodically.

## Use in evcc

Three ways to connect evcc; no evcc sponsor token is needed for any of them.

### Option 1 — evcc web UI (easiest)

In the evcc web UI go to Configuration, add a charger of type Home Assistant,
pick your instance (auto-discovered) and select the entities from the
dropdowns — use the entity reference below to pick the right ones. evcc
handles the Home Assistant login itself; no token to copy.

### Option 2 — template in evcc.yaml

This integration exposes the charger as an
[evcc Home Assistant charger](https://docs.evcc.io/en/chargers/home-assistant-charger/).
The entity IDs are **fixed and language-independent** (they don't change with your
Home Assistant language), so you can copy this straight into your `evcc.yaml`.
Needs a Home Assistant long-lived access token
(HA → profile → Long-lived access tokens):

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

### Option 3 — custom charger (RFID + phases via the evcc UI)

The template has no `identify` field, so RFID vehicle identification needs a
user-defined (`type: custom`) charger — which can also be built in the evcc web
UI. Like option 2 this talks to the Home Assistant API directly, so it needs
the URI and a long-lived access token. Replace `http://homeassistant.local:8123`
and `<TOKEN>` below (and add the `_2` suffix if you have a second charger):

```yaml
status:
  source: http
  uri: http://homeassistant.local:8123/api/states/sensor.unite_evcc_bridge_iec61851_status
  headers:
    - Authorization: Bearer <TOKEN>
  jq: .state
enabled:
  source: http
  uri: http://homeassistant.local:8123/api/states/switch.unite_evcc_bridge_charging_enabled
  headers:
    - Authorization: Bearer <TOKEN>
  jq: .state == "on"
enable:
  source: ifelse
  if:
    source: http
    uri: http://homeassistant.local:8123/api/services/switch/turn_on
    method: POST
    headers:
      - Authorization: Bearer <TOKEN>
      - Content-Type: application/json
    body: '{"entity_id": "switch.unite_evcc_bridge_charging_enabled"}'
  else:
    source: http
    uri: http://homeassistant.local:8123/api/services/switch/turn_off
    method: POST
    headers:
      - Authorization: Bearer <TOKEN>
      - Content-Type: application/json
    body: '{"entity_id": "switch.unite_evcc_bridge_charging_enabled"}'
maxcurrent:
  source: http
  uri: http://homeassistant.local:8123/api/services/number/set_value
  method: POST
  headers:
    - Authorization: Bearer <TOKEN>
    - Content-Type: application/json
  body: '{"entity_id": "number.unite_evcc_bridge_maximum_current", "value": ${maxcurrent}}'
power:
  source: http
  uri: http://homeassistant.local:8123/api/states/sensor.unite_evcc_bridge_active_power
  headers:
    - Authorization: Bearer <TOKEN>
  jq: .state | tonumber
energy:
  source: http
  uri: http://homeassistant.local:8123/api/states/sensor.unite_evcc_bridge_energy_total
  headers:
    - Authorization: Bearer <TOKEN>
  jq: .state | tonumber
currents:
  - source: http
    uri: http://homeassistant.local:8123/api/states/sensor.unite_evcc_bridge_current_l1
    headers:
      - Authorization: Bearer <TOKEN>
    jq: .state | tonumber
  - source: http
    uri: http://homeassistant.local:8123/api/states/sensor.unite_evcc_bridge_current_l2
    headers:
      - Authorization: Bearer <TOKEN>
    jq: .state | tonumber
  - source: http
    uri: http://homeassistant.local:8123/api/states/sensor.unite_evcc_bridge_current_l3
    headers:
      - Authorization: Bearer <TOKEN>
    jq: .state | tonumber
voltages:
  - source: http
    uri: http://homeassistant.local:8123/api/states/sensor.unite_evcc_bridge_voltage_l1
    headers:
      - Authorization: Bearer <TOKEN>
    jq: .state | tonumber
  - source: http
    uri: http://homeassistant.local:8123/api/states/sensor.unite_evcc_bridge_voltage_l2
    headers:
      - Authorization: Bearer <TOKEN>
    jq: .state | tonumber
  - source: http
    uri: http://homeassistant.local:8123/api/states/sensor.unite_evcc_bridge_voltage_l3
    headers:
      - Authorization: Bearer <TOKEN>
    jq: .state | tonumber
identify:
  source: http
  uri: http://homeassistant.local:8123/api/states/sensor.unite_evcc_bridge_session_rfid
  headers:
    - Authorization: Bearer <TOKEN>
  jq: .state
phases1p3p:
  source: http
  uri: http://homeassistant.local:8123/api/services/select/select_option
  method: POST
  headers:
    - Authorization: Bearer <TOKEN>
    - Content-Type: application/json
  body: '{"entity_id": "select.unite_evcc_bridge_phase_mode", "option": "${phases1p3p}"}'
tos: true
```

The charger reports the RFID tag of the running session
(`sensor.unite_evcc_bridge_session_rfid`). It is empty when charging freely.
evcc matches it against the `identifiers` of your vehicles (see
[vehicle identification](https://docs.evcc.io/en/reference/configuration/vehicles/))
to assign a session to a vehicle. `evcc charger` in a terminal shows per
attribute whether it works.

## Firmware differences

Not every Unite firmware serves the same registers. The bridge handles that by
construction:

- **Required** (telemetry, session, control path): these exist on all known
  firmware. A failure here is treated as a real outage (reconnect, retry).
- **Optional** (currently only the session RFID tag, Modbus `1516-1530`,
  firmware from Vestel spec v1.9 / 2023 onward): probed once per connection.
  A clean refusal disables it until the next reconnect; repeated
  timeouts disable it for the session. The sensor reads "unknown" and
  everything else keeps working — no log ping-pong, no retry storms, and a
  failed probe never drops the connection.
