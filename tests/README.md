# Running the test suite

Use a dedicated Python 3.12 environment. The pinned dependencies exercise the
integration against Home Assistant 2024.4.4, the minimum supported release.

```bash
python3.12 -m venv .venv-test
.venv-test/bin/python -m pip install --requirement requirements-test.txt
.venv-test/bin/python -m pytest
```

The coordinator test module intentionally fails to import when its Home
Assistant dependencies are missing. A full test run must never report success
by silently skipping the coordinator and session tests.

CI runs the complete suite once against the minimum supported Home Assistant
and Python versions. This keeps the compatibility guarantee without duplicating
the same test run against a second dependency environment on every change.

For v0.7.44, the complete suite passes on Home Assistant 2024.4.4
with Python 3.12 and Home Assistant 2026.8.3 with Python 3.14. Tests
cover the transport with simulated Bluetooth devices and proxy slots.

## Issue 53: passive Bluetooth scanner updates

Run the 21 regression cases with:

```bash
.venv-test/bin/python -m pytest tests/test_advertisement_callbacks.py -q
```

The tests feed advertisements through Home Assistant's Bluetooth manager.
They cover cached setup, passive and mixed scanner sources, session tracking,
live GATT priority, and charger reachability. Bluetooth transport is simulated.

## Charger transaction deadlines

`test_charger_request_deadlines.py` covers the v0.7.40 fix with 19 cases,
including stalled writes, a shared write/response budget, queued post-session
reads, cancellation, response cleanup and late unrelated responses. The
1.5-second transaction budget starts after acquiring the shared request lock.

```bash
.venv-test/bin/python -m pytest tests/test_charger_request_deadlines.py -q
```

## Issue 58 and BLE connection ownership

```bash
.venv-test/bin/python -m pytest tests/test_charger_discovery_retry.py tests/test_connection_ownership.py -q
```

The restart regression replays two cached charger advertisements through
Home Assistant's Bluetooth manager, shares a simulated proxy slot, and
uses 850 ms charger responses. The charger frames stay unchanged and the
handle advertises only selection menu. Both bridges identify their charger,
and timer, pacer, pressure and battery update through the correct charger.

Other cases cover transient Bluetooth failures, independent retry timing and
the hard cap, a disappearing route, serialization with eager tasks, foreign
and placeholder identities, re-pairing during and after discovery, and
shutdown. The three-charger contention matrix covers every replay order, one
or two shared proxy slots, and 850 ms or 1.4-second replies. One route recovers
without a new advertisement.

The ownership tests cover old callbacks, queued requests, idle release and
diagnostic retention, contradictory native status, loss during live reads,
late startup samples across brushing sessions, source handoffs, deferred
session recovery and the valid later-clock case. The tests run the production
state machines over virtual deadlines and simulated BLE transport.

Connected-route tests expire Home Assistant's scanner history during a
working link and exercise re-verification on an identical returning frame.
Discovery also checks a returning previously foreign charger, an identity
read interrupted by route loss and a stale timeout after the same address
returns. Request tests reject early, unrelated and duplicate replies while
accepting a valid notification during the delimiter write itself. An empty
status frame leaves the request open for a valid status byte.
Slow-reconnect cases check that continuing timers count once, a reset counts
as a new session, and normal battery reads still run after timer recovery.
They include 1.4-second responses and a lost first timer reply.
Live transport cases cover short and persistent silence, failed passthrough
with or without native status, and silent initial probes and reconnects.
Recovery checks that the displayed timer does not inflate the saved duration
or prevent a genuine continuation from counting once.

Setup-deadline cases stall each charger notification subscription and check
both the next queued charger and the failing candidate's retry. Direct-mode
cases cover mandatory and optional subscription timeouts and a stalled first
startup read. A slow, responsive charger can still finish both subscriptions
under their individual deadlines.

## Issue 39: battery refresh regression coverage

`test_battery_refresh.py` was added before the production fix, with 21 failing
bug reproductions. The fix and subsequent review now satisfy those cases and
the additional connection, retry and timestamp checks. No cases are skipped or
marked as expected failures. Run the focused cases with:

```bash
.venv-test/bin/python -m pytest tests/test_battery_refresh.py
```

The tests use Home Assistant's Bluetooth manager for callback replay and
unchanged-advertisement filtering. A simulated clock advances real coordinator
tasks through the normal 20-second pause grace, 30-second record delay,
60-second connection cooldown and six-hour maintenance interval. It does not
replace the session tracker or sync scheduler, or globally change asyncio's
clock. Transport and storage are faked; no physical Bluetooth connection is made.

