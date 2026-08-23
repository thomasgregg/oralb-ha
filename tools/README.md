# Standalone tools

These tools run independently of Home Assistant. Live discovery and GATT
operations require a computer with a local Bluetooth adapter; they cannot route
connections through Home Assistant's ESPHome, Shelly or other remote Bluetooth
proxies. The night-light tester's `frames` subcommand is entirely offline and
requires neither Bluetooth nor Bleak.

## iO Sense diagnostic probe

`iosense_probe.py` automatically finds a likely iO Sense charger, captures its
complete portable Bluetooth advertisement, connects to it, enumerates its GATT
services, reads every characteristic marked readable, and attempts read-only
descriptor reads. When the known iO Sense command transport is present,
it also requests a small identity snapshot using only protocol GET operations.

Use this tool when:

- an iO Sense is powered but Oral-B Live does not create a charger device;
- `charger_address` remains `null`;
- the integration has no charger-related debug log lines;
- a charger may be a newer hardware or firmware revision;
- maintainers request its advertisement or GATT layout for an issue.

### Safety

The tool never sends charger POST/SET operations and never writes a command
payload. Its optional protocol phase is restricted to GET requests for
firmware, hardware version, server mode, device ID and paired-brush identity.
The command header and terminator for a GET are GATT writes because that is how
the charger's read protocol is transported, but they do not change settings.

The default run performs these operations in order:

1. active BLE scan;
2. automatic charger selection, with a short menu only if ambiguous;
3. connection and GATT service discovery;
4. reads of characteristics marked readable and read-only attempts on their
   descriptors;
5. known charger GET requests, only when the required transport exists.

For an advertisement-only capture with no connection and no GATT writes, use
`--scan-only`. To connect and dump ordinary GATT while skipping the charger GET
protocol, use `--no-protocol`.

### Prerequisites

- Python 3.10 or newer on Windows and Linux.
- Python 3.12 on current macOS systems. Do not use Apple's bundled
  `/usr/bin/python3` when it reports Python 3.9: current PyObjC packages cannot
  be installed successfully with that interpreter.
- A working local Bluetooth Low Energy adapter in the computer running the
  tool.
- Bluetooth permission for the terminal or Python application on macOS.
- BlueZ and access to its D-Bus Bluetooth service on Linux.
- The `bleak` Python package.

The probe does not import the Home Assistant integration and does not require
Home Assistant, HACS or an Oral-B account.

### Installation

The easiest method does not require cloning the repository. Download the
standalone `iosense_probe.py` attached to the latest GitHub release into a
dedicated directory.

macOS or Linux:

```bash
mkdir -p "$HOME/iosense-diagnostic"
cd "$HOME/iosense-diagnostic"
curl --fail --location --output iosense_probe.py \
  https://github.com/thomasgregg/oralb-ha/releases/latest/download/iosense_probe.py
```

On macOS, create the environment with Python 3.12 specifically:

```bash
python3.12 --version
python3.12 -m venv .venv-iosense
.venv-iosense/bin/python -m pip install --upgrade pip bleak
```

If `python3.12` is not found, install Python 3.12 with Homebrew or from
python.org, open a new terminal, and repeat those commands. An environment
previously created with Apple's Python 3.9 must be recreated with Python 3.12.

On Linux, create it with the system's supported Python 3:

```bash
python3 --version
python3 -m venv .venv-iosense
.venv-iosense/bin/python -m pip install --upgrade pip bleak
```

Windows PowerShell:

```powershell
$ProbeDirectory = Join-Path $HOME "iosense-diagnostic"
New-Item -ItemType Directory -Force $ProbeDirectory | Out-Null
Set-Location $ProbeDirectory
Invoke-WebRequest `
  -Uri "https://github.com/thomasgregg/oralb-ha/releases/latest/download/iosense_probe.py" `
  -OutFile "iosense_probe.py"
py -3.12 -m venv .venv-iosense
.venv-iosense\Scripts\python.exe -m pip install --upgrade pip bleak
```

Maintainers running from a source checkout can instead change to the cloned
repository, verify the source file, and create the environment there:

