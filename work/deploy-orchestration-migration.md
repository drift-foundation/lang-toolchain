# Deploy Orchestration Migration: Shell → Python

Refactor the compiler deploy pipeline from shell-script orchestration to a
Python-owned orchestration model with a single source of truth for deploy
behavior and metadata.

## Motivation

The `stdlib_dep.txt` / `--dep` fix (0.27.72) exposed a structural problem:
deploy metadata and semantic logic is split across shell and Python.  Shell
scripts compute deploy metadata (stdlib dep spec, version strings, manifest
JSON, runtime variant discovery) while Python entry points independently
re-derive the same information.  This duplication is brittle and makes the
deploy contract harder to reason about.

Deploy is no longer thin command sequencing.  It carries workflow logic,
metadata contracts, package semantics, signing/trust behavior, smoke
validation, and multiple artifact types.  Shell is the wrong long-term home
for this.

## Current State Inventory

### Shell scripts (`tools/deploy/`)

| Script | Lines | Role | Semantic logic? |
|--------|-------|------|-----------------|
| `deploy.sh` | 204 | **Orchestrator**: arg parsing, version metadata extraction (calls Python for `DRIFTC_VERSION`/`ABI_VERSION`), prerequisite checks, runtime archive builds, staging directory setup, step sequencing, cleanup | **Yes**: version metadata computation, path normalization, signing key validation, prerequisite checks |
| `step_build_pex.sh` | 77 | Build PEX --scie eager for `bin/driftc` | **Some**: reads `requirements.txt` for pinned dep versions, detects Python version |
| `step_build_deploy_pex.sh` | 95 | Build PEX --scie eager for `bin/drift` | **Some**: same as above, plus stages `tools.drift_deploy` package into PEX |
| `step_bundle.sh` | 222 | Stage compiler sources, runtime archives, docs, examples | **Some**: knows which directories to copy, which file extensions matter, generates README/examples content |
| `step_stdlib_pkg.sh` | 92 | Build, sign, install stdlib package + core trust store | **Yes**: computes stdlib build flags, invokes signing, generates `stdlib_dep.txt`, invokes `gen_trust_store.py` |
| `step_smoke.sh` | 41 | Compile + run smoke test | **Some**: validates exit code and stdout against expected values |
| `step_publish.sh` | 80 | Atomic publish + symlink switch | **Yes**: generates `manifest.json` with version/ABI/build metadata, atomic rename with rollback |
| `driftc-wrapper.sh` | 49 | Deployed driftc shell wrapper (legacy, pre-PEX) | **Yes**: reads `stdlib_dep.txt`, injects `--dep` |

### Python files (`tools/deploy/`)

| File | Role | Semantic logic? |
|------|------|-----------------|
| `pex_entry.py` | Deployed driftc PEX entry point | **Yes**: resolves deploy tree, configures sys.path, peeks stdlib package manifest to derive `--dep` spec, injects `--package-root` + `--dep` |
| `deploy_pex_entry.py` | Deployed drift CLI PEX entry point | **Some**: resolves deploy tree, dispatches to `drift deploy` or `lang.drift.cli` |
| `gen_trust_store.py` | Generate core trust store from sidecar | **Yes**: validates sidecar format, extracts keys, builds trust store JSON |
| `vendor_python_deps.py` | Vendor Python distributions | **Minimal**: metadata collection, file copying |

### Python package (`tools/drift_deploy/`)

| File | Role |
|------|------|
| `drift_deploy.py` | **User-facing** `drift deploy` orchestrator for downstream packages/apps |
| `manifest.py` | drift-manifest.json loading/validation |
| `resolver.py` | Constraint-aggregation + version resolution |
| `lockfile.py` | drift-lock.json read/write/verify |
| `semver.py` | Semver parsing + constraints |
| `sidecar.py` | App provenance .meta.json |
| `staged_trust.py` | Staged trust overlay for smoke |

### Key observation: two deploy pipelines

There are **two distinct deploy pipelines** in this codebase:

