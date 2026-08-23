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
| `FF09` | notify, read | Sequential pacer sector, elapsed interval seconds and configured sector count |
| `FF0A` | notify, read | Smiley/display face |
| `FF0B` | notify, read | Pressure state and, on protocol 8/9, pressure/motor fields |
| `FF0C` | read, write, notify | Authentication-gated cache; not used |
| `FF0D` | notify, read | Motion and gyroscope data, approximately 30 Hz |
| `FF0E` | notify | Configurable batched motion/gyroscope dashboard stream |
| `FF29` | read | Retained latest-session summary |

The configuration service is:

`A0F0FF20-5047-4D53-8208-4F72616C2D42`

| Characteristic | Access | Content |
| --- | --- | --- |
| `FF21` | read, write, notify | Brush control/configuration channel; not written by Oral-B Live |
| `FF22` | read, write | Brush real-time clock |
| `FF25` | read, write | Available brushing modes |
| `FF26` | read, write | Per-sector pacer times |
| `FF2B` | read, write | Three handle SmartRing LED drive levels plus one uninterpreted byte; read only by Oral-B Live |
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
- `FF09` pacer sector;
- `FF0B` pressure;
- optional `FF05` battery and `FF0A` smiley notifications.

It also performs initial reads of `FF02`, `FF05`, `FF08`, `FF0A`, `FF0B`,
`FF25`, `FF26`, `FF2B` and `FF2D`. These populate identity, battery
diagnostics, current timer and pressure, display face, mode availability,
target/pacer configuration, SmartRing LED drive levels and brush-head remainder
without waiting for each value to change.

When a directly connected session ends, Oral-B Live also reads `FF0A`
immediately with a few short retries. The optional notification is not reliable
on every observed session. These reads retain the existing brush connection;
the integration does not disconnect to force an advertisement and risk losing
the brush's single connection slot.

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
Oral-B Live exposes the pressure state and, when the longer payload is
available, raw force as an attribute for both direct and charger-forwarded
`FF0B` values. A direct `FF06` read during hard brushing returned `00 00 00 00`;
that capture helped confirm that a constant zero there is a button state, not a
failed pressure sensor.

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

