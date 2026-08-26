# Oral-B Live hardware validation — 2026-08-26

## Test environment

- Oral-B Live: `v0.7.34b5`
- Home Assistant Core: `2026.8.3`
- Home Assistant OS: `18.2`
- Host: Raspberry Pi 5
- Brush: Oral-B iO Series, model ID `54`, protocol `8`, firmware revision `82` (`00.82.26` from direct reads)
- Brush address suffix: `64D3`
- Normal integration option: `charger_priority`
- Test-only option: `live`
- Official Oral-B app: force-closed except while changing and restoring the Vibration setting
- iO Sense charger: unplugged during advertisement/direct/raw-capture tests
- Automated tests: 164 passed with both minimum and current dependency sets

All timestamps below use Europe/Berlin local time unless marked UTC.

## Issue #20 — number_of_sectors reports 8

Issue: <https://github.com/thomasgregg/oralb-ha/issues/20>

Status after validation: **open for b5 and older-firmware hardware validation**. Evidence and release comment: <https://github.com/thomasgregg/oralb-ha/issues/20#issuecomment-5422166732>

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

Status after validation: **closed as completed after b5 charger hardware validation**. Hardware evidence: <https://github.com/thomasgregg/oralb-ha/issues/21#issuecomment-5422074693>. Fix and validation request: <https://github.com/thomasgregg/oralb-ha/issues/21#issuecomment-5422166958>

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

### Charger-priority settings/menu test on b4: fail, new charger-specific path

With the brush physically in its settings menu and the motor off:

- main state entered `selection_menu` at about 08:33:04
- charger `brush_status` changed to `pre_run` at about 08:33:06
- integration synthesized `running` for about 18 seconds
- pressure became `normal`, force `1555 mN`
- charger `session_status` remained `inactive`
- the short false session was not added to `sessions_today`

After the charger was physically unplugged, a stale charger bridge state continued to report `running`; pressure remained `normal` at `1555 mN` and the synthetic timer advanced from 48 to 90 seconds. Reloading only the Oral-B Live config entry cleared it to advertisement/idle, pressure `unknown`, timer `0`.

The likely path is `charger brush_status = pre_run` being treated as an active session by `resolve_charger_session_running()`, followed by `_charger_session_started()` creating a synthetic running state and polling FF0B even though `session_status` is inactive and the motor is off.

### Charger-priority b5 validation: pass

The `v0.7.34b5` provisional-session fix was installed through HACS and Home Assistant was restarted. Three targeted hardware paths passed.

With the motor physically off and the handle in its settings menu, the charger reported `pre_run`, but the integration did not promote it:

- initial brush state: `selection_menu`
- pressure remained `unknown`
- timer remained `0`
- charger session remained `inactive`
- `sessions_today` remained `6`
- after the handle became quiet, the integration remained idle on advertisement data for the complete 45-second observation window

A genuine charger-bridged session then promoted only after motor evidence:

- main state: `running`
- data source: `charger_bridge`
- live connection: `true`
- charger brush status: `run`
- charger session status: `active_running`
- timer advanced from 12 to 24 seconds during the sampled interval
- live pressure and force updated normally

At motor stop, pressure cleared immediately and the timer stabilized at 40 seconds. The charger session transitioned `active_running -> active_idle -> inactive`; exactly one session finalized after the grace period, `sessions_today` changed `6 -> 7`, and Last session duration became 40 seconds. The source returned to advertisement and the live timer reset to 0 without a duplicate finalization.

Finally, charger power was removed during a newly observed provisional `pre_run`. Bridge ownership was discarded immediately:

- `bridge_connected`: `false`
- main state remained `idle` from advertisement data
- live connection remained `false`
- pressure remained `unknown`
- timer remained `0`
- charger session remained `inactive`
- `sessions_today` remained `7`

The charger's diagnostic Brush status retained its last raw `pre_run` value while the base was unpowered, but that stale diagnostic did not control the brush coordinator. After charger power was restored and the brush was docked, Brush status cleared to `not_connected`, the brush reported `charging`, pressure was `unknown`, timer was 0, and the session remained inactive.

### Recommendation and resolution

Post the b5 results to #21 and close it as completed. The original advertisement path, passive-session path, charger provisional path, genuine charger promotion/finalization path, and charger-disconnect cleanup are all now confirmed on hardware.

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

The iO Sense charger was rediscovered during b5 validation and its detail entities were available. After the final power restoration, the brush reported charging from advertisement data, the charger Brush status was `not_connected`, charger Session status was `inactive`, pressure was `unknown`, timer was 0, and `sessions_today` remained 7.

## Suggested upstream actions

1. Completed: #26 received the signed startup/aggregate evidence and was closed.
2. Completed: #22 received the 42 -> 43 continuous-timer merge and single 72-second finalization evidence and was closed.
3. Completed: #20 received the raw capture and cross-firmware result. The defensive fallback shipped in `v0.7.34b5`; the issue remains open pending b5 validation and the older firmware-50 capture.
4. Completed: #21 received the charger-specific `pre_run` evidence. The provisional-session fix shipped in `v0.7.34b5`, passed all three targeted charger hardware checks, and the issue was closed.

GitHub comments were posted to all four issues. Issues #21, #22, and #26 were closed after hardware validation. Issue #20 remains open for the older firmware-50 capture or equivalent targeted confirmation. No new issue was opened.
