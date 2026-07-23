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
CHAR_SMILEY = "a0f0ff0a-5047-4d53-8208-4f72616c2d42"  # notify/read: display face
CHAR_PRESSURE = "a0f0ff0b-5047-4d53-8208-4f72616c2d42"  # notify: [state, ...]
CHAR_SENSOR_DATA = "a0f0ff0d-5047-4d53-8208-4f72616c2d42"  # motion, ~30 Hz
CHAR_CONTROL = "a0f0ff21-5047-4d53-8208-4f72616c2d42"  # command channel
CHAR_RTC = "a0f0ff22-5047-4d53-8208-4f72616c2d42"  # seconds, brush epoch
CHAR_AVAILABLE_MODES = "a0f0ff25-5047-4d53-8208-4f72616c2d42"
CHAR_PACER = "a0f0ff26-5047-4d53-8208-4f72616c2d42"  # per-sector seconds
CHAR_SESSION_DATA = "a0f0ff29-5047-4d53-8208-4f72616c2d42"  # last session
CHAR_REFILL_REMAINDER = "a0f0ff2d-5047-4d53-8208-4f72616c2d42"

NOTIFY_CHARS = (
    CHAR_STATE,
    CHAR_PRESSURE,
    CHAR_MODE,
    CHAR_BRUSH_TIME,
    CHAR_SECTOR,
)

OPTIONAL_NOTIFY_CHARS = (
    CHAR_STATUS_BLOB,
    CHAR_SMILEY,
)

# --- Advertisement payload (manufacturer data 0x00DC, 11 bytes) --------------
# [0] protocol  [1] model  [2] firmware  [3] state  [4] pressure/flags
# [5..6] brush time [minutes, seconds]  [7] mode  [8] sector
# [9] sector timer  [10] number of sectors
ADV_IDX_PROTOCOL = 0
ADV_IDX_MODEL = 1
ADV_IDX_FIRMWARE = 2
ADV_IDX_STATE = 3
ADV_IDX_PRESSURE = 4
ADV_IDX_TIME_HI = 5
ADV_IDX_TIME_LO = 6
ADV_IDX_MODE = 7
ADV_IDX_SECTOR = 8
ADV_IDX_SECTOR_TIMER = 9
ADV_IDX_TOTAL_SECTORS = 10

# --- Value maps --------------------------------------------------------------
STATES: dict[int, str] = {
    0: "unknown",
    1: "initializing",
    2: "idle",
    3: "running",
    4: "charging",
    5: "setup",
    6: "flight_menu",
    7: "charge_forbidden",
    8: "selection_menu",
    9: "session_summary",
    10: "post_brushing_summary",
    113: "final_test",
    114: "pcb_test",
    115: "sleeping",
    116: "transport",
    117: "calibration_test",
}

RUNNING_STATE = 3
# Some iO firmware can advertise selection_menu for an entire short motor
# session without ever exposing running. Treat both as active for the passive
# charger-priority fallback; a minimum duration below filters button/menu blips.
PASSIVE_SESSION_ACTIVE_STATES = {RUNNING_STATE, 8}
MIN_PASSIVE_SESSION_SECONDS = 5

# States in which the brush is reachable and worth connecting to in
# LIVE mode. Charging (4) is included: docked is the most reliable
# moment to win the free slot before a session starts.
AWAKE_STATES = {1, 2, 3, 4, 5, 8, 9, 10}
# States in which the brush will not hold a connection anyway.
RELEASE_STATES = {115, 116}

# --- Charger-priority mode ---------------------------------------------------
# Advert states that show a session happened since our last sync. A transition
# into this group creates a new sync generation; repeated advertisements and
# running -> summary transitions remain part of the same generation.
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
# Treat a brush record close to a passively observed start as the same
# session, so the authoritative record can refine the fallback without
# incrementing the daily count twice.
SESSION_RECONCILE_WINDOW_SECONDS = 2 * 60
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
    5: "super_sensitive",
    6: "tongue_clean",
    8: "settings",
    9: "off",
    11: "smart_adapt",
}

SECTOR_NO_SECTOR = 0xF0

SMILEYS: dict[int, str] = {
    0: "off",
    1: "standard",
    2: "special_2",
    3: "special_3",
    4: "special_4",
    5: "special_5",
    6: "special_6",
    7: "special_7",
}

REFILL_STATES: dict[int, str] = {
    0: "on",
    1: "reset",
    2: "snooze",
    0xFE: "interval",
    0xFF: "off",
}

# Model identifiers from the oralb-ble parser. ff02/advertisements cannot
# distinguish the marketing model for most iO brushes, so keep the generic
# protocol name where that is all the brush reports.
MODEL_NAMES: dict[int, str] = {
    0: "Triumph D36",
    1: "Triumph D36",
    2: "Triumph D36",
    32: "Genius Series D701",
    33: "Genius Series D701",
    34: "Genius Series D701",
    39: "Smart/Pro Series D700",
    40: "Smart/Pro Series D700",
    41: "Smart/Pro Series D700",
    48: "iO Series",
    49: "iO Series",
    50: "iO Series",
    52: "iO Series 4",
    53: "iO Series 5",
    54: "iO Series",
    64: "Smart Series D21",
    65: "Smart Series D21",
    66: "Smart Series D21",
    67: "Smart Series D21",
    68: "Smart Series D21",
    69: "Smart Series D21",
    70: "Smart Series D21",
    80: "Pro Series D601",
    81: "Pro Series D601",
    82: "Pro Series D601",
    83: "Pro Series D601",
    84: "Pro Series D601",
    85: "Pro Series D601",
    86: "Pro Series D601",
    87: "Pro Series D601",
    112: "Genius X D706",
    113: "Genius X D706",
    114: "Genius X D706",
    117: "Genius X D706",
    118: "Genius X D706",
    119: "Genius X D706",
}

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
