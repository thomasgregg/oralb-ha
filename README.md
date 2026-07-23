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
- [Connection modes](#connection-modes)
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

**Active layer.** Opens a GATT connection — through any connectable
Bluetooth path, including ESPHome Bluetooth proxies with active
connections enabled — either held continuously for live 1 Hz data or
opened briefly after each session, depending on the configured
[connection mode](#connection-modes).

**One connection slot.** The brush accepts exactly one BLE client at a
time — and it stops advertising entirely while that slot is taken.
Whoever holds the slot gets everything (live notifications, the session
record); everyone else gets silence. This was established empirically
in July 2026 on an iO Series 10 with an iO Sense charger: with Home
Assistant holding the connection, live data streamed into Home
Assistant perfectly — and the iO Sense display stayed dark. With the
charger holding it, the display worked — and the brush was
radio-silent to everyone else until the session was over. This is a
firmware property of the brush, not something any integration can work
around, and it forces the trade-off described in the next section.

## Connection modes

Three parties want the brush's single connection slot: the iO Sense
charger (for its lights and countdown display), the Oral-B phone app,
and Home Assistant. Only one of them can have it during a brushing
session. Because no software can remove that constraint, who wins is a
configuration option: *Settings → Devices & Services → Oral-B Live →
Configure*. Switching takes effect immediately.

### Charger priority (default)

Home Assistant never competes for the slot during brushing. The charger
(or the app) connects as designed, so the iO Sense lights and timer
behave exactly as they do out of the box. If the brush broadcasts
running and quiet states, Home Assistant records the session
immediately from those passive advertisements. Duration falls back to
elapsed wall time when the advertised timer remains at zero. A guarded
fallback also handles firmware that remains in `selection_menu` during
a short motor session without ever advertising `running`.

After the session, Home Assistant also makes short attempts to read the
brush's own **last-session record** (see
[Protocol notes](#last-session-record-ff29)), battery level and state.
When that succeeds, its authoritative timestamp, duration and mode
refine the passively recorded session without counting it twice. When
the charger continues to own the brush's only connection, the passive
record remains available instead of losing the session.

Back-to-back sessions are tracked independently. If another session
starts while the previous brush-history read is settling or retrying,
the newer session remains queued for its own read instead of being
cleared with the older one.

What you give up: live timer, pressure and quadrant updates depend on
what the brush firmware exposes in advertisements. They are not
guaranteed in charger-priority mode because Home Assistant deliberately
does not take the connection slot.

What you keep: a complete brushing log (start time, duration, mode),
battery tracking, and a fully functional charger and phone app.

If several sessions happen while Home Assistant is down or out of
range, only the most recent one is recovered — the record on the brush
holds a single session.

### Live

The original behaviour (v0.4 and earlier). Home Assistant seizes the
slot whenever it is free — most reliably while the brush is docked —
and holds it. All entities update live at 1 Hz during brushing: timer,
pressure, quadrant, mode. Sessions are recorded from the live stream.

What you give up: while Home Assistant holds the slot, the iO Sense
display does not work and the phone app cannot sync. The brush also
drops idle clients after about 30 seconds of its own accord, so the
integration reconnects continuously in the background — this is
normal and visible as brief `live_connection` flaps while docked.

## How this differs from the official integration

Home Assistant ships an `oralb` integration. On most brushes it is the
better choice: fully passive, no Bluetooth connection slots, no custom
component. If your brush still broadcasts while you brush, stop here and
use it.

On an iO Series 10 with mid-2026 firmware, it no longer does. Here is
what the official integration actually reports during a session on this
brush:

- **The timer stays at 0.** It does not count while you brush. A second
  or two after you switch off, it jumps straight to the final duration
  — "you brushed for 96 seconds" — with nothing in between.
- **Pressure is frozen.** It keeps whatever value it read before the
  session started, however hard you press. The brush's own red ring
  lights up; Home Assistant never hears about it.
- **The quadrant never advances.** It holds its previous value for the
  whole session, so no quadrant progress is visible.
- **Whole sessions can vanish.** The end-of-session summary is a single
  advertisement. Miss it — weak signal, a scanner that only listens 10%
  of the time, the phone app taking the connection — and the session is
  gone entirely, with no record that you brushed at all.
- **Updates can stop until you reload.** The battery sensor polls over a
  GATT connection. When those attempts fail against this firmware,
  passive updates stall and the entities freeze at their last value
  until the config entry is reloaded
  ([home-assistant/core#177039](https://github.com/home-assistant/core/issues/177039)).

None of that is a bug in the official integration. The brush changed:
recent iO firmware stops broadcasting during a session and emits only a
post-session summary. A passive listener has nothing to listen to. The
data still exists — it moved to GATT notifications, which require a
connection.

This integration keeps the passive listening and adds that connection.

The "Oral-B Live" column below describes **live** mode; in
charger-priority mode the in-session rows are traded for a
post-session sync — see [Connection modes](#connection-modes).

| | Official `oralb` | Oral-B Live |
| --- | --- | --- |
| Data source | Advertisements only | Advertisements plus GATT (notifications or post-session reads) |
| Connection | Never connects (battery uses a poll) | Configurable: held live connection, or a few seconds after each session |
| Live timer on recent iO firmware | Stays at 0, jumps at the end | Counts up at 1 Hz while brushing (live mode) |
| Live pressure during a session | Frozen at its pre-session value | `low` / `normal` / `high`, live from `ff0b` (live mode) |
| Live quadrant during a session | Frozen | Advances as the brush paces them (live mode) |
| A missed summary advertisement | Session lost entirely | Session recorded from observed start/end states, live stream, or the brush's own `ff29` record |
| Brushing log | None | Last session, duration, sessions today, kept across restarts |
| Number of sectors | From advertisement | Read from the brush's quadrant configuration |
| Battery | Active poll (can stall updates on this firmware) | Read on connect / each sync |
| Cost | None | One Bluetooth connection slot (held, or briefly per sync) |
| iO Sense charger display | Works | Works in charger-priority mode; disabled in live mode |
| Competes with the phone app | No | Live mode: yes. Charger priority: no |

Practical trade-offs worth knowing before switching:

- The brush accepts one client at a time. In live mode, while this
  integration is connected, the Oral-B app and the iO Sense charger
  cannot connect at all — this is why charger-priority mode exists and
  is the default.
- Holding a connection (live mode) uses more brush battery than
  passive listening or brief per-session syncs.
- Entity IDs differ from the official integration, so dashboard cards
  need repointing after switching.
- Everything here was worked out on one brush. On other models the
  official integration may already be enough.

## Entities

Entity structure mirrors the official `oralb` integration, so existing
dashboards and toothbrush cards keep working.

| Entity | Description |
| --- | --- |
| Toothbrush state | `idle`, `running`, `charging`, `selection_menu`, `session_summary`, `post_brushing_summary`, ... |
| Time | Session duration in seconds. Updates at 1 Hz while connected (live mode). |
| Sector | Current quadrant (`sector_0` ... `sector_3`, or `no_sector`) |
| Number of sectors | Read from the brush's quadrant time configuration |
| Mode | `daily_clean`, `sensitive`, `gum_care`, `whiten`, `intense`, ... |
| Pressure | `low` / `normal` / `high`, live while connected |
| Battery | Percentage, read on connect / each sync |
| Last session | Timestamp of the last completed session |
| Last session duration | Length of that session, in seconds |
| Sessions today | Number of sessions today, resets at midnight |

In charger-priority mode the in-session entities (time, pressure,
sector) update only when advertisements expose them; in live mode they
stream at 1 Hz. See
[Connection modes](#connection-modes).

The state entity also exposes `live_connection`, `connection_mode`,
`rssi`, `state_raw` and `mode_raw` as attributes, which is useful when
diagnosing how a session was captured.

### Brushing log

When a session ends, **Last session** records the start time with
`duration_seconds`, `mode`, `quadrants_covered` and
`high_pressure_events` as attributes. In live mode these come from the
live stream. In charger-priority mode passive advertisements provide an
immediate fallback; a later brush history read refines its start time,
duration and mode when the connection is available. Because it is a
proper timestamp sensor, Home Assistant's recorder keeps the history
automatically: a history graph on **Last session duration** is a
complete brushing log that accumulates from installation onwards.

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
4. Pick a **connection mode** via *Configure* on the integration entry
   — see [Connection modes](#connection-modes). The default, charger
   priority, keeps the iO Sense charger display and the phone app
   working; switch to live mode if you want in-session 1 Hz data in
   Home Assistant instead.

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

**One client at a time.** The brush's single BLE connection slot is the
defining constraint of this integration; see
[Connection modes](#connection-modes) for why the trade-off cannot be
engineered away. In live mode the charger display and phone app lose;
in charger-priority mode Home Assistant deliberately loses during the
session and catches up from the brush's own record afterwards. There
is no configuration in which both stream live simultaneously.

**Charger-priority mode recovers one session at a time.** The brush's
last-session record holds exactly one session. If Home Assistant is
down or out of range across several sessions, only the most recent is
recovered on the next sync.

**Quadrant changes are paced by the brush.** The brush emits a quadrant
notification only when the sector actually changes, typically every 30
seconds depending on its configuration. The quadrant is the brush's own
pacer telling you where to brush next, not a detection of where the
brush actually is. Short sessions may show only one quadrant.

**Full stored history is not available.** The brush exposes its most
recent session in `ff29` (used by charger-priority mode), but the
deeper multi-session history the phone app shows requires control-
channel commands and is deliberately not attempted; see
[Protocol notes](#protocol-notes).

## Protocol notes

Findings from GATT reconnaissance of an Oral-B iO Series 10 (model ID
`0x36`, protocol version 8), July 2026, cross-checked against
MatrixEditor/oralb-io. Documented here so others do not have to repeat
the work.

### One connection slot, and what it does to advertising

The brush accepts a single BLE client. While that slot is held — by
the iO Sense charger, the phone app, or Home Assistant — the brush
**stops advertising entirely**, so other clients cannot even discover
it, let alone connect (connection attempts fail with device-not-found
or time out). When the slot is free the brush advertises continuously
in idle/charging states with `kCBAdvDataIsConnectable` set, and a
pending connection completes in well under a second.

Two behaviours follow that are worth knowing:

- **The brush sheds idle clients after ~30 seconds.** A client that is
  connected but receiving no notifications (brush idle on the charger)
  is disconnected by the brush almost exactly 30 s after activity
  stops. Reconnection is immediate. A held "permanent" connection is
  therefore really a connect/drop/reconnect cycle while docked.
- **A connection made while docked survives a session.** If a client
  wins the slot while the brush is charging, picking the brush up and
  brushing does not evict it: the client receives the full state
  transitions, the 1 Hz timer, ~30 Hz pressure and quadrant
  notifications for the whole session. This is what live mode does.

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
| `ff29` | read | **Last completed session record** (see below) |

Configuration service `a0f0ff20-...`:

| Characteristic | Access | Content |
| --- | --- | --- |
| `ff21` | read, write, notify | Control channel (commands) |
| `ff22` | read, write | Real-time clock, seconds on the brush's own epoch |
| `ff25` | read, write | Available brushing modes |
| `ff26` | read, write | Quadrant times, seconds per sector |

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

### Last-session record (`ff29`)

An earlier revision of these notes claimed `ff29` never changed and
needed control-channel commands to fill. That was wrong: on this
firmware it updates within seconds of every completed session, and
anonymous clients can read it freely. Observed layout, 23 bytes,
little-endian:

| Offset | Example | Content |
| --- | --- | --- |
| 0–3 | `c2 1e f5 31` | Session start, seconds on the brush clock |
| 4–5 | `49 01` | Session counter (tentative; 329, appears to increment per session) |
| 6–7 | `78 00` | Target duration, seconds (120) |
| 8–9 | `3c 00` | **Session duration, seconds** |
| 10–11 | `14 00` | Pressure-related count (tentative) |
| 12–13 | `1e 00` | Per-quadrant time, seconds (matches `ff26`) |
| 14–18 | `12 1f 0a 01 01` | Unknown |
| 19 | `04` | Brushing mode |
| 20 | `5f` | Battery percent at session end |
| 21–22 | `00 00` | Unknown |

The example is a real 60-second session in `intense` mode at 95%
battery. Verification of the timestamp field: `ff22` (the RTC, same
epoch) read 63 seconds after the session started returned exactly the
record's timestamp plus 63.

**The brush clock drifts.** On the tested unit it ran about eight days
ahead of wall time — it is presumably only disciplined when the phone
app syncs it. Do not convert the record timestamp to absolute time
directly. Instead read `ff22` in the same connection and compute
`wall_start = now − (rtc − record_timestamp)`, which cancels the drift
entirely. This is what charger-priority mode does; the raw timestamp
is used only to deduplicate records across syncs and restarts.

Bytes 4–5, 10–11 and 14–18 are provisional readings from a small
number of sessions on one device; corrections welcome.

The deeper multi-session history shown in the phone app is a different
mechanism: it needs writes to the `ff21` control channel, whose command
set also contains a factory reset, so it is deliberately not attempted
here. `ff0c` remains authentication-gated.

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

**No live updates during brushing.** In charger-priority mode (the
default) this is by design — the brush is silent while the charger
holds the connection, and the session syncs about a minute after you
finish. If you want in-session data in Home Assistant, switch the
entry to live mode via *Configure*, accepting that the iO Sense
display will stop working. In live mode, check the `live_connection`
attribute on the state entity: if it is `false`, the connection was
not won — disable Bluetooth on the phone or unpair the Oral-B app and
retry.

**Charger display or phone app stopped working.** The entry is in live
mode and Home Assistant holds the brush's only connection slot. Switch
to charger-priority mode via *Configure*.

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