1. **Compiler deploy** (`deploy.sh` → step scripts): Builds and publishes a
   complete Drift distribution (compiler + runtime + stdlib).  Invoked via
   `just deploy`.

2. **Package deploy** (`drift_deploy.py`): Builds and publishes downstream
   Drift packages/apps from `drift-manifest.json`.  Invoked via `drift deploy`
   (or `PYTHONPATH=. python -m tools.drift_deploy.drift_deploy`).

This plan covers **pipeline 1** (compiler deploy).  Pipeline 2 is already
Python-native and is not in scope.

## Semantic Metadata Duplication (the specific problem)

### stdlib dep spec — 3 producers today

1. `step_stdlib_pkg.sh` writes `stdlib_dep.txt` containing `std@${DRIFTC_VERSION}`
2. `pex_entry.py` peeks the stdlib `.dmp`/`.zdmp` manifest to derive `std@<version>`
3. `driftc-wrapper.sh` reads `stdlib_dep.txt`

The canonical fact is: "the stdlib package has id=std and version=X".  This
should be computed once, in Python, from the package manifest.  The shell
script should not be constructing it from `DRIFTC_VERSION`.

### Version metadata — shell calls Python, then constructs shell vars

`deploy.sh` lines 140-148 call Python to extract `DRIFTC_VERSION` and
`ABI_VERSION`, then constructs `VERSION_DIR`, `GIT_COMMIT`, `BUILD_UTC`, etc.
as shell variables that propagate via env to all steps.  `step_publish.sh`
uses these to generate `manifest.json` via heredoc.

### Runtime variant discovery

`deploy.sh` lines 174-181 call Python to build runtime archives.
`step_publish.sh` lines 27-35 rediscover variants by testing file existence.
`step_bundle.sh` lines 62-70 copies by hardcoded variant list.  The variant
list (`default debug asan alloc_track optimized`) appears in three places.

### Trust store generation

`step_stdlib_pkg.sh` invokes `gen_trust_store.py` as a subprocess.  This is a
Python-calling-shell-calling-Python round-trip.

## Target Module Layout

```
tools/deploy/
  deploy.py              — NEW: Python orchestrator, the sole deploy entrypoint
  steps/
    __init__.py
    metadata.py          — version, ABI, git, build metadata (single source of truth)
    runtime.py           — runtime archive builds
    pex.py               — PEX --scie eager builds (driftc + drift CLI)
    bundle.py            — compiler sources, runtime archives, docs, examples
    stdlib.py            — stdlib build + sign + trust store + dep spec
    smoke.py             — smoke test execution + validation
    publish.py           — atomic publish + symlink + manifest.json
  pex_entry.py           — UNCHANGED (deployed entry point, reads dep spec from manifest)
  deploy_pex_entry.py    — UNCHANGED (deployed entry point)
  driftc-wrapper.sh      — KEPT (deployed artifact consumed by non-PEX installations)
  smoke_test.drift       — UNCHANGED
  vendor_python_deps.py  — UNCHANGED (utility, not in critical path)
```

### End state: what is deleted

All shell orchestration scripts are deleted.  `deploy.sh` is removed entirely
— not reduced to a wrapper, not kept for compatibility.  Python is the only
deploy entrypoint.  `gen_trust_store.py` is inlined into `steps/stdlib.py`.

The only remaining shell file is `driftc-wrapper.sh`, which is a **deployed
runtime artifact** (shipped inside the distribution, consumed by non-PEX
installations at runtime).  It is not build-time orchestration and is not
in scope for this migration.

### What callers use after migration

| Caller | Before | After |
|--------|--------|-------|
| `just deploy` | `tools/deploy/deploy.sh {{ARGS}}` | `PYTHONPATH=. .venv/bin/python3 tools/deploy/deploy.py {{ARGS}}` |
| `just lang-codegen-test-pex` | `bash tools/deploy/step_build_pex.sh` etc. | `PYTHONPATH=. .venv/bin/python3 -c "from tools.deploy.steps.pex import build_driftc_pex; ..."` or calls deploy.py with a `--steps` subset flag |
| CI (if any) | `tools/deploy/deploy.sh` | `python3 tools/deploy/deploy.py` |
| Docs (`doc/README.md`) | References `deploy.sh` and 6 step scripts | References `deploy.py` and Python step modules |