Coverage includes both summary states and the complete off-dock sequence;
idle/charging and direct-mode compatibility; confirmed brushing in selection
menu; charger ownership and scanner connectability; unchanged-payload retries;
sleep, contention, changes while waiting for the connection lock, unload,
cancelled reads, service-cache recovery and stalled-read deadlines;
current versus retained battery;
empty/invalid status and genuine zero percent; protocol 6/7/8 and unknown-record
compatibility; optional-read failures; restoration races; and normal-grace
pause/resume and consecutive-session accounting. Retries enforce the same
60-second minimum interval as the first connection. Quiet history expires
after 15 seconds; each maintenance read has a three-second deadline and the
whole connection attempt has a 20-second deadline, with a separate bounded
three-second disconnect. These bounds do not apply to the direct mode's ongoing
notification connection.

An existing session-record test now expects an unknown battery measurement
time when RTC is missing. The retained percentage remains useful, but reading
it again must not invent fresh metadata or overwrite a newer current sample.

The ten-second prompt-refresh assertion is a software scheduling contract with
instant fake transport, not a hardware latency guarantee. These tests do not
establish Bluetooth connection timing, phone/charger contention behavior, or
whether all firmware provides FF05 in its summary window. Hardware validation
remains required. Broader FF29 identity, RTC and asynchronous generation races
are separate session-recovery work; the battery fix must not bring record
application forward merely to make a battery test pass.

## Issue 38: passive sessions and protocol-6 captures

The tests in `test_battery_refresh.py` also exercise missed passive stops,
a lone stale timer sample versus a coherent new timer, and a menu wake during
pause grace. They reproduce a short session with pre-run sectors and a face
that changes at the quiet-state transition. Capture tests verify that an absent
clock or a placeholder record keeps the retry pending, FF05 still publishes,
and debug logs distinguish an incomplete pair, a timed-out stage and a stale
quiet window. These tests simulate Bluetooth transport. The protocol-6 field
layout still requires correlated reads from a physical brush.

## Issue 49: disconnected client cleanup

`test_disconnect_cleanup.py` began with eight cases for brush and iO Sense
charger lifecycles: six failed before the production fix and the two
connected-client controls passed. All 37 cases now pass, without skips or
expected-failure markers.

```bash
.venv-test/bin/python -m pytest tests/test_disconnect_cleanup.py -q
```

Coverage includes explicit cleanup after remote loss, remote-disconnect
callbacks, stale-client replacement, waiting for in-flight cleanup before
reconnect, late and duplicate callbacks, repeated remote losses, callback
handoff from another thread, intentional disconnect without reconnect,
cancellation during cleanup or connection setup, unload while cleanup is
pending, cleanup errors, connections returned after remote loss or shutdown,
eager Home Assistant task creation, and charger session ownership cleanup.

The fake client keeps connectivity separate from resource ownership and only
releases resources when `disconnect()` is awaited. This verifies explicit
cleanup calls, not actual BlueZ notification duplication on physical hardware.
The minimum environment pins bleak 0.21.1, whose BlueZ `_cleanup_all()` already
closes the bus on remote loss. Bleak 3.0.2 instead leaves bus closure to
`disconnect()`, matching the fake's lifecycle. The manifest has no bleak pin,
so Home Assistant's installed version matters.

The linked [Sonicare fix](https://github.com/mtheli/philips_sonicare_ble/commit/76e32f1)
uses the same three cleanup paths and removes an outer five-second timeout
that could cancel cleanup before bleak closes its bus. Sonicare issue #45 ends
with the reporter confirming no duplicates after v0.32.1. The brush and charger
cleanup tasks likewise have no short outer timeout and survive cancellation
of their callers; reconnect and unload await them. Temporary history/battery
connections retain their existing separate deadline policy.

## Issue 45: unavailable FF0A capability contracts

`test_ff0a_capability.py` started with 26 cases, including three failing
reproductions. All 47 cases now pass without skips or expected-failure markers.
The same focused suite passes in the local Home Assistant 2026.8.3 / bleak
3.0.2 / Python 3.14 environment. The shared Bluetooth advertisement fixture
accepts both the minimum and newer BLEDevice/service-info constructor forms.

```bash
.venv-test/bin/python -m pytest tests/test_ff0a_capability.py -q
```

A matching FF0A-not-found error must be corroborated by the same service table
containing state and battery but not FF0A before further reads are suppressed.
Negative evidence belongs to that client/table only. Generic errors, timeouts,
empty/off results and uncertain discovery preserve the bounded retry budget.
A rebuilt connection/table, in-place addition of FF0A, or an actual FF0A
notification invalidates previous absence evidence. Discarded clients are not
retained by the capability cache.

Coverage includes both capture paths, repeated capture on the same live
client, initial-read suppression, misleading error text, lazy/incomplete/
erroring/missing lookup APIs, service changes during an outstanding read,
reused and replacement client objects, no inheritance of a previous face,
delayed old-session reads, independent battery publication, and later
retained-record recovery. Tests use the production tracker and retry delays
with a virtual clock, simulated transport, and bleak's service-lookup container.

These tests cover software decisions, not firmware capability validation or
physical service-discovery reliability. Hardware observations remain necessary
to establish support on a particular handle/firmware.
