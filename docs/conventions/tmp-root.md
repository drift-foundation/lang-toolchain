# `DRIFT_TMP_ROOT` — janitor-safe session scratch

## Invariant

> Any artifact a Drift process writes under `/tmp` must live inside a
> predictable Drift-owned namespace, so a janitor can later sweep it
> regardless of whether the process exited cleanly, was OOM-killed, or
> was SIGKILLed.

The motivation is **not** graceful cleanup. `/tmp` is tmpfs (RAM-backed)
on most Linux setups, and cleanup hooks — `try/finally`,
`tempfile.TemporaryDirectory`, shell `trap` — do not run on `SIGKILL` or
OOM-kill. A predictable namespace is the only reliable safety net.

## Namespace

```
/tmp/drift-$USER/session-$PID-$timestamp/
```

`$DRIFT_TMP_ROOT` is the canonical env var. If set, all Drift tooling
honors it. If unset, the helper picks the path above and writes the
chosen value back into `os.environ` so child processes inherit it.

## Rules

1. Any `/tmp` path a Drift process writes **must** be under
   `$DRIFT_TMP_ROOT`. Pass `dir=session_root()` to
   `tempfile.TemporaryDirectory` / `mkdtemp` / `mkstemp`, or use the
   `drift_*` wrappers (below).
2. `tempfile.TemporaryDirectory()` (no `dir=`) is **fine in practice**
   only because the top-level `conftest.py` sets `tempfile.tempdir`
   and `TMPDIR` to the session root — but write new code with an
   explicit `dir=session_root()` for greppability.
3. Pytest tests should keep using `tmp_path` / `tmp_path_factory`. The
   `conftest.py` sets `PYTEST_DEBUG_TEMPROOT=$DRIFT_TMP_ROOT/pytest`
   so these land inside the Drift namespace automatically.
4. Hard-coded `/tmp/foo` paths are forbidden. Use `session_root()`.
5. Repo-local build dirs (`build/tests/...`) are fine as-is — they
   are not the tmpfs concern this convention targets.
6. Long-lived artifacts (post-mortem dumps, profiling output worth
   keeping) belong in the repo (`work/`, `build/`), not under
   `$DRIFT_TMP_ROOT`.

## Helper (`lang/test_support/drift_tmp.py`)

```python
from lang.test_support.drift_tmp import (
    session_root,    # -> Path
    drift_tempdir,   # context manager wrapping TemporaryDirectory(dir=...)
    drift_mkdtemp,   # mkdtemp(dir=...) — caller owns cleanup
    drift_mkstemp,   # mkstemp(dir=...)
)
```

## Setup (interactive shells, agents, downstream projects)

```bash
export DRIFT_TMP_ROOT="/tmp/drift-$USER/session-$$-$(date +%s)"
mkdir -p "$DRIFT_TMP_ROOT"
# trap-based cleanup is welcome but not load-bearing — the janitor is
# the actual safety net.
```

For pytest invocations driven outside this repo, also set:

```bash
export PYTEST_DEBUG_TEMPROOT="$DRIFT_TMP_ROOT/pytest"
export TMPDIR="$DRIFT_TMP_ROOT"
```

## Janitor

`tools/drift_janitor.sh` sweeps session dirs older than 6 hours by
default. Dry-run is the default:

```bash
# preview (no deletion)
tools/drift_janitor.sh

# apply
tools/drift_janitor.sh --apply

# tune the age threshold
tools/drift_janitor.sh --minutes 1440 --apply   # 24h
```

The underlying find pattern is hard-coded to `session-*` under
`/tmp/drift-$USER/`, with `-xdev -prune`, so the janitor cannot
accidentally touch anything outside the Drift namespace.

## Related env vars (unchanged)

- `DRIFT_CACHE_DIR` — persistent package cache (`~/.cache/drift/`).
- `DRIFT_RUNTIME_LIB_CACHE_DIR` — persistent runtime archive cache.

These are user-scoped and survive across sessions on purpose.
`DRIFT_TMP_ROOT` is the **ephemeral** companion for per-session scratch.