### CLI design: clean Python-native, not shell-compatible

The Python CLI is designed for clarity, not for reproducing `deploy.sh` quirks.
No effort is spent on getopt compatibility, positional dest, or `KEY=VALUE`
forms.  All callers (just, tests, docs) are updated to the new interface.

```
python3 tools/deploy/deploy.py --dest <DEST> [--python <PYTHON>]
```

Standard argparse.  `--dest` is required (no positional alternative).
`--python` is optional.  No `DEST=path` or `PYTHON=path` forms.  No bare
`--` prefix stripping.  No `~` expansion (Python `Path.expanduser()` handles
this natively).

This is an internal tool with no external API contract.  The CLI shape should
be whatever is cleanest in Python.

## Single Source of Truth: stdlib dep spec

**Before** (3 producers):
```
step_stdlib_pkg.sh   →  echo "std@${DRIFTC_VERSION}" > stdlib_dep.txt
pex_entry.py         →  peek_package_id_and_version(*.dmp) → "std@X"
driftc-wrapper.sh    →  cat stdlib_dep.txt
```

**After** (1 producer, 2 consumers):
```
steps/stdlib.py      →  builds stdlib, signs it, peeks manifest, writes stdlib_dep.txt
                         (single canonical producer using peek_package_id_and_version)
pex_entry.py         →  reads stdlib_dep.txt OR peeks manifest (deployed runtime, unchanged)
driftc-wrapper.sh    →  reads stdlib_dep.txt (deployed runtime, unchanged)
```

The key change: `stdlib_dep.txt` is written by reading the **actual built
package manifest**, not by string-concatenating the version.  `steps/stdlib.py`
calls `peek_package_id_and_version()` on the just-built `.dmp`, same as
`pex_entry.py` does at runtime.  One code path, one truth.

## Detailed Step Design

### `steps/metadata.py` — Deploy metadata

```python
@dataclass
class DeployMetadata:
    driftc_version: str      # from driftc_versions.py
    abi_version: int         # from driftc_versions.py
    git_commit: str          # short
    git_commit_full: str     # full SHA
    build_utc: str           # ISO 8601
    host_platform: str       # uname -s
    host_arch: str           # uname -m
    version_dir: str         # "drift-{version}+abi{abi}"

def load_deploy_metadata(repo_root: Path) -> DeployMetadata: ...
```

Replaces: `deploy.sh` lines 140-155 (Python-via-shell version extraction).

### `steps/runtime.py` — Runtime archive builds

```python
RUNTIME_VARIANTS = ("default", "debug", "asan", "alloc_track", "optimized")

def build_runtime_archives(repo_root: Path, clang: str) -> list[str]: ...
```

Replaces: `deploy.sh` lines 173-181.  Single source for variant list.

### `steps/pex.py` — PEX builds

```python
def read_pinned_version(repo_root: Path, package: str) -> str: ...
def detect_python_version(venv: Path) -> str: ...
def build_driftc_pex(repo_root: Path, dist: Path) -> Path: ...
def build_drift_pex(repo_root: Path, dist: Path) -> Path: ...
```

Replaces: `step_build_pex.sh` (77 lines), `step_build_deploy_pex.sh` (95 lines).
Still invokes `pex` CLI via subprocess — that doesn't change.

Exported for use by `lang-codegen-test-pex` justfile recipe, which needs
to build PEX + bundle without running the full deploy pipeline.

### `steps/bundle.py` — Bundle compiler sources

