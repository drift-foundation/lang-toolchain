# Compressed Package Distribution (.zdmp) + Smoke Dep Pinning Fix

## Overview

Two changes shipping together:

1. **Compressed package distribution (.zdmp)** — hard cutover from `.dmp` to zstd-compressed `.zdmp` as the published package format, with `.sig` sidecar naming.
2. **Smoke dep pinning fix** — baseline smoke now passes `--dep` pins for all resolved dependencies, not just the artifact being smoked.

---

## 1. Compressed Package Distribution (.zdmp)

### What changed

Published Drift packages are now zstd-compressed. The compiler emits `.dmp` as a build intermediate; the deploy pipeline compresses to `.zdmp` before signing, staging, and publishing. Signature semantics are unchanged — signatures cover the canonical uncompressed bytes.

### Hard cutover decisions

- Published artifact: `<name>.zdmp` (not `.dmp`)
- Signature sidecar: `<name>.sig` (not `<name>.dmp.sig`)
- No legacy `.dmp` fallback in production paths
- No dual-publish mode
- During development, discovery accepts both `.zdmp` and `.dmp`; `.zdmp` wins when both exist for the same stem

### Compression settings (pinned)

- Algorithm: zstd
- Level: 3
- Threads: 0 (single-threaded, deterministic output)
- `write_content_size=True` in frame header

### Local cache

- Location: `$DRIFT_CACHE_DIR/pkg/v0/<sha256>.dmp` (default `~/.cache/drift/pkg/v0/`)
- Content-addressed by sha256 of uncompressed bytes
- Cache hit skips decompression; signature verification still runs on every load (cache does not weaken trust)
- Cache populated only after successful decompress + hash verification

### Trust/signature verification rule

1. Read `.sig` sidecar to get expected uncompressed sha256
2. Check cache — if hit, return cached bytes (skip decompression)
3. If miss: decompress `.zdmp`, verify sha256 matches expected, write to cache
4. Caller (`load_package_v0_with_policy`) still runs `verify_package_signatures` against uncompressed bytes on every load
5. Ed25519 signature verification happens against uncompressed bytes regardless of cache state

### Files changed

#### New files
| File | Purpose |
|------|---------|
| `lang/driftc/packages/zdmp.py` | Compression/decompression/cache module |
| `lang/driftc/packages/test_zdmp.py` | 10 unit tests |

#### Core changes
| File | Change |
|------|--------|
| `requirements.txt` | Added `zstandard>=0.23.0` |
| `lang/driftc/packages/dmir_pkg_v0.py` | Extracted `_load_dmir_pkg_v0_impl()`, added `load_dmir_pkg_v0_from_bytes()` |
| `lang/driftc/packages/provider_v0.py` | Discovery accepts `.zdmp`+`.dmp` with dedup; loading handles `.zdmp` via cache |
| `lang/driftc/packages/signature_v0.py` | Sig path: `pkg_path.with_suffix(".sig")` |
| `tools/drift_deploy/resolver.py` | Discovery accepts `.zdmp`; `_sha256_file` hashes uncompressed bytes for `.zdmp` |
| `tools/drift_deploy/drift_deploy.py` | Compress after sign, stage `.zdmp`+`.sig`, dep namespace discovery accepts `.zdmp` |

#### Sig naming updates (hard cutover)
| File | Change |
|------|--------|
| `lang/drift/cli.py` | Sign output, trust import, signer inspection use `.with_suffix(".sig")` |
| `lang/drift/publish.py` | Sidecar lookup uses `.with_suffix(".sig")` |
| `lang/drift/trust.py` | Trust import accepts `.zdmp`, uses `.with_suffix(".sig")` |
| `lang/drift/sign.py` | Docstring update |
| `tools/deploy/step_stdlib_pkg.sh` | Stdlib sig naming (`std.sig` not `std.dmp.sig`) |
| `tools/deploy/step_bundle.sh` | Docstring |
| `tools/deploy/pex_entry.py` | Docstring |
| `justfile` | Build clean target |

#### Test updates (16 occurrences across 10 files)
All `Path(str(pkg) + ".sig")` patterns → `pkg.with_suffix(".sig")`:
- `lang/tests/driver/test_drift_sign_cli.py`
- `lang/tests/driver/test_drift_trust_cli.py`
- `lang/tests/driver/test_drift_key_package_cli.py`
- `lang/tests/driver/test_deploy_self_sufficient_python_bundle.py`
- `lang/tests/driver/test_driftc_package_v0.py`
- `lang/tests/driver/test_drift_publish_fetch_vendor.py`
- `lang/tests/driver/test_deploy_stdlib_package.py`
- `lang/tests/driver/test_external_consumer.py`
- `lang/tests/driver/test_deploy_pex_scie.py`
- `lang/tests/codegen/e2e/pkg_consumer_runner.py`

---

## 2. Smoke Dep Pinning Fix

### Bug

Baseline smoke only passed `--dep <artifact>@<version>` for the artifact being smoked. It did not pass `--dep` pins for the artifact's resolved dependencies. When multiple versions of a dependency were visible in the smoke package root, the compiler failed:

```
error: multiple versions of 'net-tls' found (0.2.0, 0.3.0, 0.3.1);
use --dep net-tls@<version> to select
```

### Root cause

`_run_baseline_smoke_package` constructed the smoke compile command with only `--dep {art.name}@{art.version}`. The resolved dependency graph (already computed for build) was not threaded through.

### Fix

- `_run_baseline_smoke_package` now accepts `resolved: dict[str, ResolvedDep] | None`
- Smoke command includes `--dep <pkg>@<version>` for every resolved dependency
- Call site in `_deploy_artifact` threads `resolved` through
- Smoke uses the same exact version selection contract as build

### Files changed

| File | Change |
|------|--------|
| `tools/drift_deploy/drift_deploy.py` | Added `resolved` parameter to `_run_baseline_smoke_package`, emit `--dep` pins |

### Regression tests added

| Test | What it verifies |
|------|-----------------|
| `TestSmokeDepPinning::test_smoke_pins_resolved_deps` | Artifact with 2 resolved deps: smoke command includes `--dep` for artifact + both deps |
| `TestSmokeDepPinning::test_smoke_no_deps_pins_only_artifact` | Artifact with no deps: smoke only pins the artifact itself |

---

## Test results

| Suite | Tests | Result |
|-------|-------|--------|
| zdmp unit tests | 10 | pass |
| Deploy tests | 66 | pass |
| Resolver tests | 37 | pass |
| Sign CLI tests | 3 | pass |
| Trust CLI tests | 2 | pass |
| Package consumer e2e | 77 | pass |
| **Total** | **195** | **pass** |

---

## Separate issue (not included)

MIR lowering bug: nested match-with-return triggers "missing return reached MIR lowering". This is a LANGUAGE_BUG requiring regression-first compiler fix, tracked independently.
