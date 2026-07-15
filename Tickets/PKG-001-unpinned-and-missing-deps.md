# PKG-001 — Dependencies unpinned; test dependencies missing

- **Severity:** Medium
- **Area:** Packaging / Reproducibility
- **File:** `requirements.txt`, `pytest.ini`

## What's wrong

`requirements.txt`:

```
fastapi>=0.115.0
uvicorn[standard]>=0.34.0
websockets>=14.0
pyModeS>=2.21,<3.0
httpx>=0.27.0
```

- **Runtime deps are unpinned** — open-ended `>=` floors with no upper bound
  (except pyModeS). Builds are non-reproducible; a future FastAPI / uvicorn /
  websockets release can silently change behavior or break the app. (The
  pyModeS `<3.0` cap is correct and important — 3.x removes the functional API
  that `app/decoder.py` relies on.)
- **Test deps are absent.** `pytest.ini` sets `asyncio_mode = auto`, which
  requires both `pytest` **and** `pytest-asyncio`, but neither is listed
  anywhere. The suite only runs inside the committed `venv/`; on a clean
  interpreter `python -m pytest` fails with `No module named pytest`.

## Why it matters

Non-reproducible builds mean "works today, breaks on next `pip install`" with no
changed source. Missing test deps mean CI or a new contributor can't run the
suite without reverse-engineering what's needed from `venv/`.

## Fix

- Pin runtime deps with compatible-release specifiers (`~=`) or, better, a
  lockfile (`pip-compile` / `uv lock` / `poetry.lock`) so installs are
  reproducible.
- Add the test deps — a `requirements-dev.txt` or
  `[project.optional-dependencies].test` listing at least `pytest` and
  `pytest-asyncio` (pinned) — and document `pip install -r requirements-dev.txt`
  in the README.