For the observed `FF09` representation, direct notifications and charger
passthrough both carry the zero-based pacer-sector value documented under
[Pacer numbering](#pacer-numbering). The sector is the brush's configured pacer
prompt, not a spatial measurement of where the brush is in the mouth. It
normally notifies only when the pacer advances at the intervals configured
through `FF26`; a short session can therefore report only one sector.

### Motion data and mouth-position inference

`FF0D` contains raw inertial samples, not a zone identifier. The normal
20-byte form carries four samples of `[uint16 timestamp, int8 x, int8 y,
int8 z]`. The captured Comino form carries two timestamped records with three
additional signed gyroscope axes per record and the marker bytes `10 80` at
offsets 18 and 19. In both the Comino `FF0D` form and `FF0E`, each record is
ordered as timestamp, gyroscope X/Y/Z, then motion X/Y/Z. `FF0E` is the
configurable direct dashboard stream used for batches of these calibrated
features.

The reconstructed direct-stream setup is a write to `FF21` followed by
notifications from `FF0E`:

```text
38 SESSION_HI SESSION_LO DIVIDER 00
```

The session identifier is big-endian and valid from `1` through `65535`.
Observed divider values are `0` for the full rate, `2` for half rate and `4`
for quarter rate. `38 00 00` cancels the stream. An `FF0E` notification is
`[status, record_count, records...]`, where each record is eight bytes:

```text
uint16_le timestamp, int8 gyro_x, int8 gyro_y, int8 gyro_z,
int8 motion_x, int8 motion_y, int8 motion_z
```

Reconstructed status values are `F0` invalid session, `01` first package,
`02` packages pending and `08` last package. These writable stream-control
details are retained for research only. The shipped integration does not start
the dashboard stream or write `FF21`.

The vendor app feeds 26-sample windows into its Comino GRU3/GRU6 classifier
and maps each result to one of 20 detailed labels, including upper/lower,
left/centre/right, inside/outside/onside and out of mouth. Local validation
reproduced the application's feature order, scaling, normalization, recurrent
topology, per-sample argmax and 26-sample majority vote.

Charger passthrough cannot forward `FF0E` notifications, but repeated `FF0D`
reads return two timestamped records newest-first. Interpolating the measured
gap between consecutive snapshots reconstructs a 25 Hz input timeline and
produced approximately one classified result per second in local testing. This
is validation evidence, not a claim that interpolated samples contain the same
information as a continuous direct stream.

The vendor model assets are proprietary and are not distributable with the
integration. Mouth-position inference therefore remains protocol research and
is not exposed as a Home Assistant entity. The public `sector` entity remains
`FF09`'s timed pacer, matching Home Assistant's built-in Oral-B integration and
Toothbrush Card. It must not be relabelled as physical mouth position.

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
| `8` | sector in bits 0–2; display face in bits 3–5 |
| `9` | seconds elapsed in the current sector |
| `10` | configured number of sectors |

Advertisements provide the passive fallback and are the data source used by
Home Assistant's built-in Oral-B integration. They are unavailable while any
client owns the toothbrush connection slot.

The display face is decoded as `(payload[8] & 0x38) >> 3` and uses the same
numbering as `FF0A`: `0` is `off`, `1` is `standard` and `2..7` are
`special_2` through `special_7`. The three-bit advertisement field cannot
represent the additional values available through FF0A. The face remains
useful outside the running state because the brush advertises its session
result after brushing, while the low sector bits are decoded independently.

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
| `0x36` | charger ring colour | RGB colour for the iO Sense ring/night light; separate from the handle's `FF2B` SmartRing colour |
| `0x37` | brush passthrough | forwarded brush-characteristic operation |
| `0x39` | brush data | paired-brush identity and firmware metadata |
| `0x3A` | session status | inactive/active-running/active-idle |
| `0x3B` | automatic update | enabled/disabled |
| `0x3D` | touchpad status | touch state |
| `0x3F` | date display mode | disabled/month-day/day-month |
| `0x42` | night-light mode | disabled/solid/breathing/rainbow/cool/custom |
| `0x44` | clock text | text currently shown by the charger |
| `0x46` | brush paired | boolean |

On the tested firmware, `SESSION_STATUS` sometimes remained `inactive` for an
entire genuine 77-second session while `BRUSH_STATUS` reported `pre_run`.
`BRUSH_STATUS` is therefore the live-session authority: `pre_run` and `run`
keep forwarding active, while `idle`, `charging` and `not_connected` end it.
The native session status remains a fallback when brush status is unavailable.
Selection-menu/pre-run observations remain provisional until the brush timer
advances or an explicit running state confirms real brushing. Static retained
timer values, menu visits and docking blips are discarded regardless of length,
so they cannot replace the latest real session. A ten-second elapsed-time
minimum remains as a secondary guard when a confirmed session has no usable
brush-timer duration.

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
- brush protocol, software, hardware, bootloader, media-content, memory-map,
  information-sector and second-controller versions.

After the charger's two-byte command header has been removed, the verified
payload offsets used by the integration are:

| Offset | Field |
| ---: | --- |
| `16` | model identifier |
| `39` | brush protocol version |
| `40` | software version |
| `41` | hardware version |
| `43` | bootloader version |
| `46` | media-content version |
| `47` | hardware-configuration version |
| `49` | memory-map version |
| `52` | information-sector version |
| `59` | second-controller version |

For an iO/Sonos brush, the Android app formats the user-visible handle firmware
as zero-padded `second-controller.software.media-content`. The captured values
`0`, `82` and `26` therefore appear as `00.82.26`. This is the app's composite
handle version, not a semantic-version interpretation of `FF02`'s single
software byte.

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
| `FF09` | pacer sector | zero-based interval ID, elapsed sector timer and configured count confirmed live |
| `FF0A` | brush display face/smiley | values through `special_10` confirmed |
| `FF0B` | pressure/motor | pressure state, timestamp, force and motor fields confirmed live |
| `FF0D` | motion | timestamped motion and gyroscope snapshots confirmed; local research tooling demonstrated the inference pipeline described above |
| `FF22` | brush real-time clock | confirmed |
| `FF25` | available modes | confirmed |
| `FF26` | per-sector pacer configuration | confirmed |
| `FF29` | retained latest-session summary | confirmed |
| `FF2B` | configured handle SmartRing LED drive levels | raw `#9BFF00` with fourth byte `0` confirmed post-session |
| `FF2D` | brush-head/refill remainder | days and brushing seconds confirmed |

The original 13-characteristic benchmark completed all reads sequentially in
one active charger-managed session. `FF2B` was confirmed later in a separate
post-session hardware test. Together they prove feature breadth, but the bridge
is polled request/response rather than a notification-rate stream.

In production the charger bridge reads `FF05` at session start and then every
30 seconds in place of one alternating timer/pacer request. When native session
state first changes to idle, `FF05` is also the first immediate passthrough
read, before the charger disconnect delay. This matters because tested charger
firmware stops forwarding brush reads shortly after it releases the private
brush connection. Home Assistant retains the last valid percentage across
restarts because a quiet, disconnected brush cannot provide a fresh `FF05`.

The production post-session sequence reads the transient `FF0A` result
immediately after the first `FF05` battery read, then attempts `FF2B` third.
Giving `FF0A` second priority improves result-face capture before the handle
display changes. Hardware testing confirmed that the iO Sense can forward the
handle SmartRing LED drive levels, but also showed that the request can miss the
charger's short forwarding window when left behind the slower session and
diagnostic reads or when the handle is docked. Keeping `FF2B` immediately after
`FF0A` preserves the toothbrush entity's raw value without adding another
request to the timing-sensitive one-second live loop.

### SmartRing LED drive levels

The first three `FF2B` bytes are the drive levels for the SmartRing's red,
green and blue LEDs. They are not screen-calibrated RGB values: unequal channel
levels are required to make the physical ring appear white. Oral-B Live keeps
the exact three device bytes as the sensor's `#RRGGBB` state and exposes the
fourth byte separately without assigning it a meaning. It does not apply a
display conversion.

Two tested iO handles returned the same drive levels for all six named colours:

| Name shown on handle | Raw `FF2B` | Display colour after measured correction |
| --- | --- | --- |
| White | `#44CF63` | `#FFFFFF` |
| Yellow | `#80FF00` | `#FFFF00` |
| Orange | `#FC7000` | `#FF8A00` |
| Blue | `#0F5BCC` | `#3870FF` |
| Turquoise | `#00FF3D` | `#00FF9D` |
| Pink | `#B2091A` | `#FF0B43` |

Using the raw white value as the per-channel calibration, a display-only
conversion is:

```text
display[channel] = min(255, round(raw[channel] * 255 / white[channel]))
white = [0x44, 0xCF, 0x63]
```

The other five named colours land at their expected hues using factors derived
only from white. That supports interpreting the values as LED drive levels,
but the calibration has not yet been verified across every iO model. Consumers
that display the colour may apply this correction once for confirmed Oral-B
values; they must not apply it to a value that has already been converted.
Toothbrush Card 0.33.0 performs that single Oral-B-specific conversion. A
future explicitly named derived display-colour attribute can be considered if
the calibration is confirmed more broadly; the raw sensor state will remain
unchanged.

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

### Pacer numbering

Direct toothbrush notifications and charger passthrough use the same `FF09`
presentation rules in the observed payloads. Pacer IDs are zero-based: raw `0`
is `sector_1`, raw `3` is `sector_4`, and `0xFF` denotes the configured last
sector. `0xF0` means that no sector is defined. The three-byte value is
`[sector, elapsed sector seconds, configured sector count]`. Oral-B Live
advances this pacer state locally from the `FF26` schedule between reads and
treats each `FF09` response as an authoritative correction.

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

Oral-B Live therefore uses a strict two-read schedule: pressure on every
one-second tick and one auxiliary value. Battery, mode, pacer configuration and
brush-head remainder occupy the first auxiliary slots; timer and pacer sector
then alternate, with battery refreshed periodically and `BRUSH_STATUS` taking
one auxiliary slot every five ticks. A separate local 1 Hz
ticker advances the displayed timer, pacer sector and elapsed sector time
independently of BLE request latency; `FF08` and `FF09` remain the authoritative
correction anchors. `FF0D` is not polled by the shipped integration because a
third serial request can delay the live pressure/card path. Charger-native
brush state is checked periodically to provide an explicit stop signal.

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
wall_start = wall_time_at_rtc_read - (brush_rtc - session_timestamp)
```

The wall timestamp is stored with the RTC sample, so a retained record and RTC
that arrive in separate charger requests still form an accurate pair.

The live stream creates the HA session immediately. A later `FF29` record is
matched to that session and refines it without increasing `sessions_today`
twice.

`FF29` belongs to the brush and retains its latest summary. It is not a dump
of a charger queue and does not contain per-second pressure distribution or
per-zone pressure time. It also has no display-face field, so Oral-B Live keeps
a transient result delivered by the ending advertisement, direct `FF0A`
notification/read or charger-forwarded `FF0A` with the matching Home Assistant
session record. Association is bounded to the completed session: `off` and
`standard` are not verdicts, a later display change cannot overwrite a captured
result, and an unavailable result remains `null`.

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
is applied as intended. The value was restored during research. Write support
is not part of the Home Assistant integration; a separate guarded
[maintainer-only night-light tester](../tools/README.md#io-sense-night-light-tester)
preserves the two audited write experiments, requires an explicit `--apply`
flag and verifies changes by reading them back.

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
| `protocol.py` | pure toothbrush payload decoders including exact `FF29` and zero-based `FF09` pacer sectors |
| `coordinator.py` | source selection, live state, timer/pacer extrapolation, sampled pressure tracking and retained-session reconciliation |
| `sensor.py` | toothbrush entities plus a separate matched iO Sense device |
| `tests/test_protocol.py` | captured-packet regression tests with no Home Assistant dependency |
| `tools/iosense_probe.py` | standalone read-only advertisement, GATT and identity capture |
| `tools/iosense_night_light.py` | guarded maintainer-only tester for two audited charger settings |
