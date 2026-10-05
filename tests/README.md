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
