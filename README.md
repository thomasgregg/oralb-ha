# Oral-B Live

[![HACS][hacs-badge]][hacs-url]
[![Release][release-badge]][release-url]

**Local live brushing data for Oral-B iO toothbrushes and iO Sense chargers in Home Assistant.**

Oral-B Live combines passive Bluetooth advertisements, direct toothbrush GATT
notifications, iO Sense charger passthrough reads, and the brush's retained
session summary. It provides live timer, pressure, zone and mode entities,
keeps a persistent brushing log, and exposes supported battery, brush-head,
display and charger diagnostics without using the Oral-B cloud.

## Contents

- [How it works](#how-it-works)
- [Connection options](#connection-options)
- [Comparison with Home Assistant's built-in Oral-B integration](#comparison-with-home-assistants-built-in-oral-b-integration)
- [Data sources and fallbacks](#data-sources-and-fallbacks)
- [Entities](#entities)
- [Installation](#installation)
- [Configuration](#configuration)
- [Requirements](#requirements)
- [Protocol findings](#protocol-findings)
- [Full protocol reference](docs/protocol.md)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)

## How it works

Oral-B Live can collect brushing data in two ways. You choose the behaviour
under the integration's **Configure** menu:

- **Charger/app compatible** is recommended for an iO Sense setup. The charger
  keeps its normal connection to the toothbrush, while Home Assistant reads
  live brush data locally through the charger. Home Assistant does not take
  the brush connection slot away from the charger or phone app.
- **Home Assistant direct** connects Home Assistant straight to the toothbrush
  for its fastest notification stream. While this connection is active, the
  charger display and phone app cannot connect to the brush.

This choice is necessary because the toothbrush accepts only one BLE client at
a time and stops advertising while that connection is occupied.

```text
Charger/app compatible

Home Assistant <--local BLE--> iO Sense <--private BLE--> Toothbrush
                                 keeps brush slot

Home Assistant direct

Home Assistant <--direct BLE notifications--> Toothbrush
     owns brush slot
```

In charger/app-compatible mode, the integration discovers an iO Sense through
its `A0F03E00` service, reads the charger's cached paired-brush identity, and
matches the brush MAC to the existing config entry. No charger selection or
manual pairing step is required. Unrelated iO Sense chargers are ignored.

While the charger owns the brush connection, Home Assistant connects to the
charger and uses its read-only `0x37` passthrough command. The charger remains
the brush's BLE owner and its display continues to handle the session. The
charger also forwards the brush's retained `FF29` session summary when it
becomes available on a charger-managed connection.

## Connection options

Choose the behaviour under *Settings → Devices & services → Oral-B Live →
Configure*.

### Charger/app compatible (recommended)

Home Assistant leaves the toothbrush connection available to the iO Sense and
phone app. When a paired iO Sense is present, it is used automatically as a
local live-data bridge.

During a session the bridge uses the measured production scheduler:

- pressure (`FF0B`) is read every one-second tick;
- timer (`FF08`) and zone (`FF09`) alternate as the second read;
- the displayed timer advances locally between authoritative brush readings;
- mode and state are read at session start, with charger session state checked
  periodically for an authoritative stop signal;
- supported session, battery and display diagnostics are collected on
  charger-managed idle connections, outside the live pressure path.

This keeps pressure fresh every second and timer/zone within two seconds. A
150-second real-session benchmark completed all 444 requests without a single
failure. The measured p95 was 779 ms for pressure+timer and 808 ms for
pressure+zone.

If no matching charger is available, the same option falls back automatically
to passive brush advertisements and guarded post-session reads. There is no
additional user setting to manage.

### Home Assistant direct

Home Assistant connects directly to the brush and subscribes to the vendor
GATT characteristics. Timer, pressure, zone, mode and state arrive at the
brush's notification rate.

This is the highest-rate source, but Home Assistant owns the brush's single
connection slot. The iO Sense display and phone app cannot use the brush at the
same time.

The direct connection subscribes to state (`FF04`), mode (`FF07`), timer
(`FF08`), sector (`FF09`) and pressure (`FF0B`) notifications, with battery
(`FF05`) and smiley (`FF0A`) notifications where supported. It also reads the
brush identity, pacer, available modes, brush-head remainder and display face
when the connection is established.

A connection acquired while the brush is docked remains active when brushing
starts, so Home Assistant receives the entire session directly. The brush can
drop an idle client after approximately 30 seconds; Oral-B Live reconnects
automatically and uses a periodic retry as a backstop. The complete direct BLE
characteristic and advertisement findings are preserved in the
[protocol reference](docs/protocol.md#direct-toothbrush-ble).

## Comparison with Home Assistant's built-in Oral-B integration

Home Assistant already includes an official **Oral-B** integration. Both
integrations work locally without the Oral-B cloud, but they are intended for
different needs:

- Choose **Home Assistant Oral-B** if you want the integration included with
  Home Assistant and only need basic live toothbrush information.
- Choose **Oral-B Live** if you use an iO Sense charger, want completed-session
  history, need the additional brush diagnostics, or want to choose whether
  Home Assistant or the charger/app owns the brush connection.

| What matters to you | Home Assistant Oral-B | Oral-B Live |
| --- | --- | --- |
| Installation | Included with Home Assistant | Installed through HACS |
| Live brushing | Time, pressure, sector, mode and state when broadcast by the brush | Time, pressure, sector, mode and state through the charger or a direct brush connection |
| Completed-session summary | Not provided | Last session, duration and sessions today, retained across Home Assistant restarts |
| Detailed session result | Not available | Actual duration, mode, target, pressure summary, ending battery and session ID where supported |
| Battery | Percentage | Percentage plus estimated brushing runtime remaining on the current charge, voltage, signed current and temperature where supported |
| Additional brush information | Basic toothbrush information | Smiley, brush-head remainder, pacer setup, target duration and additional iO modes |
| iO Sense charger | Not exposed | Separate charger device with connection, display, light, clock, Wi-Fi and transport diagnostics |
| Works while using the Oral-B app or charger display | Normally, because it mainly listens for broadcasts | Yes with **Charger/app compatible**; not at the same time with **Home Assistant direct** |
| Connection choice | Automatic | **Charger/app compatible** or **Home Assistant direct** |
| Oral-B cloud required | No | No |
| Changes brush or charger settings | No | No; access is read-only |

In **Charger/app compatible**, the iO Sense keeps its private brush connection
and Oral-B Live obtains live and retained data through the charger. In **Home
Assistant direct**, Home Assistant takes the brush's single connection for the
highest-rate notifications, so the app and charger display cannot use the
brush simultaneously.

Only one integration should manage a given toothbrush in Home Assistant.
Disable the other config entry for that brush to avoid duplicate devices,
entities and Bluetooth work.

## Data sources and fallbacks

The state entity exposes the active `data_source` so the path is always visible:

| Data source | Meaning |
| --- | --- |
| `charger_bridge` | Live reads forwarded locally through a matched iO Sense |
| `direct_brush` | Direct GATT notifications from the toothbrush |
| `advertisement` | Passive manufacturer data from the toothbrush |

Sources are selected automatically inside the chosen connection option.
Entity IDs stay the same when the source changes.

Completed sessions are saved immediately from the live or passive stream. The
brush retains one authoritative `FF29` summary containing exact duration,
mode, pressure totals, event counts and ending battery. Through the charger it
becomes readable on the next charger-managed brush connection and refines the
already-recorded session without counting it twice.
The Last session entity records `source: retained_session` after this
reconciliation.

## Entities

### Toothbrush device

| Entity | Description |
| --- | --- |
| Toothbrush state | `idle`, `running`, `charging`, `selection_menu`, summaries and diagnostic states |
| Time | Current brushing duration; locally advanced between charger timer anchors |
| Pressure | `low`, `normal` or `high`; charger reads also expose raw force as an attribute |
| Mode | Daily clean, sensitive, gum care, whiten, intense, super sensitive, tongue clean, Smart Adapt, gentle white and supported unknown values |
| Sector | Current pacer sector (`sector_1` … `sector_8`) |
| Sector timer | Advertised sector time where available |
| Number of sectors | Configured pacer sector count |
| Target duration | Sum of configured per-sector times |
| Smiley | Brush display face from `FF0A` |
| Battery | Brush battery percentage |
| Battery diagnostics | Estimated brushing runtime remaining on the current charge, voltage, signed current and temperature where supported |
| Brush-head diagnostics | Remaining days and brushing seconds from `FF2D` |
| Last session | Timestamp plus complete session attributes |
| Last session duration | Duration of the latest session |
| Sessions today | Daily session counter, retained across restarts |

The **Last session** attributes can include:

- duration and brushing mode;
- source and session identifier;
- configured target and sectors covered;
- high/low-pressure event counts and durations;
- average and maximum pressure in millinewtons;
- battery percentage at the end of the session.

### iO Sense Charger device

A successfully matched charger appears as a separate device connected through
the toothbrush device. Its read-only entities include:

| Entity | Description |
| --- | --- |
| State | Charger availability and connection state, with firmware, hardware, MAC, pairing, charging and bridge details as attributes |
| Session status | Whether the charger reports an active brushing session |
| Brush status | Paired-brush connection and charging status reported by the charger |
| Wi-Fi status | Current charger Wi-Fi state |
| Wi-Fi signal | Received signal strength in dBm |
| Cloud connection | Charger connection to the Oral-B service; the integration itself does not use the cloud |
| Internet type | Network transport reported by the charger |
| Displayed time | Clock text currently shown on the charger |
| Timezone | Charger timezone setting |
| Clock format | 12- or 24-hour display mode |
| Date display format | Configured date layout |
| Clock brightness | Configured clock brightness percentage |
| Night-light mode | Current night-light configuration |
| Ring color | Configured charger ring color |
| Uptime | Time since the charger last restarted |
| Automatic updates | Whether automatic charger firmware updates are enabled |
| Touchpad status | Current rear-touchpad state |
| Brush connection policy | Charger policy for maintaining its paired-brush connection |

Uptime, automatic updates, touchpad status and brush connection policy are
disabled by default. The integration does not write display, light, network or
update settings.

## Installation

### HACS

1. In HACS, open **Custom repositories**.
2. Add `https://github.com/thomasgregg/oralb-ha` as an **Integration**.
3. Install **Oral-B Live**.
4. Restart Home Assistant.

HACS tracks GitHub releases. After a new release is published, open HACS and
select **Redownload** or install the offered update, then restart Home Assistant.

### Manual

Copy `custom_components/oralb_live` into
`config/custom_components/oralb_live` and restart Home Assistant.

## Configuration

1. Disable the official Oral-B config entry for the same brush to avoid
   duplicate entities and competing Bluetooth activity.
2. Wake the toothbrush by pressing its button.
3. Add or confirm **Oral-B Live** under *Settings → Devices & services*.
4. Open **Configure** and choose one of the two connection options.

The default charger/app-compatible option discovers and matches an iO Sense
automatically. The charger must be within range of a connectable Home Assistant
Bluetooth adapter or proxy.

## Dashboard

The main entities follow the structure expected by
[toothbrush-card](https://github.com/Anrolosia/toothbrush-card):

```yaml
type: custom:toothbrush-card
device_id: <your Oral-B Live device id>
show_subtitle: true
show_header: false
```

A simple session log:

```yaml
type: grid
cards:
  - type: tile
    entity: sensor.<your_brush>_last_session
    name: Last session
  - type: tile
    entity: sensor.<your_brush>_last_session_duration
    name: Duration
  - type: tile
    entity: sensor.<your_brush>_sessions_today
    name: Sessions today
  - type: history-graph
    title: Brushing history
    hours_to_show: 336
    entities:
      - sensor.<your_brush>_last_session_duration
```

## Requirements

- Home Assistant 2024.1 or newer.
- A connectable Bluetooth adapter or ESPHome Bluetooth proxy near the brush.
- An iO Sense charger for charger-bridge data; the integration still operates
  without one through its other local sources.

Recommended ESPHome proxy configuration:

```yaml
esp32_ble_tracker:
  scan_parameters:
    interval: 320ms
    window: 320ms
    continuous: true

bluetooth_proxy:
  active: true
```

## Protocol findings

All runtime communication is local. The protocol was reconstructed from local
BLE captures and vendor application behaviour and verified on an iO Series 10
with iO Sense firmware `0.3.4`.

See [the full protocol reference](docs/protocol.md) for packet layouts, the
charger command map, live-read captures, benchmark data, queue experiments and
the boundary between verified behaviour and inference.

### Toothbrush service

The brush uses vendor service `A0F0FF00-5047-4D53-8208-4F72616C2D42`.

| Characteristic | Content |
| --- | --- |
| `FF02` | Model, protocol and firmware identifiers |
| `FF04` | Brush state |
| `FF05` | Battery and supported electrical diagnostics |
| `FF07` | Brushing mode |
| `FF08` | Brushing timer (`[minutes, seconds]`) |
| `FF09` | Zero-based pacer zone plus configured zone count |
| `FF0A` | Display face / smiley |
| `FF0B` | Pressure state, force and motor data |
| `FF0D` | Motion and gyroscope snapshots |
| `FF22` | Brush real-time clock |
| `FF25` | Available modes |
| `FF26` | Per-zone pacer times |
| `FF29` | Retained last-session summary |
| `FF2D` | Brush-head/refill remainder |

### iO Sense service and passthrough

The charger advertises service `A0F03E00-5047-4D53-8208-4F72616C2D42` with
four characteristics:

| Characteristic | Purpose |
| --- | --- |
| `A0F03C00` | Command headers and protocol delimiter |
| `A0F03C01` | Charger data and passthrough responses |
| `A0F03C02` | Command payloads |
| `A0F03C03` | Command status acknowledgements |

Read-only passthrough uses charger command `0x37`. A brush read request is:

```text
command: C1 37
payload: UUID_LSB UUID_MSB 01 00
end:     E0
```

The response contains the requested short UUID, read operation, success byte,
payload length and raw brush data. Requests must be sent sequentially; combined
multi-record requests are rejected by the tested firmware.

### Retained session (`FF29`)

The verified protocol 7/8 summary uses 21 bytes, little-endian:

| Offset | Content |
| --- | --- |
| `0..3` | Session start on the brush clock |
| `4..5` | Packed 13-bit session ID and 3-bit user ID |
| `6..7` | Packed 13-bit target duration and 3-bit sector count |
| `8..9` | Session duration in seconds |
| `10..11` | High-pressure time in 100 ms units |
| `12..13` | Low-pressure time in 100 ms units |
| `14` | Average pressure in 100 mN units |
| `15` | Maximum pressure in 100 mN units |
| `16` | High-pressure event count |
| `17` | Low-pressure event count |
| `18` | Power-on event count |
| `19` | Brushing mode |
| `20` | Battery percentage at session end |

The brush clock can drift. Absolute time is calculated relative to `FF22` read
in the same connection: `wall_start = now - (rtc - session_timestamp)`.

### Charger-native information

The charger exposes local GET commands for Wi-Fi/internet/cloud status, RSSI,
clock text and format, timezone, brightness, date-display mode, night-light
mode, ring colour, firmware/hardware identity, uptime, touchpad state, brush
policy and session state. `BRUSH_DATA` is paired-brush identity and firmware
metadata—not brushing history.

The iO Sense Wi-Fi connection is used for an outbound TLS/MQTT path. No local
LAN API or locally enumerable durable session queue has been identified. Oral-B
Live does not use cloud credentials, cloud session retrieval, MQTT redirection
or certificate replacement.

## Known limitations

- The toothbrush still has one BLE client slot. Home Assistant direct mode
  intentionally occupies it.
- Charger passthrough is request/response, not a notification stream. Three
  raw timer+zone+pressure reads had a 1.141-second p95, so the integration uses
  the verified two-read scheduler instead of claiming three raw 1 Hz reads.
- The charger accepts passthrough only while it is connected to the brush.
  Static diagnostics and `FF29` are collected opportunistically during those
  managed connections.
- The current session's exact `FF29` summary becomes available through the
  charger on its next brush connection. The immediate session record is built
  locally from the live stream and reconciled later.
- The brush retains only its latest summary. The charger's richer durable
  upload queue is not exposed by any discovered local command.
- Connecting to the charger uses its BLE peripheral connection. If the phone
  app is changing charger settings at the same moment, one client may need to
  retry; the integration disconnects from an idle charger to minimise this.
- Protocol support is verified on one iO Series 10/iO Sense pair. Unsupported
  characteristics remain unknown rather than being guessed.

## Troubleshooting

### Charger is not discovered

- Confirm the entry uses **Charger/app compatible**.
- Keep the iO Sense powered and within active Bluetooth range.
- Confirm the proxy has `active: true`.
- Wake the brush once so the charger advertises its paired/connection state.
- Check the toothbrush state entity's `charger_address` and `data_source`
  attributes.

### Live values use advertisements

`data_source: advertisement` means no matched charger bridge or direct brush
connection is currently available. The integration remains functional through
its passive fallback and will switch sources automatically when possible.

### Charger display or phone app cannot use the brush

The entry is using **Home Assistant direct**. Select **Charger/app compatible**
to leave the brush connection with the charger/app.

### Session details update later

The immediate session is reconstructed locally. Exact pressure totals and
ending battery come from `FF29` on the next managed connection and update the
same session instead of creating a duplicate.

### Entities remain unavailable

Disable another integration that is managing the same brush, verify active
Bluetooth connectivity, reload the config entry, and wake the brush. Debug
logging for `custom_components.oralb_live` shows charger matching, source
selection and read failures without exposing cloud credentials.

## Credits

- [bkbilly/oralb_ble](https://github.com/bkbilly/oralb_ble)
- [Bluetooth-Devices/oralb-ble](https://github.com/Bluetooth-Devices/oralb-ble)
- [Home Assistant Oral-B integration](https://www.home-assistant.io/integrations/oralb/)
- [MatrixEditor/oralb-io](https://github.com/MatrixEditor/oralb-io)
- [Anrolosia/toothbrush-card](https://github.com/Anrolosia/toothbrush-card)

## Disclaimer

Not affiliated with, endorsed by, or connected to Oral-B or Procter & Gamble.
Protocol behaviour may differ across models and firmware.

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-url]: https://github.com/hacs/integration
[release-badge]: https://img.shields.io/github/v/release/thomasgregg/oralb-ha
[release-url]: https://github.com/thomasgregg/oralb-ha/releases
