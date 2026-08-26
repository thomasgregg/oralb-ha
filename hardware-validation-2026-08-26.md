# Oral-B Live hardware validation — 2026-08-26

## Test environment

- Oral-B Live: `v0.7.34b4`
- Home Assistant Core: `2026.8.3`
- Home Assistant OS: `18.2`
- Host: Raspberry Pi 5
- Brush: Oral-B iO Series, model ID `54`, protocol `8`, firmware revision `82` (`00.82.26` from direct reads)
- Brush address suffix: `64D3`
- Normal integration option: `charger_priority`
- Test-only option: `live`
- Official Oral-B app: force-closed except while changing and restoring the Vibration setting
- iO Sense charger: unplugged during advertisement/direct/raw-capture tests
- Automated tests: 153 passed with both minimum and current dependency sets

All timestamps below use Europe/Berlin local time unless marked UTC.

## Issue #20 — number_of_sectors reports 8

Issue: <https://github.com/thomasgregg/oralb-ha/issues/20>

### Baseline

With **Brush vibration enabled** and **All sessions** selected:

- `number_of_sectors`: `4`
- `sector_times_seconds`: `[30, 30, 30, 30]`
- `target_duration_seconds`: `120`
- direct brush connection active

### Reverse-setting Home Assistant test

Only **Brush vibration** was disabled in the official app. After a 30-second synchronization, the app was force-closed and Home Assistant reconnected directly.

The setting change immediately produced:

- `number_of_sectors`: `unknown`
- `sector_times_seconds`: `null`
- `target_duration_seconds`: `null`
- no physical pacing vibrations
- no sector transitions during a 155-second run
- `sector`: `no_sector`
- running `sector_raw`: `240` (`0xF0`)
- final summary: 155 seconds, 0 quadrants, target duration `null`

This model/firmware did **not** reproduce the exact erroneous value `8`. It reproduced the empty-pacer configuration that triggers the issue on the older 2D64/firmware-50 handle, but protocol 8/firmware 82 reports a zero/unknown total instead of 8.

### Standalone raw capture

The read-only `iosense_probe.py --brush-pacer` capture completed successfully:

- script SHA-256: `0d90601a59ea48ad4f02a60417ecba01c286e84a6fcbd14aae79b0cc89e73cd6`
- selected device address: redacted in the JSON
- FF02 initial: `360852` (model 54, protocol 8, firmware revision 82)
- FF25 initial/final: `0b00010302040607`
- FF26 initial/final: `0000000000000000`
- FF09 initial: `000000`
- FF09 final and only raw change: `f00000`
- FF09 total hint: `0`
- running transition observed: yes
- ending transition observed: yes (`state_raw = 10`)
- notifications: 172
- subscription/unsubscription errors: none
- timer notifications ran continuously through 167 seconds

Capture artifact: [brush-pacer-vibration-off.json](./brush-pacer-vibration-off.json)

### Restoration

Brush vibration was re-enabled with **All sessions** selected. Direct reads then returned:

- `number_of_sectors`: `4`
- `sector_times_seconds`: `[30, 30, 30, 30]`
- `target_duration_seconds`: `120`

### Recommendation

Attach the raw JSON and this result to #20. Keep #20 open until either the original 2D64/firmware-50 reverse-setting capture is supplied or the integration defensively rejects an implausible FF09 total when FF26 has no usable schedule. This capture proves the stored Vibration setting controls FF26, but it also proves firmware families disagree about the empty-state FF09 total.

## Issue #21 — pressure changes while idle

Issue: <https://github.com/thomasgregg/oralb-ha/issues/21>

### Advertisement-only menu test: pass

With the charger unplugged, the config entry reloaded, and no live connection:

- state changed `initializing -> selection_menu`
- motor remained physically off
- pressure stayed `unknown`
- timer stayed `0`
- `sessions_today` and `last_session` did not change after the grace period

### Advertisement-only passive session: pass

- 51-second session
- pressure changed normally while running, including high-pressure samples
- pressure cleared to `unknown` immediately at stop
- exactly one session finalized after the 20-second grace period
- summary source: `advertisement`
- duration: 51 seconds
- quadrants: 2
- high-pressure events: 2
- high-pressure duration: 5.2 seconds

### Charger-priority settings/menu test: fail, new charger-specific path

With the brush physically in its settings menu and the motor off:

- main state entered `selection_menu` at about 08:33:04
- charger `brush_status` changed to `pre_run` at about 08:33:06
- integration synthesized `running` for about 18 seconds
- pressure became `normal`, force `1555 mN`
- charger `session_status` remained `inactive`
- the short false session was not added to `sessions_today`

After the charger was physically unplugged, a stale charger bridge state continued to report `running`; pressure remained `normal` at `1555 mN` and the synthetic timer advanced from 48 to 90 seconds. Reloading only the Oral-B Live config entry cleared it to advertisement/idle, pressure `unknown`, timer `0`.

