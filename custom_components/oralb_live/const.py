"""Constants for the Oral-B Live integration.

Byte-level knowledge in this file comes from GATT reconnaissance of an
Oral-B iO Series (model bytes 36 08 52, "IO Series") performed 2026-07,
cross-referenced with the oralb-ble library and bkbilly/oralb_ble.
"""

from __future__ import annotations

DOMAIN = "oralb_live"

ORALB_MANUFACTURER_ID = 220  # 0x00DC, Procter & Gamble

# --- GATT characteristics (Oral-B vendor service a0f0ff00-...) ---------------
CHAR_DEVICE_ID = "a0f0ff01-5047-4d53-8208-4f72616c2d42"  # MAC, reversed
CHAR_MODEL_ID = "a0f0ff02-5047-4d53-8208-4f72616c2d42"
CHAR_STATE = "a0f0ff04-5047-4d53-8208-4f72616c2d42"  # notify: [state, 0]
CHAR_STATUS_BLOB = "a0f0ff05-5047-4d53-8208-4f72616c2d42"  # byte0 = battery %
CHAR_PRESSURE_EVENT = "a0f0ff06-5047-4d53-8208-4f72616c2d42"  # notify: [0, hi, 0, 0]
CHAR_MODE = "a0f0ff07-5047-4d53-8208-4f72616c2d42"  # notify: [mode]
CHAR_BRUSH_TIME = "a0f0ff08-5047-4d53-8208-4f72616c2d42"  # notify: [hi, lo] seconds, 1 Hz
CHAR_SECTOR = "a0f0ff09-5047-4d53-8208-4f72616c2d42"  # notify: [sector, ?, total]
CHAR_RTC = "a0f0ff22-5047-4d53-8208-4f72616c2d42"
CHAR_PACER = "a0f0ff26-5047-4d53-8208-4f72616c2d42"  # per-sector seconds

NOTIFY_CHARS = (
    CHAR_STATE,
    CHAR_PRESSURE_EVENT,
    CHAR_MODE,
    CHAR_BRUSH_TIME,
    CHAR_SECTOR,
)

# --- Advertisement payload (manufacturer data 0x00DC, 11 bytes) --------------
# [0] protocol  [1..2] model  [3] state  [4] pressure/flags
# [5..6] brush time seconds (big endian)  [7] mode  [8] sector
# [9] sector flags  [10] extra
ADV_IDX_STATE = 3
ADV_IDX_PRESSURE = 4
ADV_IDX_TIME_HI = 5
ADV_IDX_TIME_LO = 6
ADV_IDX_MODE = 7
ADV_IDX_SECTOR = 8

# --- Value maps --------------------------------------------------------------
STATES: dict[int, str] = {
    0: "unknown",
    1: "initializing",
    2: "idle",
    3: "running",
    4: "charging",
    5: "setup",
    6: "flight_menu",
    7: "final_test",
    8: "selection_menu",
    9: "session_summary",
    10: "post_brushing_summary",
    113: "pcb_test",
    114: "sleeping",
    115: "transport",
}

# States in which the brush is awake and accepts/keeps a connection.
AWAKE_STATES = {1, 2, 3, 5, 8, 9, 10}
# States after which we release the connection so the phone/app can have it.
RELEASE_STATES = {4, 114, 115}

# iO-series mode identifiers. Unknown values are exposed as "mode_<n>".
MODES: dict[int, str] = {
    0: "daily_clean",
    1: "sensitive",
    2: "gum_care",
    3: "whiten",
    4: "intense",
    6: "super_sensitive",
    7: "tongue_clean",
    8: "settings",
}

SECTOR_NO_SECTOR = 0xF0

PRESSURE_FROM_ADV: dict[int, str] = {
    0x30: "normal",
    0x32: "normal",
    0x38: "high",
    0x3A: "high",
}

SIGNAL_UPDATE = f"{DOMAIN}_update"

# Connection management
CONNECT_RETRIES = 2
IDLE_DISCONNECT_SECONDS = 120
RELEASE_GRACE_SECONDS = 8
