# Display-face hardware validation

This working log correlates the physical Oral-B handle display with the raw
display-face value exposed by Oral-B Live. It supports
[Toothbrush Card issue #20](https://github.com/mtheli/toothbrush-card/issues/20).

## Test device

| Field | Value |
| --- | --- |
| Retail model | Oral-B iO 10 |
| Home Assistant device | iO Series Toothbrush 64D3 |
| Oral-B model reported over BLE | iO Series (`model_id` 54) |
| BLE protocol | 8 |
| Firmware | `00.82.26` (`firmware_revision` 82) |
| Secondary controller | 0 |
| Media content | 26 |
| Oral-B Live connection mode | Charger priority |
| iO Sense bridge | Used when available |

The retail model was supplied by the tester; it is not distinguishable from
the generic BLE model information reported by the brush.

## Method

1. Start the physical brush and run it for the planned duration.
2. Stop it normally.
3. Immediately observe or photograph the result shown on the handle.
4. Read the retained `display_face` from the Oral-B Live **Last session**
   entity through Home Assistant MCP.
5. Record the session duration, mode, physical display and reported value.

Durations are observations, not assumed thresholds. Repeat controlled runs to
separate stable display mappings from session-scoring variation.

## Observations

| Run | Controlled | Duration | Mode | Oral-B Live value | Physical display | Source | Notes |
| ---: | :---: | ---: | --- | --- | --- | --- | --- |
| 0 | No | 29 s | Tongue clean | `special_9` | Not observed | iO Sense charger bridge | Pre-test baseline retained by Home Assistant; do not use as a decoded mapping without a matching display observation. |
| 1 | Yes | 22 s | Tongue clean | `special_6` | Blue smiling face with two round eyes and an upturned curved mouth | iO Sense charger bridge | The handle display also showed `0:22`. The live Smiley entity had already returned to `off`; the value was recovered from the retained Last session `display_face`. |
| 2 | Yes | 21 s live / 22 s retained | Daily clean | No result (`display_face: null`) | Pause/paused display; no face | iO Sense charger bridge, then retained session | Smiley stayed `off` with raw value 0. |
| 3 | Yes | 21 s live / 22 s retained | Daily clean | No result (`display_face: null`) | Pause/paused display; no face | iO Sense charger bridge, then retained session | Repeated result. Smiley stayed `off` with raw value 0. |
| 4 | No | 12 s live / 13 s retained | Intense at start; switched to Daily clean at 9 s | No result (`display_face: null`) | Not observed | iO Sense charger bridge, then retained session | Unplanned setup run. Smiley stayed `off` with raw value 0; provenance changed from `advertisement` to `charger_bridge` during the run and back to `advertisement` afterward. Do not use as a decoded mapping. |
| 5 | Yes | 118 s live / 119 s retained | Daily clean | `special_5` (raw 5) | Blue smiling face with two round eyes and an upturned curved mouth | iO Sense charger bridge, then retained session | Photo `IMG_3323.HEIC` shows `1:59` and four completed quadrant dots. The result arrived about 2 s after switch-off and was preserved with `display_face_source: charger_bridge`. |
| 6 | Yes | 111 s | Daily clean | Advertisement: `off` (raw 0); later direct FF0A: `special_5` (raw 5) | Blue smiling face with two round eyes and an upturned curved mouth | Toothbrush advertisement, then direct brush | Photo `IMG_3325.heic` shows `1:51` and four completed quadrant dots. The advertisement stream captured every timer second but emitted no result face; a direct connection to the unchanged saved session then read FF0A as 5. |

### Run 5 capture

All timestamps below are Home Assistant local time (Europe/Berlin) on
2026-08-26.

| Event or measurement | Captured value |
| --- | --- |
| Selection menu observed | 12:38:24.510 |
| Running observed | 12:38:28.095 |
| Brush stopped / idle | 12:40:27.344 |
| Live timer at stop | 118 s |
| Result signal | 12:40:29.339: `special_5`, raw 5, source `charger_bridge` |
| Retained session received | 12:40:47.347 |
| Retained duration / target | 119 s / 120 s |
| Retained mode / quadrants | Daily clean / 4 |
| Retained display result | `special_5`, source `charger_bridge` |
| Photographed handle display | Blue smiling face with two round eyes and an upturned curved mouth; `1:59`; four completed quadrant dots |
| Retained pressure summary | 0 high-pressure events; 1 low-pressure event; 119 s low; average 600 mN; maximum 600 mN |
| Battery | 99% at end; estimated runtime changed to 8,982 s |
| Sessions today | 8 before retained processing, 9 afterward |
| Smiley reset | 12:40:57.347: `off`, raw 0, source `advertisement` |
| Charger session ended | 12:40:58.323: `inactive` |

### Run 6 capture

The iO Sense charger was powered off and Oral-B Live confirmed
`data_source: advertisement` before the session. The handle was stopped
manually at exactly 1:51.

| Event or measurement | Captured value |
| --- | --- |
| Running observed | 12:48:06.058 |
| Brush entered post-brushing summary | 12:49:57.311 |
| Final timer | 111 s (`1:51`) |
| Mode / sectors | Daily clean / sectors 1 through 4 |
| Live pressure | `normal`, source `advertisement` |
| Smiley throughout capture | `off`, raw 0, source `advertisement` |
| Photographed handle display | Blue smiling face with two round eyes and an upturned curved mouth; `1:51`; four completed quadrant dots |
| Immediate session record | 111 s, target 120 s, 4 quadrants, source `advertisement` |
| Immediate retained face | `display_face: null`, `display_face_source: null` |
| Charger state | `not_connected`; session `inactive` throughout |
| Direct connection established | 12:57:30.894: `data_source: direct_brush`, `live_connection: true` |
| Direct FF0A result | `special_5`, raw 5, source `direct_brush` |
| Direct battery read | 98%, source `direct_brush` |

## Issue-ready conclusion

One controlled observation on BLE model ID 54, protocol 8, firmware
`00.82.26` pairs `special_6` with an ordinary blue smiling face with round eyes
and an upturned mouth after a 22-second tongue-clean session. It is not the
star-eyed face reported for `special_6` on the issue author's iO6 and iO8.

Two matching 21/22-second Daily Clean runs showed only the handle's paused
display and produced no FF0A result: Smiley stayed raw 0 (`off`) and the
retained session kept `display_face: null`. Oral-B Live did not incorrectly
inherit the earlier tongue-clean face.

This demonstrates that the short-session behaviour reported for the iO6/iO8
does not reproduce on this iO 10: at roughly 22 seconds in Daily Clean it emits
no `standard`/frown result at all, while Tongue Clean can emit `special_6` with
an ordinary smile. The observations suggest that mode and/or iO 10 firmware
affect whether a result face is produced and how its value is numbered. Over
BLE the iO 10 reports only the generic `iO Series` name and model ID 54.

A photographed 119-second Daily Clean session now pairs `special_5` (raw 5)
with the same visible ordinary blue smile previously paired with `special_6`
after a 22-second Tongue Clean session on this brush. The photo also matches
the retained duration (`1:59`) and four-quadrant report. Therefore the raw
`special_N` number cannot safely be treated as a globally unique face design,
even on one handle and firmware version; the integration and card should
preserve the raw value and its provenance rather than assigning a universal
semantic label without more protocol evidence.

Run 6 adds a stronger transport-specific result: with the iO Sense powered
off, the advertisement stream captured the complete 111-second Daily Clean
session but exposed raw face 0 (`off`) throughout, even though a photograph
shows the handle displaying the ordinary blue smile at 1:51. On this iO 10,
the advertised three-bit field therefore did not represent the visible
post-session face in this controlled run. Home Assistant was then switched to
a direct brush connection without starting another session. Its initial FF0A
read returned raw 5 (`special_5`) for that same still-current result, matching
the photographed ordinary smile. This is a controlled same-session transport
disagreement: advertisement raw 0 versus direct FF0A raw 5.
