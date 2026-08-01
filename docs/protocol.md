# Oral-B iO and iO Sense local protocol reference

This document records the protocol evidence used by Oral-B Live. Runtime
operation is entirely local: Home Assistant does not require an Oral-B
account, cloud token, GraphQL request or internet connection.

The implementation performs only read-only data operations. Charger read
requests require GATT command writes, but it does not change charger display,
lighting, Wi-Fi, update or brush configuration.

## Verified architecture

An iO toothbrush has one BLE client slot. An iO Sense normally owns that slot
during a charger-managed session. The charger exposes a second BLE peripheral
connection and can forward individual reads to the brush:

```text
toothbrush <-- private BLE --> iO Sense <-- local BLE --> Home Assistant
```

This makes the charger a stable local request/response bridge without making
Home Assistant compete for the toothbrush connection. The charger's Wi-Fi is
not involved in this path.

Evidence in this reference is classified as:

| Level | Meaning |
| --- | --- |
| Captured | Observed in a local BLE or network capture |
| Reconstructed | Recovered from the vendor application's protocol model |
| Inferred | Best explanation consistent with captures, but not decrypted or directly exposed |

The test pair was an iO Series 10 and iO Sense charger on firmware `0.3.4`.
The brush returned `36 08 52` from `FF02`: model ID `0x36`, protocol version
`0x08` and firmware revision `0x52`. The physical product identified it as a
Series 10; that marketing model is not encoded by this generic model ID.

## Direct toothbrush BLE

Home Assistant direct mode connects to the toothbrush itself. It is the
highest-rate local path because changing values arrive as GATT notifications
rather than sequential charger requests.

### Connection lifecycle

The tested toothbrush accepts one BLE client. While that slot is held by Home
Assistant, an iO Sense or the phone app, the brush stops advertising. Other
clients cannot discover or connect to it until the slot is released.

When the slot is free, the brush advertises continuously while idle or
charging and reports itself as connectable. A pending connection normally
completes in under a second.

Two captured behaviours shape direct mode:

- A connection established while the brush is docked remains active when the
  brush is picked up and used. State, timer, pressure, mode and zone
  notifications continue through the session.
- The brush can disconnect an idle client approximately 30 seconds after
  activity stops. Oral-B Live reconnects immediately after that callback and
  also retries every 30 seconds while it has no direct connection. A separate
  stale-link watchdog rebuilds a connection that remains present but stops
  delivering activity.

Direct mode deliberately reacquires the brush slot. The charger display and
phone app therefore cannot use the brush at the same time. The connection is
released for sleeping and transport states.

No pairing or bonding is required for the normal read/notify characteristics
used by the integration.

### Toothbrush vendor service

The primary brush service is:

`A0F0FF00-5047-4D53-8208-4F72616C2D42`

| Characteristic | Access | Content |
| --- | --- | --- |
| `FF01` | read | Device MAC in byte-reversed wire order |
| `FF02` | read | Model identifier, protocol version and firmware revision |
| `FF04` | notify, read | Toothbrush state and substate |
| `FF05` | notify, read | Battery percentage, estimated brushing runtime remaining and supported electrical diagnostics |
| `FF06` | notify, read | Button state: none, power or mode |
| `FF07` | notify, read | Brushing mode |
| `FF08` | notify, read | Brushing timer as `[minutes, seconds]`, normally 1 Hz while running |
| `FF09` | notify, read | Current sector/zone and configured sector information |
| `FF0A` | notify, read | Smiley/display face |
| `FF0B` | notify, read | Pressure state and, on protocol 8/9, pressure/motor fields |
| `FF0C` | read, write, notify | Authentication-gated cache; not used |
| `FF0D` | notify, read | Motion and gyroscope data, approximately 30 Hz |
| `FF29` | read | Retained latest-session summary |

The configuration service is:

`A0F0FF20-5047-4D53-8208-4F72616C2D42`

| Characteristic | Access | Content |
| --- | --- | --- |
| `FF21` | read, write, notify | Brush control/configuration channel; not written by Oral-B Live |
| `FF22` | read, write | Brush real-time clock |
| `FF25` | read, write | Available brushing modes |
| `FF26` | read, write | Per-sector pacer times |
| `FF2D` | read, write | Brush-head state, remaining days and brushing seconds |

