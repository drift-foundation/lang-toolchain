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

`$DRIFT_TMP_ROOT` is the canonical env var. If set, all Drift tooling
honors it verbatim — a user override always wins. If unset, the helper
picks a `session-$PID-$timestamp` dir and writes the chosen value back
into `os.environ` so child processes inherit it. WHERE that session dir
lands depends on the entrypoint:

```
<repo>/build/tmp/session-$PID-$timestamp/     # repo gates (default)
/tmp/drift-$USER/session-$PID-$timestamp/     # direct/non-repo tooling
```

**Repo gate entrypoints** (root `conftest.py`, the e2e runners,
`tools/deps_check.py`) pass `base=<repo>/build/tmp` to `session_root()`,
so full gates scratch on **repo-local disk**, not tmpfs. Rationale: a
full-suite run writes enough `.ll` scratch, objects, package builds, and
pytest `tmp_path` trees to exhaust a memory-backed `/tmp` — the
2026-07-08 full gate died on ENOSPC mid-compile exactly this way. The
`session-*` layout (and thus janitor semantics) is identical under
either root; `build/` is gitignored.

Tooling invoked outside the repo gates (or anything calling
`session_root()` with no `base`) keeps the legacy `/tmp/drift-$USER/`
namespace.

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

## Python helper (`lang/test_support/drift_tmp.py`)

```python
from lang.test_support.drift_tmp import (
    session_root,    # -> Path
    drift_tempdir,   # context manager wrapping TemporaryDirectory(dir=...)
    drift_mkdtemp,   # mkdtemp(dir=...) — caller owns cleanup
    drift_mkstemp,   # mkstemp(dir=...)
)
```

Pytest tests should keep using `tmp_path` / `tmp_path_factory` — the
top-level `conftest.py` sets `PYTEST_DEBUG_TEMPROOT=$DRIFT_TMP_ROOT/pytest`
and `tempfile.tempdir = session_root()` so these land inside the
Drift namespace automatically.  Non-pytest tooling (deploy steps, ad
hoc CLIs) MUST use the explicit `dir=session_root()` form — conftest.py
does not run for those.

## Drift helper (`std.env.drift_tmp_path`)

Compiled Drift programs do NOT inherit Python's `tempfile.tempdir` or
pytest's `PYTEST_DEBUG_TEMPROOT` — those redirections live in the
Python harness, not in the compiled binary.  Drift code that needs
scratch storage must derive the path from `$DRIFT_TMP_ROOT` itself:

```drift
import std.env as env;
import std.io as io;

pub fn main() nothrow -> Int {
    val path = env.drift_tmp_path("my_test_output.bin");
    val w = io.file_builder(path).read(false).write(true)...
    ...
}
```

`env.drift_tmp_path(name)` returns `$DRIFT_TMP_ROOT/<name>` when the
env var is set (the normal case under pytest / the e2e runner / any
process launched via Drift tooling) and falls back to `/tmp/<name>`
for direct manual invocation outside a Drift session.

## Static audit

`lang/tests/test_tmp_root_compliance.py` enforces this convention in
CI.  It fails on:

- Any `/tmp/` reference in active source (Python, Drift, shell,
  justfile) — scans full lines, not just quoted substrings, so
  heredocs / embedded shell commands / docstrings are covered.
- `tempfile.mkdtemp()`, `mkstemp()`, `TemporaryDirectory()`, and
  `NamedTemporaryFile()` calls without a `dir=` keyword.
- Any `tempfile.mktemp()` call — forbidden outright (deprecated;
  TOCTOU race between path return and file create, and no `dir=`
  API surface).

There is **no file-level allow-list** — per-line markers are the
only opt-out.  Earlier drafts had a file-level allow-list which was
too coarse: a future real /tmp write inside an "exempt" file would
slip through.  Per-line markers force the contributor to acknowledge
each specific safe literal, and a reviewer can see exactly which
lines are exempted in the diff.

**Per-line opt-out marker** (comment-syntax-agnostic — works in
Python `#`, Drift `//`, shell `#`, etc.):

```
# drift-tmp-root-audit: allow <reason>
// drift-tmp-root-audit: allow <reason>
```

Use **only** for genuinely safe cases:

| Case | Example |
|---|---|
| Documentation / docstring prose | `--manifest /tmp/foo.json` in a README |
| Mock return value, never written | `return Path("/tmp/fake.a")` in a test |
| Path-rejection negative test | `assert raises(ValueError, abs_path="/tmp/x.drift")` |
| The namespace origin itself | `drift_tmp.py`'s default session-root construction |
| The janitor's sweep target | `tools/drift_janitor.sh`'s hard-coded base |

Any other use is a regression.

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

Repo-local gate sessions (`build/tmp/session-*`) are NOT swept by the
janitor — they are disk-backed, so stale ones are a disk-space concern,
not a wedge-the-box concern. Reclaim them with `rm -rf build/tmp` (or
any build-dir clean); nothing outside the current run holds state
there.

## Related env vars (unchanged)

- `DRIFT_CACHE_DIR` — persistent package cache (`~/.cache/drift/`).
- `DRIFT_RUNTIME_LIB_CACHE_DIR` — persistent runtime archive cache.

These are user-scoped and survive across sessions on purpose.
`DRIFT_TMP_ROOT` is the **ephemeral** companion for per-session scratch.