The likely path is `charger brush_status = pre_run` being treated as an active session by `resolve_charger_session_running()`, followed by `_charger_session_started()` creating a synthetic running state and polling FF0B even though `session_status` is inactive and the motor is off.

### Recommendation

Do not close #21 solely on the advertisement result. The original advertisement idle/menu behavior passes in b4, but the charger-priority `pre_run` path is a concrete remaining bug. Either extend #21 with the charger evidence or open a focused issue titled approximately “Charger pre_run/settings menu creates synthetic running session and stale pressure.” A fix should require stronger evidence than `brush_status = pre_run`—for example active charger session state and/or a real brush running transition—and must clear the synthetic session on charger disconnect.

## Issue #22 — pause/resume fragments

Issue: <https://github.com/thomasgregg/oralb-ha/issues/22>

Status after validation: **closed as completed** on 2026-08-26. Hardware-evidence comment: <https://github.com/thomasgregg/oralb-ha/issues/22#issuecomment-5421946553>

### Short-pause merge path: pass

A direct/live session included a physical two-second motor pause:

- first running state began: 08:55:32.580
- interim `post_brushing_summary`: 08:56:15.082
- resumed running: 08:56:18.463
- timer continued from 42 to 43 rather than resetting
- final timer/duration: 72 seconds
- final state: `post_brushing_summary`
- pressure cleared to `unknown` on the final stop
- `sessions_today`: 3 -> 4 exactly once
- one `last_session` record was created, source `direct_brush`

The finalized session included both motor fragments:

- duration: 72 seconds
- quadrants: 2
- average pressure: 708 mN
- maximum pressure: 848 mN

### Reset path: pass with an observation

In an earlier longer stop/resume attempt, the brush reset its own timer to zero. The integration treated the resumed run as a new session and finalized it once, which is correct for a genuine timer reset.

During the first fragment of that earlier attempt, the direct timer advanced while the main entity remained idle, suggesting one running-state notification may have been missed. The clean short-pause test above did receive both running transitions and validates the intended merge behavior.

### Recommendation

The timestamped state/timer sequence was posted to #22. On model 54/protocol 8/firmware 82, b4 passes both required decisions: continuous timer fragments merge, while a brush-side timer reset starts a new session. The issue was closed as completed.

## Issue #26 — signed FF0B startup force

Issue: <https://github.com/thomasgregg/oralb-ha/issues/26>

Status after validation: **closed as completed** on 2026-08-26. Hardware-evidence comment: <https://github.com/thomasgregg/oralb-ha/issues/26#issuecomment-5421946882>

### Direct hardware result: pass

The complete direct pressure history contained 1,940 force samples. Startup included the signed-negative sample required to exercise the fix:

- timestamp: 08:50:57.222
- raw FF0B: `00cc9f61f9e40c071a38`
- force bytes: `61f9`
- signed little-endian force: `-1695 mN`
- decoded pressure state: `low`
- next force sample: `+665 mN`

Computed results:

- minimum raw signed force: `-1695 mN`
- maximum non-negative force: `2675 mN`
- average non-negative force: `1714 mN`
- values near unsigned 65535: none

The finalized Home Assistant session reported exactly the independently recomputed values:

- `average_pressure_millinewtons`: `1714`
- `maximum_pressure_millinewtons`: `2675`

This proves the negative startup value is decoded as signed, excluded from aggregates, and does not become a huge unsigned value.

### Recommendation

The raw startup value, next positive value, sample count, and aggregate equality were posted to #26. The issue was closed as completed.

## Final restored state

- Official app force-closed
- Brush vibration: enabled
- Vibration scope: All sessions
- Pacer schedule: four 30-second sectors
- Target duration: 120 seconds
- Oral-B Live option: `charger_priority`
- Brush: docked and reporting `charging`
- Brush timer: 0
- Brush pressure: `unknown`
- Home Assistant system log errors for `oralb_live`: none

The legacy iO Sense charger-detail entities remained `unavailable` after the charger was powered, the brush was docked, and the config entry was reloaded. The brush itself reports charging correctly from advertisement data. This does not affect the captured issue results, but charger bridge discovery should be checked again when the base next advertises.

## Suggested upstream actions

1. Completed: #26 received the signed startup/aggregate evidence and was closed.
2. Completed: #22 received the 42 -> 43 continuous-timer merge and single 72-second finalization evidence and was closed.
3. Remaining: attach `brush-pacer-vibration-off.json` to #20 and keep it open pending the older firmware-50 capture or a defensive fallback.
4. Remaining: keep #21 open or split the charger-specific `pre_run` false-session bug into a focused new issue with the captured state sequence.

GitHub comments were posted and issues #22 and #26 were closed after hardware validation. No comment was posted to #20 or #21, and no new issue was opened.