```python
COMPILER_PACKAGES = ("lang/driftc", "lang/drift", "lang/codegen",
                     "lang/compiler_infra", "lang/language_runtime")
SOURCE_EXTENSIONS = (".py", ".lark")
NATIVE_EXTENSIONS = (".c", ".h", ".S")

def bundle_compiler(repo_root: Path, dist: Path) -> None: ...
def bundle_runtime_archives(repo_root: Path, dist: Path, variants: list[str]) -> None: ...
def bundle_docs_and_examples(dist: Path) -> None: ...
```

Replaces: `step_bundle.sh` (222 lines, ~half is README heredoc).

### `steps/stdlib.py` — Stdlib build + sign + trust

```python
def build_stdlib_package(repo_root: Path, stage: Path, version: str) -> Path: ...
def sign_stdlib(repo_root: Path, dmp_path: Path) -> Path: ...
def generate_core_trust_store(sig_path: Path, out_path: Path, namespaces: list[str]) -> None: ...
def install_stdlib(dmp: Path, sig: Path, dist: Path) -> None: ...
def write_stdlib_dep(dmp: Path, dist: Path) -> None:
    """Single source of truth: peek actual manifest, write stdlib_dep.txt."""
    from lang.driftc.packages.dmir_pkg_v0 import peek_package_id_and_version
    result = peek_package_id_and_version(dmp)
    dep_spec = f"{result[0]}@{result[1]}"
    (dist / "lib" / "stdlib" / "stdlib_dep.txt").write_text(dep_spec + "\n")
```

Replaces: `step_stdlib_pkg.sh` (92 lines) + `gen_trust_store.py` (94 lines).
`gen_trust_store.py` logic is inlined — it's only used here.

### `steps/smoke.py` — Smoke test

```python
def run_smoke_test(dist: Path, repo_root: Path, stage: Path) -> None: ...
```

Replaces: `step_smoke.sh` (41 lines).

### `steps/publish.py` — Publish + manifest

```python
def generate_manifest(dist: Path, meta: DeployMetadata, variants: list[str]) -> None: ...
def publish_atomic(dist: Path, dest: Path, version_dir: str) -> None: ...
def switch_current_symlink(dest: Path, version_dir: str) -> None: ...
```

Replaces: `step_publish.sh` (80 lines).

### `deploy.py` — Main orchestrator

```python
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    meta = load_deploy_metadata(repo_root)
    validate_prerequisites(repo_root)

    # Build runtime archives
    variants = build_runtime_archives(repo_root, clang)

    # Staging
    with staging_dir(dest) as (stage, dist):
        build_driftc_pex(repo_root, dist)
        build_drift_pex(repo_root, dist)
        bundle_compiler(repo_root, dist)
        bundle_runtime_archives(repo_root, dist, variants)
        bundle_docs_and_examples(dist)

        dmp, sig = build_and_sign_stdlib(repo_root, stage, dist, meta)
        write_stdlib_dep(dmp, dist)
        generate_core_trust_store(sig, dist, ...)

        run_smoke_test(dist, repo_root, stage)

        generate_manifest(dist, meta, variants)
        publish_atomic(dist, dest, meta.version_dir)
        switch_current_symlink(dest, meta.version_dir)
```

## Migration Phases

### Phase 1: Python orchestrator + metadata

1. Create `tools/deploy/steps/` package with `__init__.py`
2. Create `steps/metadata.py` — `DeployMetadata` extraction
3. Create `deploy.py` — new Python orchestrator that initially delegates to
   existing shell step scripts via subprocess (transitional)
4. Update `justfile`: `deploy` recipe calls `deploy.py` directly
5. Delete `deploy.sh`
6. Verify: `just deploy` produces identical output

**Test strategy**: Run `just deploy`, diff staged tree against pre-migration
deploy.  Binary-identical outputs.

### Phase 2: Move PEX + bundle steps

1. Create `steps/pex.py` — port `step_build_pex.sh` and `step_build_deploy_pex.sh`
2. Create `steps/bundle.py` — port `step_bundle.sh`
3. Update `deploy.py` to call Python steps instead of shell scripts
4. Update `justfile` `lang-codegen-test-pex` recipe to call Python steps
5. Delete `step_build_pex.sh`, `step_build_deploy_pex.sh`, `step_bundle.sh`