```bash
cd /path/to/oralb-ha
test -f tools/iosense_probe.py && echo "oralb-ha repository found"
python3.12 -m venv .venv-iosense  # macOS
.venv-iosense/bin/python -m pip install --upgrade pip bleak
```

Replace `/path/to/oralb-ha` with the real checkout location. If the terminal is
still in the home directory, running `tools/iosense_probe.py` would produce a
`can't open file .../tools/iosense_probe.py` error.

### Installation troubleshooting

| Error | Cause and correction |
| --- | --- |
| `Failed building wheel for pyobjc-core` on macOS, or a package reports that it requires a newer Python | The environment was created with an unsupported Python version. Recreate it with Python 3.12 on macOS or Python 3.10 or newer on Windows/Linux. |
| `can't open file .../tools/iosense_probe.py` | A source-checkout command was run outside the cloned repository. Change to the `oralb-ha` directory, or use the recommended release download and run `iosense_probe.py` directly. |
| `No module named bleak` | Bleak was installed into a different Python. Run `.venv-iosense/bin/python -m pip install bleak` and invoke the probe with that same Python path. |
| Bluetooth permission or authorization error on macOS | Enable Bluetooth access for Terminal, iTerm or the application hosting the shell in System Settings, then reopen it. |

### Prepare the charger

Before running the probe:

1. close or force-quit the Oral-B phone app;
2. temporarily disable Oral-B Live or stop Home Assistant if it is actively
   connecting to the charger;
3. put the computer within one or two metres of the charger;
4. keep the charger powered;
5. tap the iO Sense control and wake the toothbrush once.

Closing other clients avoids two applications competing for the charger's BLE
peripheral connection. The scan-only mode does not establish a connection and
does not require Oral-B Live to be disabled.

### Run the complete probe

From the diagnostic directory created above on macOS or Linux:

```bash
.venv-iosense/bin/python iosense_probe.py
```

Windows PowerShell:

```powershell
.venv-iosense\Scripts\python.exe iosense_probe.py
```

For a source checkout, use `.venv-iosense/bin/python tools/iosense_probe.py`
instead. The virtual environment does not need to be activated; invoking its
Python executable directly is sufficient.

The scan normally selects the charger automatically using the advertised
`iO Sense` name, the known charger service UUID, or Procter & Gamble
manufacturer ID `220`. The ordinary 11-byte toothbrush advertisement is
explicitly deprioritized. If no known signature exists—important for a
possible new charger revision—the tool shows the ten strongest nearby devices
and asks for one numbered selection.

An automatic selection looks like:

```text
Scanning for Bluetooth Low Energy devices (15s)...
Selected iO Sense (AA:BB:CC:DD:EE:FF), RSSI -48 dBm
Connecting to capture GATT and read-only values...
Known charger transport found; running read-only GET probes...
```

When multiple possible chargers exist, choose from the short menu:

```text
Multiple possible iO Sense devices were found:
  [1] iO Sense  AA:BB:CC:DD:EE:FF  RSSI -48  manufacturer IDs [220]
  [2] iO Sense  11:22:33:44:55:66  RSSI -76  manufacturer IDs [220]
Select device [1]:
```

### Output

The tool prints an issue-friendly summary and writes a timestamped JSON report
in the current directory:

```text
iosense-probe-YYYYMMDD-HHMMSS.json
```

The report includes:

- operating system, Python and Bleak versions;
- selected device name, address and RSSI;
- advertised service UUIDs;
- manufacturer and service-data payloads with their complete hex values;
- legacy iO Sense advertisement decoding or the exact incompatibility reason;
- every discovered GATT service, characteristic, property and descriptor;
- successful read values and individual read errors;
- whether the known charger command transport exists;
- every read-only GET response and every frame written by the protocol probe;
- an explicit safety record stating that no POST or SET operation was sent.

A failure to connect is still saved in the report alongside the advertisement
that was found. Attach the JSON file and the console summary to the relevant
GitHub issue. The report contains Bluetooth addresses, a possible device ID and
raw manufacturer data; inspect it before posting publicly. Do not remove or
alter the manufacturer payload when investigating a new advertisement format,
because those bytes are the evidence needed to add safe support.