Service `A0F0FF80-5047-4D53-8208-4F72616C2D42` is the firmware-update channel.
Its `FF81` command and `FF82` payload characteristics are not used.

Although some characteristics permit writes, both integration modes use them
only to obtain data or receive notifications; they do not write device
settings.

### Direct notification stream

At connection setup, Oral-B Live subscribes to:

- `FF04` state;
- `FF07` mode;
- `FF08` timer;
- `FF09` sector/zone;
- `FF0B` pressure;
- optional `FF05` battery and `FF0A` smiley notifications.

It also performs initial reads of `FF02`, `FF05`, `FF08`, `FF0A`, `FF25`,
`FF26` and `FF2D`. These populate identity, battery diagnostics, current timer,
display face, mode availability, target/pacer configuration and brush-head
remainder without waiting for each value to change.

`FF02` and the corresponding advertisement bytes identify the protocol model,
not necessarily the marketing model printed on the brush. In the reconstructed
model map, `0x34` and `0x35` identify iO Series 4 and 5 respectively, while
`0x30`, `0x31`, `0x32` and the captured `0x36` report only the generic iO
Series family.

The length-gated `FF05` battery layout is:

| Offset | Encoding | Content |
| --- | --- | --- |
| `0` | unsigned byte | battery percentage |
| `1..2` | little-endian unsigned 16-bit | estimated brushing runtime remaining on the current charge, in seconds; `0xFFFF` means unavailable |
| `3..4` | little-endian unsigned 16-bit | battery voltage in millivolts |
| `5..6` | little-endian signed 16-bit | battery current in milliamperes; `-1` means unavailable |
| `7` | signed byte | battery temperature in degrees Celsius |

Protocol 6 introduced the remaining-runtime field. Protocol 8 extended the
payload with voltage, signed current and temperature. Older or differently
featured brushes can return a shorter payload, so fields are decoded only when
their bytes are present.

`FF06` is the button characteristic, not pressure. Pressure is `FF0B`; its
first byte is `0` low, `1` normal or `2` high. A captured protocol-8/9 payload
also contains a timestamp, force, motor angle, motor target and identifier.
Oral-B Live exposes the pressure state and, for charger-forwarded values, raw
force as an attribute. A direct `FF06` read during hard brushing returned
`00 00 00 00`; that capture helped confirm that a constant zero there is a
button state, not a failed pressure sensor.

The recognized `FF0A` display-face values are:

| Value | Face |
| ---: | --- |
| `0` | off |
| `1` | standard |
| `2..11` | `special_2` through `special_11` |

Values through `special_10` were confirmed in captured charger-forwarded
reads. Unknown future values remain visible as their raw face number.

`FF08` is elapsed time for the active brushing session. It is unrelated to the
estimated battery runtime carried by `FF05`.

