# Oral-B Live

[![hacs][hacs-badge]][hacs-url]
[![release][release-badge]][release-url]

**Live brushing data for Oral-B iO toothbrushes in Home Assistant.**

Recent iO firmware stopped broadcasting live session data over Bluetooth
advertisements. This integration retrieves it over a GATT connection
instead, restoring the real-time brushing timer, quadrant tracking and
mode that passive listening can no longer provide.

---

## Contents

- [The problem](#the-problem)
- [How it works](#how-it-works)
- [Entities](#entities)
- [Installation](#installation)
- [Configuration](#configuration)
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
characteristics. The 1 Hz brushing timer, quadrant and mode flow
straight into the entities.

**Polite by design.** The brush accepts a single client. If the
connection cannot be won, typically because the Oral-B phone app or an
iO Sense charger holds it, the integration falls back to passive mode
rather than fighting for the device. When the brush returns to charging
or sleep, the connection is released promptly so other clients can sync.

## Entities

Entity structure mirrors the official `oralb` integration, so existing
dashboards and toothbrush cards keep working.

| Entity | Description |
| --- | --- |
| Toothbrush state | `idle`, `running`, `charging`, `selection_menu`, `session_summary`, `post_brushing_summary`, ... |
| Time | Session duration in seconds. Updates at 1 Hz while connected. |
| Sector | Current quadrant (`sector_0` ... `sector_3`, or `no_sector`) |
| Number of sectors | Read from the brush's pacer configuration |
| Mode | `daily_clean`, `sensitive`, `gum_care`, `whiten`, `intense`, ... |
| Pressure | `normal` / `high` — see [Known limitations](#known-limitations) |
| Battery | Percentage, read on connect |

The state entity also exposes `live_connection`, `rssi`, `state_raw` and
`mode_raw` as attributes, which is useful when diagnosing whether a
session was captured actively or passively.

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
   device causes duplicate entities and failed connections.
2. Wake the brush by pressing its button. Oral-B Live discovers it
   automatically; confirm the discovered device.
3. Alternatively add it manually via *Settings, Devices & Services, Add
   integration, Oral-B Live*.

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

**Pressure is not available during brushing.** This is a firmware
limitation, not a bug in this integration — see
[Protocol notes](#protocol-notes) for the evidence. Pressure updates
from advertisements while the brush is idle or charging, but stays
frozen during an active session.

**Quadrant changes are paced by the brush.** The brush emits a quadrant
notification only when the sector actually changes, typically every 30
seconds depending on its pacer configuration. Short sessions may show
only one quadrant. There is also a two to five second connection
establishment delay at the start of a session, during which early
transitions can be missed.

**The phone app wins connection races.** With Bluetooth enabled and the
Oral-B app paired, the app may claim the brush first. The integration
then reports passive data only, which on recent firmware can mean just
the end-of-session summary.

**Session history is not implemented.** The brush stores completed
sessions and exposes them through a command channel (`ff81`/`ff82`) that
this integration does not yet speak.

## Protocol notes

Findings from GATT reconnaissance of an iO-series brush (model bytes
`36 08 52`), July 2026. Documented here so others do not have to repeat
the work.

### Vendor service `a0f0ff00-5047-4d53-8208-4f72616c2d42`

| Characteristic | Access | Content |
| --- | --- | --- |
| `ff01` | read | Device MAC, byte-reversed |
| `ff02` | read | Model identifier |
| `ff04` | notify, read | Toothbrush state, `[state, 0]` |
| `ff05` | notify, read | Status blob; byte 0 is battery percentage |
| `ff06` | notify, read | Nominally pressure — see below |
| `ff07` | notify, read | Brushing mode |
| `ff08` | notify, read | Brushing time as `[minutes, seconds]`, 1 Hz while running |
| `ff09` | notify, read | Current quadrant |
| `ff0b`, `ff0d` | notify | Motion telemetry, roughly 30 Hz |
| `ff26` | read, write | Pacer configuration, seconds per sector |

No pairing or bonding is required for these. Anonymous connections are
accepted.

### Pressure: tested and ruled out

Pressure could not be obtained over a connection on this firmware. The
evidence:

- `ff06`, the characteristic that would carry it, reads a constant
  `00 00 00 00` and never notified across multiple sessions.
- A dedicated diagnostic polled nine candidate characteristics three
  times per second for 45 seconds across three marked high-pressure
  intervals: 720 samples, no byte separating high from normal pressure.
- `ff04`, `ff07`, `ff0a`, `ff10`, `ff21` and `ff2d` were entirely
  static.
- `ff05` bytes 3, 10 and 14 shift on average under pressure, but their
  distributions overlap heavily. These are motion-derived and not a
  usable pressure signal.

The Oral-B app and the iO Sense charger do display live pressure, which
indicates the data is gated behind a handshake rather than absent.
Earlier work by Ruben Faelens found that notifications only began after
the command sequence used by the official app was replicated, obtained
by decompiling it. The unused write pipes in the `a0f0ff80` service are
the likely channel. Implementing that would require reverse-engineering
the current app and is out of scope for now.

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

**Unknown states or modes.** Unmapped values appear as
`unknown_state_<n>` and `mode_<n>`. Please open an issue with the raw
value and what the brush was doing, and it will be added.

## Credits

- [bkbilly/oralb_ble](https://github.com/bkbilly/oralb_ble) — pioneered
  the active-connection approach and the original characteristic map.
- [Bluetooth-Devices/oralb-ble](https://github.com/Bluetooth-Devices/oralb-ble)
  and the official
  [oralb integration](https://www.home-assistant.io/integrations/oralb/).
- Ruben Faelens, for documenting the app handshake problem.

## Disclaimer

Not affiliated with, endorsed by, or connected to Oral-B or Procter &
Gamble. Protocol details were obtained by observing a single iO-series
device; other models may differ.

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-url]: https://github.com/hacs/integration
[release-badge]: https://img.shields.io/github/v/release/thomasgregg/oralb-ha?display_name=tag&sort=semver
[release-url]: https://github.com/thomasgregg/oralb-ha/releases