### Useful options

Capture only the advertisement, with no connection or GATT writes:

```bash
.venv-iosense/bin/python iosense_probe.py --scan-only
```

Dump GATT but do not run the charger's command-based GET protocol:

```bash
.venv-iosense/bin/python iosense_probe.py --no-protocol
```

Select an exact address or macOS CoreBluetooth UUID:

```bash
.venv-iosense/bin/python iosense_probe.py \
  --address AA:BB:CC:DD:EE:FF
```

Use a longer scan and a specific report filename:

```bash
.venv-iosense/bin/python iosense_probe.py \
  --scan-timeout 30 --output iosense-issue-14.json
```

Run `.venv-iosense/bin/python iosense_probe.py --help` for the full option
list. When running from a source checkout, substitute `tools/iosense_probe.py`
for `iosense_probe.py` in these examples.

### Interpreting common results

| Result | Likely meaning |
| --- | --- |
| Known 14-byte `0xA2` advertisement and known GATT transport both succeed | The charger protocol is supported; investigate the Home Assistant/proxy discovery path |
| Manufacturer ID `220` is present but payload length or type differs | Likely newer advertisement revision; attach the complete report |
| Known advertisement but connection fails | Charger is advertising, but the local adapter, another connected client or signal quality prevented GATT access |
| No known advertisement, but a manually selected device exposes the charger GATT service | The advertisement matcher needs to support that variant |
| Advertisement is found and GATT layout differs | Possible new charger hardware/protocol revision |
| Nothing plausible is found | Move closer, close the Oral-B app, tap the charger, extend `--scan-timeout`, and confirm the computer's local BLE adapter works |

The tool cannot prove whether an ESPHome or Shelly proxy delivered the same
advertisement to Home Assistant. It provides an independent local capture: if
the probe finds a supported charger while Home Assistant does not, focus next
on scanner mode, proxy reachability and Home Assistant Bluetooth diagnostics.

## Toothbrush pacer capture