**Test strategy**: Same diff-based verification.  PEX builds are deterministic
for same inputs.  `just lang-codegen-test-pex` must pass.

### Phase 3: Move stdlib + trust store

1. Create `steps/stdlib.py` — port `step_stdlib_pkg.sh` + inline `gen_trust_store.py`
2. stdlib dep spec: single canonical path via `peek_package_id_and_version()`
3. Update `deploy.py`
4. Delete `step_stdlib_pkg.sh`, `gen_trust_store.py`

**Test strategy**: Verify `stdlib_dep.txt` content matches, trust store JSON
matches, smoke passes.

### Phase 4: Move smoke + publish

1. Create `steps/smoke.py` — port `step_smoke.sh`
2. Create `steps/publish.py` — port `step_publish.sh`
3. Move runtime variant list to `steps/runtime.py` (single definition)
4. Update `deploy.py`
5. Delete `step_smoke.sh`, `step_publish.sh`

**Test strategy**: Full `just deploy` + verify deployed binary works.

### Phase 5: Cleanup + test coverage

1. Add unit tests for `steps/metadata.py`, `steps/stdlib.py`, `steps/pex.py`
2. Update generated `doc/README.md` (inside bundle) to reference Python steps
3. Audit and update any test files that invoke shell deploy scripts directly
4. Verify no remaining references to deleted shell scripts in codebase

## Files to Delete

| File | Phase | Replaced by |
|------|-------|-------------|
| `deploy.sh` | 1 | `deploy.py` |
| `step_build_pex.sh` | 2 | `steps/pex.py` |
| `step_build_deploy_pex.sh` | 2 | `steps/pex.py` |
| `step_bundle.sh` | 2 | `steps/bundle.py` |
| `step_stdlib_pkg.sh` | 3 | `steps/stdlib.py` |
| `gen_trust_store.py` | 3 | `steps/stdlib.py` |
| `step_smoke.sh` | 4 | `steps/smoke.py` |
| `step_publish.sh` | 4 | `steps/publish.py` |

**Total: 8 shell/Python files deleted.**

## Files Kept

| File | Reason |
|------|--------|
| `driftc-wrapper.sh` | Deployed runtime artifact (not build-time orchestration) |
| `pex_entry.py` | Deployed runtime entry point (PEX) |
| `deploy_pex_entry.py` | Deployed runtime entry point (drift CLI PEX) |
| `vendor_python_deps.py` | Independent utility |
| `smoke_test.drift` | Test source file |

## Callers to Update

### `justfile`

**`deploy` recipe** (line 391):
```
# Before:
deploy *ARGS:
	tools/deploy/deploy.sh {{ARGS}}

# After:
deploy *ARGS:
	PYTHONPATH=. ./.venv/bin/python3 tools/deploy/deploy.py {{ARGS}}
```

Invocation changes accordingly:
```bash
# Before:
just deploy -- --dest="~/opt/drift" --python="$PWD/.venv/bin/python3"

# After:
just deploy --dest ~/opt/drift --python .venv/bin/python3
```

No `--` prefix needed (Python argparse, not getopt).  No quoting gymnastics.

**`lang-codegen-test-pex` recipe** (lines 201-203):
```bash
# Before:
bash tools/deploy/step_build_pex.sh
bash tools/deploy/step_build_deploy_pex.sh
bash tools/deploy/step_bundle.sh

# After:
PYTHONPATH=. ./.venv/bin/python3 -c "
from tools.deploy.steps.pex import build_driftc_pex, build_drift_pex
from tools.deploy.steps.bundle import bundle_compiler, bundle_runtime_archives
from pathlib import Path
import os
repo = Path('.').resolve()
dist = Path(os.environ['DIST'])
build_driftc_pex(repo, dist)
build_drift_pex(repo, dist)
bundle_compiler(repo, dist)
bundle_runtime_archives(repo, dist)
"
```

