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
