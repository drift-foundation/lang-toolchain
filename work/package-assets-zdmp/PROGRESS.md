# PROGRESS — package assets in `.zdmp` + `drift unpack`

Power-loss recovery point. Newest on top. See `PLAN.md`.

## Status table

| Item | State |
|---|---|
| Classification | **FEATURE / package-format + CLI boundary change** (signed-artifact surface) |
| Design grounded in code | **DONE** — SCI already commits asset `(path,sha)`; loader additively tolerant; no `drift unpack` yet |
| Design reply to drift-workflows | **DONE** (sent; shape ACKed) |
| PLAN.md | **DONE** |
| **D-2** drop loose `assets/` folder | **RESOLVED** — drop outright; no `--unverified-assets` |
| **D-3** unpack trust source | **RESOLVED** — no silent self-trust fallback; mirror `trust verify-package` flags |
| **D-4** `--dest` semantics | **RESOLVED** — must not exist (v1); `--replace` deferred |
| **D-1** spec-first vs implement-now | **RESOLVED** — implement now (user, 2026-06-24) |
| **D-5** large-asset strategy (lazy vs size guard) | **OPEN** — DB-schema can land first; gate large-asset (drift-web) on this |
| `doc/design/package-assets-v0.md` spec | not started (gated on D-1) |
| D1: driftc `--asset` packs into blobs/TOC + `manifest.assets` | **DONE** — container round-trip verified by hand |
| D1: `build_package_cmd` passes `--asset` (build+deploy) | **DONE** |
| D1: drop loose `_stage_assets()` publish (library only; apps keep it) | **DONE** |
| Container parse: `AssetEntry` + `_parse_assets` + `LoadedPackage.assets` | **DONE** |
| D2: `drift unpack` CLI + verify→temp→sanitize→atomic-rename | **DONE** (`tools/drift_deploy/drift_unpack.py` + cli dispatch) |
| Regression: `drift unpack` e2e (happy + tamper + dest-exists + no-trust) | **DONE** — `test_unpack_cli.py` 5 passed |
| D1: backward-compat regression (assets don't perturb module decode) | **DONE** — `test_dmir_pkg_assets.py` 6 passed |
| Existing-surface regression: build+manifest (176) | **DONE** — green |
| Existing-surface: verify-package (path unpack reuses) | **DONE** — 24 passed |
| Existing-surface: package_v0 (.dmp container) | **DONE** — 78 passed |
| Existing-surface: build + manifest | **DONE** — 176 passed |
| Review fixes (TOCTOU / dir-assets+symlinks / blob-type) | **DONE** — 21 passed |
| Version bump → DRIFTC 0.33.56 (ABI 18 unchanged) | **DONE** |
| history.md entry | **DONE** |
| Release note → `/tmp/drift-announce` | **DONE** — `2026-06-24T23:40:24Z-drift-lang-release-notes.md` |

## Log

### 2026-06-24 (cont.) — second review round addressed (2)
- **Medium (SHA-collision cross-role):** an asset whose bytes equal a code
  (DMIR/interface) blob would overwrite that content-addressed TOC row to type
  ASSET, aliasing a code blob. Guarded at BOTH ends: emit (`driftc.py` rejects an
  asset sha already typed non-ASSET) and load (`_parse_assets` rejects an asset sha
  that is also a module interface/payload blob — passed `module_blob_shas`).
  Regression: `test_dmir_pkg_assets.py::test_asset_sha_colliding_with_code_blob_rejected`.
- **Low/Med (clean extraction failure):** `_extract_assets_fail_closed` now wraps
  decompress/load exceptions as `UnpackError`, so `drift unpack --json` returns a
  structured exit-1 failure (not a traceback) on a post-verify corrupt/swapped artifact.
  Regression: `test_unpack_cli.py::test_unpack_corrupt_artifact_after_verify_clean_failure`.
- **Tests:** `test_dmir_pkg_assets.py` (8) + `test_unpack_cli.py` (7) → **15 passed**.
  No version change (same 0.33.56 uncommitted feature; ABI 18). history.md regression
  list updated; published release note unchanged (internal hardening, no contract change).

### 2026-06-24 (cont.) — all regressions green; release note published
- Existing-surface suites all green with my additive changes: verify-package **24**,
  package_v0 **78**, build+manifest **176** (the slow serial driver run earlier was
  pathological + competing with orphaned runs; re-ran under xdist `-n4`).
- Feature + 3 review fixes fully covered: `test_unpack_cli.py` (6), `test_dmir_pkg_assets.py`
  (7), `test_build.py::TestAssetDirectoryEntries` (8).
- Release note published to `/tmp/drift-announce/2026-06-24T23:40:24Z-drift-lang-release-notes.md`.
- **FEATURE COMPLETE.** DRIFTC 0.33.56, ABI 18. Remaining follow-up: D-5 (large-asset
  lazy-load / size guard) before enabling large-asset packages; deferred `drift unpack --replace`.

### 2026-06-24 (cont.) — review findings addressed (3)
- **High (TOCTOU verify→extract):** `drift_unpack._extract_assets_fail_closed` now
  recomputes `sha256(decompressed .dmp)` and requires it to equal the verified
  `report["artifact_sha256"]` before loading/writing → refuses to materialize bytes
  that weren't covered by the successful verify. Regression: `test_unpack_cli.py::
  test_unpack_artifact_swapped_after_verify_fails_closed` (monkeypatches verify OK, swaps
  the `.zdmp`, asserts exit 1 + nothing written).
- **Medium (directory assets):** added `manifest.resolve_asset_files` (recursive, ALL
  extensions), shared by BOTH the SCI path (`compute_artifact_sci`) and the packing path
  (`build_package_cmd`) so the signed identity and packed blobs stay in lock-step.
  Explicit symlink policy per review: in-root file symlink OK (target bytes packed, regular
  file on unpack); escaping / dangling / symlink-to-directory all REJECTED (never silently
  skipped); unpack writes regular files only (bytes, not topology). Regressions:
  `test_build.py::TestAssetDirectoryEntries` (8: expansion, file/dir-same-SCI, empty-dir,
  in-root symlink, escaping/dangling/symlink-dir rejection, nonexistent passthrough).
- **Medium (asset blob-type):** `_parse_assets` now requires the referenced blob's TOC
  type == `BLOB_TYPE_ASSET` (an asset entry may not alias a DMIR/interface blob).
  Regression: `test_dmir_pkg_assets.py::test_asset_aliasing_code_blob_rejected`.
- **Tests:** review-fix suite **21 passed** (dmir assets 7 + unpack 6 + asset-dir 8).
  Existing-surface package_v0 + verify-package driver run in progress.

### 2026-06-24 (cont.) — D-1 = implement now; feature landed
- **driftc** (`driftc.py`): `--emit-package` gains repeatable `--asset RELPATH FILE`;
  reads bytes → content-addressed blob (`BLOB_TYPE_ASSET=3`) in the existing
  blobs/TOC, referenced by new `manifest.assets:[{path,blob,len}]`. Logical path
  normalized with the SCI's `_normalize_canonical_path` (no abs/`..`). Deterministic
  ordering for reproducible artifact_sha256.
- **build_cmd** (`build_package_cmd`): one `--asset <rel> <root/rel>` per declared
  asset → covers `drift build` AND `drift deploy`.
- **deploy** (`drift_deploy.py`): loose `_stage_assets` now LIBRARY-skipped (assets in
  the `.dmp`); apps keep loose staging (no container).
- **container** (`dmir_pkg_v0.py`): `AssetEntry` + `_parse_assets` + `LoadedPackage.assets`;
  absent key → `[]` (backward-compatible). Loader tolerates unknown blob type/field.
- **unpack** (NEW `tools/drift_deploy/drift_unpack.py` + `cli.py` dispatch + help stub):
  verify (reuse `verify_deployed_package`) → temp-extract → per-blob sha recheck +
  path re-sanitize → atomic `os.replace` to `--dest`. Trust flags mirror
  `trust verify-package`; no silent self-trust; `--dest` must not exist (v1).
- **Verified by hand:** emit `singular` lib + 2 SQL assets → load → assets present, bytes
  exact, `manifest.assets` stamped. `drift unpack --help` + `driftc --asset` render.
- **Tests green:** `test_unpack_cli.py` (5), `test_dmir_pkg_assets.py` (6),
  `test_build.py`+`test_manifest.py` (176). `test_driftc_package_v0` + verify-package
  running.
- **Version:** DRIFTC 0.33.55 → **0.33.56**; **ABI stays 18** (package tooling +
  container manifest only). Author-claims unchanged (SCI stable); cert-claims re-issue
  for asset-shipping packages (artifact_sha256 changes). history.md updated.
- **Next:** confirm final regression green → release note to `/tmp/drift-announce`.

### 2026-06-24 — plan written, decisions D-2/D-3/D-4 resolved
- Mapped the package/trust subsystem (two Explore passes) and **verified two load-bearing
  facts in code**: (1) `source_content_id` already folds asset `(path, sha256)` and is
  bound three-ways across manifest/author-claim/cert-claim → assets already committed by
  signature; (2) DMIR-PKG v0 loader enforces manifest`.blobs`↔TOC + per-blob sha but does
  NOT reject unknown blob types or require blobs to be module-owned → asset blobs are
  additively tolerated by current code consumers.
- Sent design reply to drift-workflows confirming **assets-in-`.zdmp` + verify-then-unpack**
  as the shape, with the clarification that the integrity primitive already exists (SCI);
  the two real gaps are (a) bytes inside `artifact_sha256`'s container and (b) a
  verify-gated extractor.
- Wrote `PLAN.md` (FEATURE, boundary-contract change). User refinements folded in:
  1. D1 phrasing: asset bytes go into the **existing `manifest.blobs`/TOC** referenced by
     `manifest.assets` — no unreferenced blobs (loader forbids them).
  2. `format_version` stays 0 (loaders ignore unknown manifest fields + blob types) **but
     pin with a regression**; noted the **eager-blob-read** cost for large assets → new
     open decision D-5 (lazy loading vs size guard).
  3. `drift unpack` trust source must not silently self-trust (D-3).
  4. `--dest` must not pre-exist for v1; `--replace` deferred (D-4).
  5. Drop loose deployed `assets/` folder outright; no `--unverified-assets` (D-2).
- **ABI status:** no runtime ABI change planned (`DRIFT_RT_ABI` stays 18); package
  `format_version` stays 0; asset-shipping packages re-issue **cert-claims only**
  (artifact_sha256 changes), author-claims unchanged (SCI stable). `DRIFTC_VERSION` will
  bump when D1/D2 land.
- **No code changed this step** — planning only; implementation gated on D-1.
- **Next:** user to pick D-1 (spec-first vs implement-now); then start the
  `doc/design/package-assets-v0.md` spec or the deploy-side blob plumbing.
