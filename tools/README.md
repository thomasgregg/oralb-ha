# Standalone iO Sense night-light tester

`iosense_night_light.py` tests the charger ring colour and night-light mode
without loading or modifying the Home Assistant integration. It uses only the
two reconstructed charger commands:

- `0x36`: ring colour (`R G B`)
- `0x42`: night-light mode (`disabled`, `solid`, `breathing`, `rainbow`,
  `cool`, or `custom`)

Stop Home Assistant, or at least disable/reload the integration, before a live
test so that two processes do not compete for the same BLE charger connection.
Install the one runtime dependency in a virtual environment if needed:

```bash
/opt/homebrew/bin/python3.12 -m venv .venv-night-light
.venv-night-light/bin/python -m pip install bleak
```

Python 3.12 is used here because Apple's system Python 3.9 cannot build the
current PyObjC dependency with the installed macOS SDK.

## Protocol-state validation

On 2026-08-01, an iO Series 10/iO Sense roundtrip successfully changed
`#000000` + `disabled` to `#7A20FF` + `solid`. Both POST operations returned a
success status and both new values were confirmed by GET. Five seconds later,
the tool restored `#000000` + `disabled` and confirmed both restored values by
GET again. No physical light change was visible on the idle charger during the
test, so this validates writable stored protocol state, not yet an observable
night-light control.

A subsequent read found command `0x43` (weekly night-light schedule) set to
`FF FF` for all seven days. The schedule may gate the mode, but the meaning and
units of its start/end bytes must be established before attempting a schedule
write. Command `0x36` may instead be the brush smart-ring colour mirrored by
the charger and only become visible in an active brushing/pressure context.

A second roundtrip on 2026-08-01 used `#FF0000` with mode `custom` (`0x05`).
The complete charger ring visibly illuminated red for several seconds. The
tool then restored and verified `#000000` + `disabled`. This confirms that
custom RGB display requires the `custom` mode; the earlier `solid` test stored
the RGB value but did not visibly apply it to the complete ring.

Preview the exact protocol frames entirely offline:

```bash
.venv-night-light/bin/python tools/iosense_night_light.py frames \
  --color '#7A20FF' --mode solid
```

Read the current values without changing anything:

```bash
.venv-night-light/bin/python tools/iosense_night_light.py status
```

Mutation commands are live dry runs unless `--apply` is supplied. The safest
first write test applies a colour and active mode briefly, verifies both, and
then automatically restores the original values:

```bash
.venv-night-light/bin/python tools/iosense_night_light.py roundtrip \
  '#7A20FF' --mode solid --hold 5

# Review the dry-run output, then explicitly permit the writes:
.venv-night-light/bin/python tools/iosense_night_light.py roundtrip \
  '#7A20FF' --mode solid --hold 5 --apply
```

The individual controls are:

```bash
# Each command is a dry run until --apply is added.
.venv-night-light/bin/python tools/iosense_night_light.py set-color '#7A20FF'
.venv-night-light/bin/python tools/iosense_night_light.py enable --mode solid
.venv-night-light/bin/python tools/iosense_night_light.py disable
.venv-night-light/bin/python tools/iosense_night_light.py set-mode breathing
```

Before any applied mutation, the tool writes a timestamped JSON backup of the
current colour and mode. Restore one explicitly with:

```bash
.venv-night-light/bin/python tools/iosense_night_light.py restore \
  iosense-night-light-backup-YYYYMMDD-HHMMSS.json --apply
```

If more than one charger is found, or macOS discovery needs a stable target,
place `--address ADDRESS_OR_COREBLUETOOTH_UUID` before the subcommand.

The default delays are intentionally conservative because the charger has
previously acknowledged a setting before safely applying its payload. Every
write requires a success status, waits one second, and then performs a GET
read-back comparison.