(Or: expose a `deploy.py --steps pex,bundle` subset mode to avoid inline
Python in the justfile.  Decision deferred to implementation.)

### `deploy-print-env` recipe (line 395)

No change needed — reads from deployed tree, does not invoke deploy scripts.

### Tests

**`tools/drift_deploy/test_deploy.py`**:
- `TestDeployPexEntry.test_step_build_deploy_pex_exists` — references
  `step_build_deploy_pex.sh`.  Update to verify `steps/pex.py` instead.

**`lang/tests/driver/test_deploy_pex_scie.py`**:
- Verify it does not invoke shell step scripts directly.  (Current code
  invokes the PEX binary, not shell steps — likely no change needed.)

### Documentation

**Generated `doc/README.md`** (inside `steps/bundle.py`):
- Section "Deploy semantics" references 6 shell step scripts.
- Update to describe Python step modules.

## Regression Strategy

Each phase uses the same verification:

1. **Binary diff**: Run `just deploy` before and after phase, diff the staged
   tree (excluding timestamps in `manifest.json` and `build_utc`).
2. **Smoke**: Deployed binary compiles and runs the smoke test successfully.
3. **PEX e2e**: `just lang-codegen-test-pex` passes (exercises PEX + bundle
   + compiler sources path).
4. **Existing tests**: `tools/drift_deploy/test_deploy.py` (64 tests),
   `test_resolver.py` (26 tests), `test_manifest.py` (11 tests) all pass.

New tests added in phase 5:
- `test_deploy_metadata()` — verify metadata extraction
- `test_stdlib_dep_single_source()` — verify `stdlib_dep.txt` content
  matches `peek_package_id_and_version()` result
- `test_trust_store_generation()` — verify trust store JSON structure

## Risks and Tradeoffs

### Low risk
- Each phase is independently deployable and verifiable
- Shell step scripts are self-contained — migration is 1:1
- No deploy contract changes (same CLI, same outputs, same artifact layout)

### Medium risk
- **PEX build subprocess calls**: `pex` CLI is invoked via subprocess regardless.
  Python step replaces shell arg construction, not the PEX tool itself.  Risk:
  edge cases in argument quoting when switching from shell to `subprocess.run`.
  Mitigation: verify PEX output is identical.

- **`lang-codegen-test-pex` justfile recipe**: Currently calls shell steps
  directly.  Must be updated in phase 2 to call Python steps or the new
  orchestrator.  Risk: PEX e2e tests break during migration.
  Mitigation: update recipe atomically with step deletion.

### Not a risk (explicitly scoped out)
- **Shell CLI compatibility**: No effort spent reproducing `deploy.sh` getopt
  quirks, positional args, `KEY=VALUE` forms, or `--` prefix stripping.  The
  Python CLI is clean argparse; all callers are updated.  This is an internal
  tool with no external consumers.

### Low risk but worth noting
- `driftc-wrapper.sh` is a deployed artifact — cannot change its interface
  without a deployment cycle.  It stays as-is; `stdlib_dep.txt` remains the
  published contract between build-time and runtime.

## Intentional Behavior Changes

1. **CLI interface**: The Python entrypoint uses clean argparse.  Shell-specific
   calling forms (`DEST=path`, positional dest, `--` prefix, manual `~`
   expansion) are dropped.  All callers updated.

2. **Progress output**: Python `print()` vs shell `echo`.  Same information,
   minor formatting differences.

Preserved without change:
- Staged artifact layout
- Deploy validation semantics (signing key required, smoke test, etc.)
- Published result (directory structure, manifest.json, symlink)
- PEX build outputs
- stdlib dep spec contract

## Out of Scope

- `tools/drift_deploy/` (package deploy pipeline) — already Python-native
- `pex_entry.py` / `deploy_pex_entry.py` deployed entry points — runtime
  artifacts, not build-time orchestration
- `driftc-wrapper.sh` — deployed runtime artifact, stable interface
- Deploy contract changes (CLI flags, artifact layout, version naming)
- New deploy features