The same release tool has a `--brush-pacer` mode for cases where the handle's
reported sector count disagrees with its physical pacing. It captures the
evidence needed for [issue #20](https://github.com/thomasgregg/oralb-ha/issues/20)
in one run:

- an exact initial FF02 device-information read;
- exact initial and post-session reads of FF25 (available modes), FF26 (pacer
  configuration) and FF09 (sector data), including empty or all-zero replies;
- timestamped raw notifications from FF04 (state), FF07 (mode), FF08 (timer)
  and FF09 (sector);
- positional annotations alongside—not instead of—the exact length and hex;
- automatic completion when a running-state notification is followed by a
  non-running state, with a two-second tail to retain final notifications;
- a 240-second safety timeout if that transition is not observed.

This mode never calls `write_gatt_char`, changes a brush setting or sends a
control command. Bluetooth notification setup normally causes the operating
system to write temporary Client Characteristic Configuration Descriptor
(CCCD) values. Those are subscription controls, not toothbrush configuration;
the tool removes every successful subscription before disconnecting.

### Download and run it

The reporter does not need a repository checkout. After a release containing
tool version 2 or later is published, these macOS/Linux commands download a
fresh standalone copy and run the complete capture:

```bash
mkdir -p "$HOME/oralb-pacer-test"
cd "$HOME/oralb-pacer-test"
curl --fail --location --output iosense_probe.py \
  https://github.com/thomasgregg/oralb-ha/releases/latest/download/iosense_probe.py
python3.12 -m venv .venv-iosense
.venv-iosense/bin/python -m pip install --upgrade pip bleak
.venv-iosense/bin/python iosense_probe.py \
  --brush-pacer --session-timeout 240 --output brush-pacer.json
```

On Linux, use `python3` in place of `python3.12` if it is Python 3.10 or newer.
On Windows PowerShell:

```powershell
$TestDirectory = Join-Path $HOME "oralb-pacer-test"
New-Item -ItemType Directory -Force $TestDirectory | Out-Null
Set-Location $TestDirectory
Invoke-WebRequest `
  -Uri "https://github.com/thomasgregg/oralb-ha/releases/latest/download/iosense_probe.py" `
  -OutFile "iosense_probe.py"
py -3.12 -m venv .venv-iosense
.venv-iosense\Scripts\python.exe -m pip install --upgrade pip bleak
.venv-iosense\Scripts\python.exe iosense_probe.py `
  --brush-pacer --session-timeout 240 --output brush-pacer.json
```

Before starting, close the Oral-B phone app, temporarily disable the Oral-B
Live config entry or stop Home Assistant, and unplug the iO Sense charger for
the duration of the capture. A toothbrush generally has one live BLE
connection slot, so another client can prevent this capture or make it
incomplete. Re-enable Home Assistant and power the charger again after the tool
exits.

The probe subscribes before it prints `Ready. Start brushing now.` After that
message, run one normal uninterrupted brushing session and stop the handle in
the usual way. For issue #20, use the routine that physically paces four
30-second zones; do not change the routine between the Home Assistant/Card
check and this raw capture.

The script does not change the official app's Vibration setting. For the
reverse test proposed in issue #20, turn Vibration off in the app first, allow
the app to finish synchronizing, and then force-close it before following the
disconnect steps above. The initial/final FF25, FF26 and FF09 snapshots show
whether that reproduced configuration remains stable throughout the captured
session.

If several toothbrushes are nearby, select the correct numbered entry. An
exact address or macOS CoreBluetooth UUID can instead be supplied:

```bash
.venv-iosense/bin/python iosense_probe.py --brush-pacer \
  --address "AA:BB:CC:DD:EE:FF" --output brush-pacer.json
```

Return `brush-pacer.json` plus the printed summary. The selected Bluetooth
address is redacted in brush-mode JSON, but FF02 and raw advertisement bytes
could still be device-specific, so inspect the file before posting it
publicly. Also report separately what Toothbrush Card displayed during a
normal Home Assistant-connected session: its version, whether sector count was
set to Auto or manually overridden, how many zones it drew, and the displayed
sector at approximately 30, 60, 90 and 120 seconds. The probe cannot test the
card concurrently because it temporarily owns the brush's BLE connection.

## iO Sense night-light tester

`iosense_night_light.py` tests the charger ring colour and night-light mode
without loading or modifying the Home Assistant integration. It uses only the
two reconstructed charger commands:

- `0x36`: ring colour (`R G B`)
- `0x42`: night-light mode (`disabled`, `solid`, `breathing`, `rainbow`,
  `cool`, or `custom`)

This maintainer-only tester is not attached to releases. Run it from a source
checkout, and create its virtual environment from the repository root:

```bash
git clone https://github.com/thomasgregg/oralb-ha.git  # omit if already cloned
cd oralb-ha
test -f tools/iosense_night_light.py && echo "oralb-ha repository found"
python3.12 -m venv .venv-night-light
.venv-night-light/bin/python -m pip install --upgrade pip bleak
```

On Windows, use `py -3.12 -m venv .venv-night-light` and replace
`.venv-night-light/bin/python` in the examples below with
`.venv-night-light\Scripts\python.exe`. Python 3.12 is used here because
Apple's system Python 3.9 is unsupported by the current Bleak/PyObjC
dependencies.

Stop Home Assistant or disable the Oral-B Live config entry before a live test
so that two processes do not compete for the same BLE charger connection.
Reloading the entry is not sufficient because it reconnects afterward.

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
write.

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

Mutation commands are live dry runs unless `--apply` is supplied: they scan,
connect and read the current settings, but issue no POST/write operation. The
safest first write test applies a colour and active mode briefly, verifies
both, and then automatically restores the original values:

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
  iosense-night-light-backup-YYYYMMDD-HHMMSS-ffffff.json --apply
```

If more than one charger is found, or macOS discovery needs a stable target,
place `--address ADDRESS_OR_COREBLUETOOTH_UUID` before the subcommand.

The default delays are intentionally conservative because the charger has
previously acknowledged a setting before safely applying its payload. Every
write requires a success status and performs a GET read-back comparison. By
default it waits one second before that read-back; maintainers can adjust the
delay with `--settle-delay` when investigating timing.