For the observed direct `FF09` representation, the low three bits carry the
sector value. Zero means no sector and `7` represents the configured last
sector. Charger passthrough uses the zero-based representation documented
under [Zone numbering](#zone-numbering). The sector is the brush's configured
pacer prompt, not a spatial measurement of where the brush is in the mouth.
It normally notifies only when the pacer advances at the intervals configured
through `FF26`; a short session can therefore report only one sector.

On the tested direct connection, `FF29` changed within seconds of a completed
session and the latest record was anonymously readable without first issuing
an `FF21` control command. Charger passthrough has the separate post-session
availability constraint described under
[Availability after a session](#availability-after-a-session).

### Toothbrush advertisement

The toothbrush also uses manufacturer ID `0x00DC`. Its 11-byte manufacturer
value is:

| Offset | Content |
| --- | --- |
| `0` | brush protocol version |
| `1` | model identifier |
| `2` | firmware revision |
| `3` | state |
| `4` | pressure/status flags |
| `5..6` | brushing time as `[minutes, seconds]` |
| `7` | brushing mode |
| `8` | sector; low three bits contain the sector value |
| `9` | sector timer |
| `10` | configured number of sectors |

Advertisements provide the passive fallback and are the data source used by
Home Assistant's built-in Oral-B integration. They are unavailable while any
client owns the toothbrush connection slot.

## iO Sense advertisement

The iO Sense advertises Procter & Gamble manufacturer ID `0x00DC`. Its
manufacturer value is 14 bytes:

| Offset | Content |
| --- | --- |
| `0` | charger protocol version |
| `1` | device type; `0xA2` identifies Stargate/iO Sense |
| `2..4` | firmware major, minor and patch |
| `5` | server-mode enum |
| `6..11` | advertised BLE MAC |
| `12` | body colour |
| `13` | connection/status bit field |

The status byte is decoded as:

| Bit | Meaning |
| ---: | --- |
| `0` | Wi-Fi connected |
| `1` | internet connected |
| `2` | cloud connected |
| `3` | brush connected |
| `4` | brush charging |
| `5` | touchpad active |
| `6` | brush paired |
| `7` | demo mode |

Advertisements provide availability and coarse connection state. They do not
contain timer, pressure, zone, score or completed-session data.

## iO Sense GATT transport

The charger service is:

`A0F03E00-5047-4D53-8208-4F72616C2D42`

| Characteristic | Role | Properties |
| --- | --- | --- |
| `A0F03C00` | command header and delimiter | write |
| `A0F03C01` | returned data | read, notify |
| `A0F03C02` | command payload | write |
| `A0F03C03` | command status | read, notify |

Protocol-2 command headers are `[operation, command]`:

| Operation | Byte |
| --- | ---: |
| GET | `0xC0` |
| POST | `0xC1` |
| EXECUTE | `0xC2` |

Returned data reverses those fields:

```text
[command, operation, payload...]
```

Status is `[command, operation, result]`; captured success uses `0x01`.
Payload writes are sent through `C02` and the transaction ends with `E0` on
`C00`.

## Charger command map

The integration implements the useful read-only subset of this reconstructed
command surface:

| ID | Command | Local value |
| ---: | --- | --- |
| `0x03` | device ID | charger identity text |
| `0x14` | Wi-Fi status | disabled/connecting/connected state |
| `0x15` | Wi-Fi RSSI | encoded signal strength, decoded to dBm |
| `0x16` | IoT status | cloud transport state |
| `0x1A` | internet type | Wi-Fi/cellular/Ethernet enum |
| `0x1B` | timezone | configured timezone text |
| `0x1E` | firmware version | semantic version |
| `0x1F` | uptime | seconds |
| `0x20` | free heap | bytes; diagnostic research only |
| `0x25` | hardware version | hardware revision |
| `0x26` | server mode | full/limited/Wi-Fi-only/AWS-only |
| `0x30` | clock brightness | percent |
| `0x31` | clock display mode | 12/24-hour setting |
| `0x33` | brush status | not connected/pre-run/idle/charging/run |
| `0x34` | brush connection policy | allowed/temporarily forbidden/forbidden |
| `0x36` | ring colour | RGB colour |
| `0x37` | brush passthrough | forwarded brush-characteristic operation |
| `0x39` | brush data | paired-brush identity and firmware metadata |
| `0x3A` | session status | inactive/active-running/active-idle |
| `0x3B` | automatic update | enabled/disabled |
| `0x3D` | touchpad status | touch state |
| `0x3F` | date display mode | disabled/month-day/day-month |
| `0x42` | night-light mode | disabled/solid/breathing/rainbow/cool/custom |
| `0x44` | clock text | text currently shown by the charger |
| `0x46` | brush paired | boolean |

Other reconstructed commands include provisioning credentials, Wi-Fi SSID
and security, OTA discovery, low-power mode, night-light schedules, custom
animations, demo mode, debug logging and system maintenance. Oral-B Live does
not request secret material or issue writes, EXECUTE operations, resets,
updates, flash erasure, data clearing, reboots or connection-policy changes.

### `BRUSH_DATA` (`0x39`)

The captured 64-byte value contains paired-brush metadata, including:

- brush MAC and device UUID;
- internal model/type and colour;
- display language;
- brush protocol and firmware/controller metadata.

Despite its name, it is not brushing history. It contains no duration,
pressure distribution, zone times, score or session list. Oral-B Live uses the
brush MAC to associate a charger with the correct toothbrush config entry and
ignores chargers paired to other brushes.

## Read-only brush passthrough (`0x37`)

A characteristic read is framed as three writes:

```text
C00: C1 37
C02: uuid_lsb uuid_msb 01 00
C00: E0
```

`0x01` is the brush-characteristic read operation and `0x00` is its zero-length
request body. For example, `08 FF 01 00` reads `FF08`.

A successful command status is:

```text
37 C1 01
```

The returned value is:

```text
37 C1 uuid_lsb uuid_msb operation success length data...
```

The tested firmware rejects a request containing several records but accepts
the same records as sequential commands. The implementation therefore sends
exactly one outstanding request and matches each response by short UUID.

Passthrough succeeds only while the charger is connected to the brush. It does
not force the charger to establish that private connection.

## Confirmed brush characteristics through the charger

| UUID | Content | Captured result |
| --- | --- | --- |
| `FF04` | state | running state confirmed |
| `FF05` | battery diagnostics | percentage, estimated brushing runtime remaining on the current charge, voltage, signed current and temperature confirmed |
| `FF07` | brushing mode | live mode confirmed |
| `FF08` | timer | `[minutes, seconds]` confirmed live |
| `FF09` | zone | zero-based zone ID plus configured count confirmed live |
| `FF0A` | brush display face/smiley | values through `special_10` confirmed |
| `FF0B` | pressure/motor | pressure state, timestamp, force and motor fields confirmed live |
| `FF0D` | motion | motion and gyroscope snapshots confirmed; not exposed as an HA entity |
| `FF22` | brush real-time clock | confirmed |
| `FF25` | available modes | confirmed |
| `FF26` | per-zone pacer configuration | confirmed |
| `FF29` | retained latest-session summary | confirmed |
| `FF2D` | brush-head/refill remainder | days and brushing seconds confirmed |

All 13 reads completed sequentially in one active charger-managed session.
That proves feature breadth, but the bridge is polled request/response rather
than a notification-rate stream.

### Pressure payload

The first `FF0B` byte is the pressure state:

| Value | State |
| ---: | --- |
| `0` | low |
| `1` | normal |
| `2` | high |

The integration also exposes the captured force word as an attribute. Motor
and motion fields are decoded by the research tooling but are not separate HA
entities.

### Zone numbering

Direct toothbrush notifications and charger passthrough use different
presentation rules in the observed payloads. Charger `FF09` zone IDs are
zero-based: raw `0` is `sector_1`, raw `3` is `sector_4`, and `0xFF` denotes
the configured last sector. `0xF0` means that no sector is defined.

## Sustained polling benchmark

One real 150.065-second session requested raw timer, zone and pressure values
sequentially, with cycle starts paced to 1 Hz:

| Measurement | Result |
| --- | ---: |
| Complete cycles | 148 / 148 |
| Successful requests | 444 / 444 |
| Request errors | 0 |
| Full-cycle p50 | 992.603 ms |
| Full-cycle p95 | 1140.772 ms |
| Full-cycle maximum | 1347.251 ms |
| Maximum individual-field gap | 1347.987 ms |
| Timer p95 | 450.200 ms |
| Zone p95 | 450.361 ms |
| Pressure p95 | 420.541 ms |

The data remained fresh: timer advanced from 33 to 182 seconds, the maximum
observed timer step was two seconds, multiple zones appeared, all three
pressure states appeared, and force ranged from 720 to 3175.

Three raw fields do not all fit strictly inside every one-second cycle. The
derived two-read timings are:

| Tick | p50 | p95 | Maximum |
| --- | ---: | ---: | ---: |
| pressure + timer | 655.522 ms | 779.035 ms | 894.694 ms |
| pressure + zone | 662.129 ms | 808.012 ms | 899.585 ms |

Oral-B Live therefore reads pressure every one-second tick and alternates
timer and zone as the second read. The displayed timer advances locally
between the authoritative two-second timer anchors. Charger-native session
state is checked periodically to provide an explicit stop signal.

## Retained session summary (`FF29`)

The verified protocol-7/8 payload is 21 bytes, little-endian:

| Offset | Field | Encoding |
| --- | --- | --- |
| `0..3` | session start on brush clock | seconds |
| `4..5` low 13 bits | session ID | integer |
| `4..5` high 3 bits | user ID | integer |
| `6..7` low 13 bits | target duration | seconds |
| `6..7` high 3 bits | number of sectors | integer |
| `8..9` | duration | seconds |
| `10..11` | high-pressure time | 100 ms units |
| `12..13` | low-pressure time | 100 ms units |
| `14` | average pressure | 100 mN units |
| `15` | maximum pressure | 100 mN units |
| `16` | high-pressure event count | integer |
| `17` | low-pressure event count | integer |
| `18` | power-on event count | integer |
| `19` | brushing mode | mode ID |
| `20` | battery at session end | percent |

The brush clock can drift. The wall-clock start is calculated relative to an
`FF22` value read in the same connection:

```text
wall_start = now - (brush_rtc - session_timestamp)
```

The live stream creates the HA session immediately. A later `FF29` record is
matched to that session and refines it without increasing `sessions_today`
twice.

`FF29` belongs to the brush and retains its latest summary. It is not a dump
of a charger queue and does not contain per-second pressure distribution or
per-zone pressure time.

### Availability after a session

When the charger reports inactive/not connected, immediate passthrough reads
are rejected. `BRUSH_DATA` remains cached but does not reconnect the brush.
The completed `FF29` becomes readable on a subsequent charger-managed brush
connection. Oral-B Live retains its locally reconstructed session until that
authoritative summary can be reconciled.

## Charger-native diagnostics

Confirmed local charger reads include:

- firmware and hardware versions, uptime and free heap;
- Wi-Fi, internet and cloud transport state plus RSSI;
- current clock text, timezone and 12/24-hour mode;
- date-display format, clock brightness, night-light mode and ring colour;
- update settings, touchpad state and brush connection policy;
- paired, connected, charging, brush-status and session-status values.

`CLOCK_TEXT` returns the displayed time. `DATE_SHOW_MODE` is the display-format
setting; no read command returning a complete calendar date has been found.

## Internal queue and Wi-Fi findings

The charger can retain richer session objects for its normal upload path, but
no local command for enumerating that durable queue was found.

A controlled internet-blocked session established that:

- the charger continues normal retry/backoff behaviour while offline;
- its working-data-size value is a fixed buffer capacity, not queue occupancy;
- free-heap changes reflect the live working set, not durable session count;
- no completed-session object appears on the BLE notification channel;
- the brush's `FF29` record remains recoverable locally on its next managed
  connection.

On the LAN, the charger behaves as an outbound client. A targeted scan found
no listening HTTP, HTTPS, SSH, MQTT or common embedded-management service.
Captured traffic was outbound encrypted TLS/MQTT-style traffic plus normal
DNS/NTP maintenance. There was no charger-to-LAN application stream or inbound
local API.

The normal vendor route appears to use mutual TLS for outbound upload and an
authenticated cloud API for app retrieval. Oral-B Live deliberately does not
intercept, redirect or depend on that route.

Public charger firmware containers are encrypted, have near-uniform entropy
and expose no useful plaintext protocol strings. Simple key derivations from
public discovery metadata did not produce a valid image. Firmware decryption,
hardware extraction, MQTT redirection and certificate replacement are outside
the integration's runtime design.

## Safety boundary

The charger firmware is timing-sensitive. A controlled same-value brightness
write showed that a success status can acknowledge transport before a setting
is applied as intended. The value was restored during research, and write
tooling was removed.

For that reason Oral-B Live:

- emits GET commands and read-only brush passthrough records only;
- serialises every request and waits for the matching response;
- never sends charger configuration or brush-configuration writes;
- never requests or modifies provisioning credentials;
- never performs reset, update, erase, reboot, clear-data or low-power
  commands.

## Implementation mapping

| Component | Responsibility |
| --- | --- |
| `charger_protocol.py` | pure advertisement, native packet and passthrough decoders/builders |
| `charger.py` | automatic pairing, connection lifecycle, serial request scheduler and charger diagnostics |
| `protocol.py` | pure toothbrush payload decoders including exact `FF29` and zero-based charger zones |
| `coordinator.py` | source selection, live state, timer extrapolation, session tracking and reconciliation |
| `sensor.py` | toothbrush entities plus a separate matched iO Sense device |
| `tests/test_protocol.py` | captured-packet regression tests with no Home Assistant dependency |
