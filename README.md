# Oral-B Live

[![hacs][hacs-badge]][hacs-url]
[![release][release-badge]][release-url]

**Live brushing data for Oral-B iO toothbrushes in Home Assistant.**

Recent iO firmware stopped broadcasting live session data over Bluetooth
advertisements. This integration retrieves it over a GATT connection
instead, restoring the real-time brushing timer, quadrant tracking,
pressure and mode that passive listening can no longer provide, and adds
a persistent brushing log.

---

## Contents

- [The problem](#the-problem)
- [How it works](#how-it-works)
- [How this differs from the official integration](#how-this-differs-from-the-official-integration)
- [Entities](#entities)
- [Tested with](#tested-with)
- [Installation](#installation)
- [Configuration](#configuration)
- [Dashboard](#dashboard)
- [Requirements](#requirements)
- [Known limitations](#known-limitations)
- [Protocol notes](#protocol-notes)
- [Troubleshooting](#troubleshooting)
- [Credits](#credits)

---

## The problem

Oral-B toothbrushes traditionally broadcast their state in BLE
advertisements once per second while brushing. Home Assistant's official
`oralb` integration listens for these passively, which works well and
costs nothing in battery or connection slots.

On recent iO firmware this changed. During a brushing session the brush
is effectively silent on the air: no advertisements while the motor
runs, followed by a single post-session summary carrying only the final
duration. A passive listener therefore sees a brushing session as a
sudden jump from idle to "you brushed for 96 seconds", with no live
timer, no quadrant progress and no mid-session updates.

The underlying data still exists. It simply moved from broadcasts to
**GATT notifications**, which require an active connection.

## How it works

Oral-B Live runs a hybrid coordinator:

**Passive layer.** Listens to advertisements (manufacturer ID `0x00DC`)
exactly like the official integration. Zero cost while the brush sleeps
on its charger, and it keeps state, battery and pressure up to date
between sessions.

**Active layer.** As soon as an advertisement reports an awake state,
the integration opens a GATT connection — through any connectable
Bluetooth path, including ESPHome Bluetooth proxies with active
connections enabled — and subscribes to the live notification
characteristics. The 1 Hz brushing timer, pressure, quadrant and mode
flow straight into the entities.

**Polite by design.** The brush accepts a single client. If the
connection cannot be won, typically because the Oral-B phone app or an
iO Sense charger holds it, the integration falls back to passive mode
rather than fighting for the device. When the brush returns to charging
or sleep, the connection is released promptly so other clients can sync.

## How this differs from the official integration

Home Assistant ships an `oralb` integration. It is a good fit for most
brushes and should be preferred where it works: it is fully passive,
costs no Bluetooth connection slots, and needs no custom component.

This integration exists for firmware where passive listening is no
longer enough.

| | Official `oralb` | Oral-B Live |
| --- | --- | --- |
| Data source | Advertisements only | Advertisements plus GATT notifications |
| Connection | Never connects (battery uses a poll) | Connects while the brush is awake, releases on charge/sleep |
| Live timer on recent iO firmware | Not available | 1 Hz while brushing |
| Live pressure during a session | Not available | `low` / `normal` / `high` from `ff0b` |
| Live quadrant during a session | Not available | Yes, as the brush paces them |
| Brushing log | None | Last session, duration, sessions today, kept across restarts |
| Number of sectors | From advertisement | Read from the brush's quadrant configuration |
| Battery | Active poll | Read once per connection |
| Cost | None | One Bluetooth connection slot while the brush is awake |
| Competes with the phone app | No | Yes — falls back to passive when the app wins |

On older brushes that still broadcast during a session, the official
integration already shows everything this one does, without the
connection cost. Check there first.

Practical trade-offs worth knowing before switching:

- The brush accepts one client. While this integration is connected, the
  Oral-B app and an iO Sense charger cannot sync.
- Holding a connection uses more brush battery than passive listening.
  Lower `IDLE_DISCONNECT_SECONDS` in `const.py` if that matters to you.
- Entity IDs differ from the official integration, so dashboard cards
  need repointing after switching.

## Entities

Entity structure mirrors the official `oralb` integration, so existing
dashboards and toothbrush cards keep working.

| Entity | Description |
| --- | --- |
| Toothbrush state | `idle`, `running`, `charging`, `selection_menu`, `session_summary`, `post_brushing_summary`, ... |
| Time | Session duration in seconds. Updates at 1 Hz while connected. |
| Sector | Current quadrant (`sector_0` ... `sector_3`, or `no_sector`) |
| Number of sectors | Read from the brush's quadrant time configuration |
| Mode | `daily_clean`, `sensitive`, `gum_care`, `whiten`, `intense`, ... |
| Pressure | `low` / `normal` / `high`, live while connected |
| Battery | Percentage, read on connect |
| Last session | Timestamp of the last completed session |
| Last session duration | Length of that session, in seconds |
| Sessions today | Number of sessions today, resets at midnight |

The state entity also exposes `live_connection`, `rssi`, `state_raw` and
`mode_raw` as attributes, which is useful when diagnosing whether a
session was captured actively or passively.

### Brushing log

The brush does not hand over its stored history, so the integration
builds its own. When a session ends, **Last session** records the start
time with `duration_seconds`, `mode`, `quadrants_covered` and
`high_pressure_events` as attributes. Because it is a proper timestamp
sensor, Home Assistant's recorder keeps the history automatically: a
history graph on **Last session duration** is a complete brushing log
that accumulates from installation onwards.

Session values are restored across restarts and stay readable while the
brush is out of range. Sessions with no recorded duration (the motor
switched straight back off) are ignored.

## Tested with

Developed and tested against a single device. Reports from other models
are welcome, particularly the raw values behind any `unknown_state_<n>`
or `mode_<n>`.

| | |
| --- | --- |
| Brush | **Oral-B iO Series 10** |
| Advertised identity | Protocol version `0x08`, model ID `0x36` (54), variant `0x52` |
| Firmware | Mid-2026; broadcasts no live data during a session |
| Accessory | iO Sense charger present (not required) |
| Home Assistant | 2026.7 on Home Assistant OS, Raspberry Pi |
| Bluetooth | ESPHome proxy (M5Stack Atom) with active connections, plus the Pi's built-in adapter and a Shelly BLE scanner |
| Protocol version | 8 (advertisement byte 0) |

The advertisement does not carry the marketing model number. Model ID
`0x36` maps to the generic "IO Series" entry shared by most of the line
(only iO 4 and iO 5 have distinct IDs, `0x34` and `0x35`), which is why
Home Assistant names these brushes "IO Series" plus a MAC suffix. The
iO Series 10 above was identified from the physical device, not the
protocol.

Older iO models and pre-2026 firmware are expected to work, but on those
the official integration may already be sufficient — see
[How this differs](#how-this-differs-from-the-official-integration).

## Installation

### HACS (recommended)

1. In HACS, open the three-dot menu and choose **Custom repositories**.
2. Add `https://github.com/thomasgregg/oralb-ha` with category
   **Integration**.
3. Install **Oral-B Live** and restart Home Assistant.

### Manual

Copy `custom_components/oralb_live` into your Home Assistant
`config/custom_components/` directory and restart.

## Configuration

1. **Disable the official Oral-B entry** for the same brush under
   *Settings, Devices & Services*. Both integrations competing for one
   device causes duplicate entities and failed connections. Remember to
   repoint any dashboard cards at the new entity IDs afterwards.
2. Wake the brush by pressing its button. Oral-B Live discovers it
   automatically; confirm the discovered device.
3. Alternatively add it manually via *Settings, Devices & Services, Add
   integration, Oral-B Live*.

## Dashboard

The entities are named to match the official integration, so
[toothbrush-card](https://github.com/Anrolosia/toothbrush-card) works
without changes. Point it at the Oral-B Live device and it renders the
live timer, quadrant ring, pressure, mode and battery:

```yaml
type: custom:toothbrush-card
device_id: <your Oral-B Live device id>
show_subtitle: true
show_header: false
```

A brushing log underneath, using the session entities:

```yaml
type: grid
cards:
  - type: heading
    heading: Brushing log
    heading_style: subtitle
    icon: mdi:calendar-check
  - type: tile
    entity: sensor.<your_brush>_last_session
    name: Last session
    icon: mdi:calendar-clock
    grid_options:
      columns: 6
  - type: tile
    entity: sensor.<your_brush>_last_session_duration
    name: Duration
    icon: mdi:timer-outline
    grid_options:
      columns: 6
  - type: tile
    entity: sensor.<your_brush>_sessions_today
    name: Sessions today
    icon: mdi:counter
  - type: history-graph
    title: Brushing history
    hours_to_show: 336
    entities:
      - entity: sensor.<your_brush>_last_session_duration
        name: Session duration
```

Replace `<your_brush>` with your brush's entity prefix, which is its MAC
address with underscores (for example `58_26_3a_f6_64_d3`).

## Requirements

- Home Assistant 2024.1 or newer
- A **connectable** Bluetooth path within range of the brush. A built-in
  adapter works; an ESPHome Bluetooth proxy in the bathroom works
  better.

For an ESPHome proxy, enable active connections and continuous scanning:

```yaml
esp32_ble_tracker:
  scan_parameters:
    interval: 320ms
    window: 320ms
    continuous: true

bluetooth_proxy:
  active: true
```

Without `continuous: true`, the proxy listens roughly 10% of the time
and will miss advertisements from a brush that only speaks occasionally.

## Known limitations

**Quadrant changes are paced by the brush.** The brush emits a quadrant
notification only when the sector actually changes, typically every 30
seconds depending on its configuration. The quadrant is the brush's own
pacer telling you where to brush next, not a detection of where the
brush actually is. Short sessions may show only one quadrant.

**The phone app wins connection races.** With Bluetooth enabled and the
Oral-B app paired, the app may claim the brush first. The integration
then reports passive data only, which on recent firmware can mean just
the end-of-session summary.

**Stored session history is not available.** The brush's own records are
not exposed to unauthenticated clients; see
[Protocol notes](#protocol-notes). The integration builds its own log
from live sessions instead.

## Protocol notes

Findings from GATT reconnaissance of an Oral-B iO Series 10 (model ID
`0x36`, protocol version 8), July 2026, cross-checked against
MatrixEditor/oralb-io.
Documented here so others do not have to repeat the work.

### Vendor service `a0f0ff00-5047-4d53-8208-4f72616c2d42`

| Characteristic | Access | Content |
| --- | --- | --- |
| `ff01` | read | Device MAC, byte-reversed |
| `ff02` | read | Model identifier |
| `ff04` | notify, read | Toothbrush state, `[state, 0]` |
| `ff05` | notify, read | Status blob; byte 0 is battery percentage |
| `ff06` | notify, read | Button state (0 none, 1 power, 2 mode) |
| `ff07` | notify, read | Brushing mode |
| `ff08` | notify, read | Brushing time as `[minutes, seconds]`, 1 Hz while running |
| `ff09` | notify, read | Current quadrant |
| `ff0a` | notify, read | Smiley rating |
| `ff0b` | notify, read | **Pressure** (0 low, 1 normal, 2 high) |
| `ff0c` | read, write, notify | Cache — requires authentication |
| `ff0d` | notify, read | Motion sensor data, roughly 30 Hz |

Configuration service `a0f0ff20-...`:

| Characteristic | Access | Content |
| --- | --- | --- |
| `ff21` | read, write, notify | Control channel (commands) |
| `ff22` | read, write | Real-time clock, seconds since 2000-01-01 |
| `ff25` | read, write | Available brushing modes |
| `ff26` | read, write | Quadrant times, seconds per sector |
| `ff29` | read | Session data |

Service `a0f0ff80-...` is the over-the-air firmware update channel
(`ff81` OTA command, `ff82` OTA payload) and is not used by this
integration.

No pairing or bonding is required for these. Anonymous connections are
accepted.

### Pressure

Pressure is delivered on `ff0b` as a single state byte: `0` low, `1`
normal, `2` high. It notifies continuously during a session.

Note for anyone repeating this work: `ff06` is the button state, not
pressure, despite sitting where a pressure characteristic would
plausibly go. Reading it during hard brushing returns a constant
`00 00 00 00`, which is easy to misinterpret as a broken pressure
sensor.

### Stored session history

`ff29` holds a session record with a timestamp (seconds since
2000-01-01) but does not update on its own: it stayed byte-identical
across several completed sessions, so it appears to be a buffer the
control channel fills on request. `ff0c` is annotated as requiring
authentication, and `ff2c` (dashboard config) is absent on this
firmware. Retrieving history therefore needs writes to the `ff21`
control channel, whose command set also contains a factory reset, so it
is deliberately not attempted here.

### Advertisement payload

Manufacturer data `0x00DC`, 11 bytes:

| Offset | Content |
| --- | --- |
| 0 | Protocol version |
| 1-2 | Model |
| 3 | State |
| 4 | Pressure flags |
| 5-6 | Brushing time, `[minutes, seconds]` |
| 7 | Mode |
| 8 | Sector |
| 9-10 | Sector flags, reserved |

## Troubleshooting

**No live updates during brushing.** Check the `live_connection`
attribute on the state entity. If it is `false`, the connection was not
won: disable Bluetooth on the phone or unpair the Oral-B app and retry.

**Entities stop updating entirely.** Reload the integration. If this
recurs with the official `oralb` integration installed alongside,
disable that entry; see
[home-assistant/core#177039](https://github.com/home-assistant/core/issues/177039).

**Dashboard warns about unknown entities.** Cards still reference the
official integration's entity IDs. Repoint them at the Oral-B Live
equivalents.

**Unknown states or modes.** Unmapped values appear as
`unknown_state_<n>` and `mode_<n>`. Please open an issue with the raw
value and what the brush was doing, and it will be added.

## Credits

- [bkbilly/oralb_ble](https://github.com/bkbilly/oralb_ble) — pioneered
  the active-connection approach and the original characteristic map.
- [Bluetooth-Devices/oralb-ble](https://github.com/Bluetooth-Devices/oralb-ble)
  and the official
  [oralb integration](https://www.home-assistant.io/integrations/oralb/).
- [MatrixEditor/oralb-io](https://github.com/MatrixEditor/oralb-io) —
  the most complete public map of the Oral-B BLE protocol, which
  corrected several characteristic assignments used here.
- [Anrolosia/toothbrush-card](https://github.com/Anrolosia/toothbrush-card)
  — the dashboard card these entities are designed to work with.
- Ruben Faelens, for documenting the app handshake problem.

## Disclaimer

Not affiliated with, endorsed by, or connected to Oral-B or Procter &
Gamble. Protocol details were obtained by observing a single iO-series
device; other models may differ.

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-url]: https://github.com/hacs/integration
[release-badge]: https://img.shields.io/github/v/release/thomasgregg/oralb-ha
[release-url]: https://github.com/thomasgregg/oralb-ha/releases

