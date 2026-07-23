"""Constants for the Oral-B Live integration.

Byte-level knowledge in this file comes from GATT reconnaissance of an
Oral-B iO Series (model bytes 36 08 52, "IO Series") performed 2026-07,
cross-referenced with the oralb-ble library, bkbilly/oralb_ble and
MatrixEditor/oralb-io.
"""

from __future__ import annotations

DOMAIN = "oralb_live"

ORALB_MANUFACTURER_ID = 220  # 0x00DC, Procter & Gamble

# --- Connection mode ---------------------------------------------------------
# The brush accepts exactly ONE BLE client at a time and stops
# advertising while that slot is taken (verified 2026-07 on an iO
# Series 10 with an iO Sense charger). Whoever holds the slot gets the
# live data; everyone else gets silence. This is a firmware property of
# the brush and cannot be engineered away, so it is a per-entry option.
CONF_CONNECTION_MODE = "connection_mode"
# Live: Home Assistant seizes and holds the slot; 1 Hz live data in HA,
# but the iO Sense charger display and the phone app cannot connect.
CONNECTION_MODE_LIVE = "live"
# Charger priority: Home Assistant leaves the slot free during
# sessions (charger lights/timer work as designed) and connects only
# briefly afterwards to read the brush's own last-session record.
CONNECTION_MODE_CHARGER = "charger_priority"
DEFAULT_CONNECTION_MODE = CONNECTION_MODE_CHARGER

# --- GATT characteristics (Oral-B vendor service a0f0ff00-...) ---------------
CHAR_DEVICE_ID = "a0f0ff01-5047-4d53-8208-4f72616c2d42"  # MAC, reversed
CHAR_MODEL_ID = "a0f0ff02-5047-4d53-8208-4f72616c2d42"
CHAR_STATE = "a0f0ff04-5047-4d53-8208-4f72616c2d42"  # notify: [state, 0]
CHAR_STATUS_BLOB = "a0f0ff05-5047-4d53-8208-4f72616c2d42"  # byte0 = battery %
CHAR_BUTTON = "a0f0ff06-5047-4d53-8208-4f72616c2d42"  # notify: [button state]
CHAR_MODE = "a0f0ff07-5047-4d53-8208-4f72616c2d42"  # notify: [mode]
CHAR_BRUSH_TIME = "a0f0ff08-5047-4d53-8208-4f72616c2d42"  # notify: [min, sec], 1 Hz
CHAR_SECTOR = "a0f0ff09-5047-4d53-8208-4f72616c2d42"  # notify: [sector, ?, total]
CHAR_PRESSURE = "a0f0ff0b-5047-4d53-8208-4f72616c2d42"  # notify: [state, ...]
CHAR_SENSOR_DATA = "a0f0ff0d-5047-4d53-8208-4f72616c2d42"  # motion, ~30 Hz
CHAR_CONTROL = "a0f0ff21-5047-4d53-8208-4f72616c2d42"  # command channel
CHAR_RTC = "a0f0ff22-5047-4d53-8208-4f72616c2d42"  # seconds, brush epoch
CHAR_PACER = "a0f0ff26-5047-4d53-8208-4f72616c2d42"  # per-sector seconds
CHAR_SESSION_DATA = "a0f0ff29-5047-4d53-8208-4f72616c2d42"  # last session

NOTIFY_CHARS = (
    CHAR_STATE,
    CHAR_PRESSURE,
    CHAR_MODE,
    CHAR_BRUSH_TIME,
    CHAR_SECTOR,
)

# --- Advertisement payload (manufacturer data 0x00DC, 11 bytes) --------------
# [0] protocol  [1..2] model  [3] state  [4] pressure/flags
# [5..6] brush time [minutes, seconds]  [7] mode  [8] sector
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

RUNNING_STATE = 3

# States in which the brush is reachable and worth connecting to in
# LIVE mode. Charging (4) is included: docked is the most reliable
# moment to win the free slot before a session starts.
AWAKE_STATES = {1, 2, 3, 4, 5, 8, 9, 10}
# States in which the brush will not hold a connection anyway.
RELEASE_STATES = {114, 115}

# --- Charger-priority mode ---------------------------------------------------
# Advert states that show a session happened since our last sync.
SESSION_SEEN_STATES = {3, 8, 9, 10}
# Advert states quiet enough to sync in without disturbing anyone.
SYNC_STATES = {2, 4}
# Never attempt syncs more often than this.
SYNC_MIN_INTERVAL_SECONDS = 60
# Give the brush time to commit ff29 after returning to a quiet state.
SESSION_RECORD_SETTLE_SECONDS = 30
# A freshly completed session can briefly return the previous ff29 record.
SYNC_RETRY_DELAY_SECONDS = 30
SYNC_RETRY_ATTEMPTS = 4
# Refresh battery/state at least this often even without a session.
PERIODIC_SYNC_INTERVAL_SECONDS = 6 * 3600
# Sanity bound for a session duration parsed from the ff29 record.
MAX_SESSION_SECONDS = 7200

STORAGE_VERSION = 1

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

# ff0b pressure states (protocol >= 6)
PRESSURE_STATES: dict[int, str] = {
    0: "low",
    1: "normal",
    2: "high",
}

PRESSURE_FROM_ADV: dict[int, str] = {
    0x30: "normal",
    0x32: "normal",
    0x38: "high",
    0x3A: "high",
}

SIGNAL_UPDATE = f"{DOMAIN}_update"

# --- Connection management (live mode) ---------------------------------------
CONNECT_RETRIES = 3
# Backstop retry interval while we hold no connection. The brush stops
# advertising while ANY client holds its single slot, so advertisements
# alone cannot be relied on to trigger a reconnect.
RECONNECT_INTERVAL_SECONDS = 30
# The brush itself drops clients that stay idle for ~30 s (observed
# brush-side timeout, 2026-07). Reconnection after such a drop is
# immediate (see _on_disconnect); this constant only guards against a
# wedged link whose disconnect callback never fired.
STALE_CONNECTION_SECONDS = 900
RELEASE_GRACE_SECONDS = 8
