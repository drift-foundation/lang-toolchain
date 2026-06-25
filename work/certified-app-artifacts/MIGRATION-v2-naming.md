# MIGRATION — canonical v2 names/layout (static audit, no code yet)

Companion to `PLAN.md` (D-6/D-7 = Phase 0). Enumerates every surface that must change for
the v2 naming/layout break, and the aliases we intentionally keep.

## Canonical v2 vocabulary (decided)
- `artifact_kind: "package" | "app"` — **no `doc`** (decided: no doc artifact kind), no
  `library`. `package` = importable Drift package (modules + declared assets); `app` =
  runnable binary/service.
- **Documentation, SQL schemas, templates, migrations, static files = declared ASSETS**
  carried BY a `package` (the shipped assets-in-`.zdmp` mechanism) or BY an `app` (Phase 2
  verified app assets) — they do NOT introduce a third artifact kind. "Asset" is the vehicle;
  `package`/`app` are the only two artifact kinds.
- Deploy categories (singular): `pkg/<package-id>/<version>/`, `app/<app-id>/<version>/`.
  **No `lib/`**, no `doc/`. `.dmp`/`.zdmp` stay (container FORMAT, never an artifact
  category — nothing to rename, just never introduce a `dmp/` category).
- The current code does the OPPOSITE normalization (`package → library` canonical); this
  break REVERSES it.

## Sizing
- **Non-test code:** 6 files touch canonical `"library"`. **Tests:** ~62 files encode
  `lib/`-dir or `kind library/package`. **Docs:** ~6 (forward-looking design only; history is
  append-only). **Env:** keep `DRIFT_PACKAGE_ROOT`; one stray `DRIFT_PKG_ROOT` doc ref to fix.

## A. Canonical kind flip `library → package` (signed identity + branches)
Route all checks through ONE helper/constant (`IMPORTABLE_KIND = "package"` / `is_importable(kind)`)
instead of scattering literals.

| File:line | Now | v2 change |
|---|---|---|
| `manifest.py:108` | `kind` comment "library"/"app"; legacy package→library | "package"/"app"; legacy **library→package alias** |
| `manifest.py:124-125` | normalize `package → library` | **REVERSE**: normalize `library → package` |
| `manifest.py:214,676` | `art.kind == "library"` gate | `== "package"` (via helper) |
| `manifest.py:235-238` | warn "kind: package is deprecated; use library"; accept {library,package,app} | flip: canonical `package`; accept `library` as alias (optional warn); set {package,app}(+library alias) |
| `manifest.py:687` | `compute_artifact_sci(kind="library")` | `kind="package"` — **SCI SOURCE-IDENTITY BREAK** (pool re-cert) |
| `source_content_id.py:166` | `SourceContentInputs.kind` comment "library"/"app" | "package"/"app" |
| `source_content_id.py:235` | `"kind": inputs.kind` (hashed) | value flips for importables — consequence only |
| `drift_build.py:478,543` | `art.kind == "library"` | `== "package"` |
| `drift_prepare.py:265,286` | `art.kind == "library"` | `== "package"` |
| `drift_deploy.py:2102,2477,…` | `art.kind == "library"` (sidecars/has_packages) | `== "package"` (the `if art.kind == "app"` asset gate stays) |
| `lang/drift/trust.py:394,423,473,663,668,818,827` | "library artifacts" grant/verify + messages | "package artifacts" |

**LEAVE ALONE:** `source_content_id.py:280,294,299,312,326` `_resolve_source_path(kind="module"/"asset")`
— a DIFFERENT `kind` axis (path-kind, not artifact-kind).

## B. Deploy category `lib/ → pkg/` (+ `app/` already exists)
No hardcoded `"lib"` in publish code — `_publish_package`/`_publish_app` write
`<--dest|--app-dest>/<name>/<version>` (caller supplies the category root). So this is
convention/docs/ORCH, not publish-code surgery.

| File:line | Now | v2 change |
|---|---|---|
| `drift_deploy.py:89`, `drift_prepare.py:75` | `--dest` help "Library root for resolving package_deps" | "Package root …" |
| `drift_lock.py:47`, `build_cmd.py:44`, `resolver.py:320` | comments "shared `lib/` tree" / "run `lib/` root" | "`pkg/`" |
| (external) ORCH staging | passes `--dest <root>/lib` | passes `--dest <root>/pkg` — **coordination dependency** |

`app/` category already supported via `--app-dest`; only docs/convention to align. The
loader/discovery (`discover_package_files`, `provider_v1.py:73`) globs given roots →
**no loader change**; consumers just point `--package-root` at `pkg/`.

## C. Env vars
- **`DRIFT_PACKAGE_ROOT`** is the REAL package-root env (`drift_build.py:188,198,206,712`) +
  `--package-root`. Already "package"-named → **KEEP, no rename.**
- **`DRIFT_PKG_ROOT`** is NOT real — only `drift_unpack.py:34` docstring + pushcoin's note.
  **Fix:** change the unpack docstring example to `DRIFT_PACKAGE_ROOT` (or formally adopt a
  canonical name; recommend just using the existing `DRIFT_PACKAGE_ROOT`).
- **`DRIFT_APP_ROOT`** does not exist. Apps are addressed by path / `--app-dest` today; a
  dedicated app-root env is net-new — **defer** unless a consumer needs discovery.

## D. CLI wording
- `drift verify-package` name stays (it verifies a package); `drift verify-app` is the app
  peer (PLAN Phase 1). No rename.
- `drift unpack` wording is already "package"; fix the `$DRIFT_PKG_ROOT` example (C).
- `lang/drift/trust.py` user-facing messages "library artifacts" → "package artifacts" (A).

## E. Docs / design / history
- Update forward-looking design/workflow docs where they state the artifact kind / deploy
  category: `doc/design/trust-v1.md` (8 refs), `doc/design/provenance-bundle.md`,
  `doc/toolchain-build-workflow.md`, `doc/test-run.md`.
- **Do NOT rewrite append-only history** (`history.md`, `doc/history.md`) — past entries are a
  record; the v2 break gets a NEW history entry, not edits to old ones.

## F. Tests (~62 files — mechanical but large; AGENTS.md test-edit gate satisfied by this
   directive)
- Tests hardcoding a `tmp_path/"lib"` deploy dir → `"pkg"` (incl. `test_trust_verify_package_cli.py::_build_good_dir`
  and the new `test_unpack_cli.py`).
- Tests asserting `kind == "library"` / `kind="library"` → `"package"`.
- **`test_manifest.py` normalization test must FLIP** (currently asserts `package → library`;
  v2 asserts `library → package`).
- `test_source_content_id.py` kind-in-canonical-body assertions update to `"package"`.

## Compatibility aliases to INTENTIONALLY KEEP
- **Manifest `kind: library`** accepted as a parser alias → normalized to `package` (existing
  manifests keep parsing during transition; optional deprecation warning). Canonical authored
  form is `package`.
- **`DRIFT_PACKAGE_ROOT`** — keep (already canonical).
- **`.dmp` / `.zdmp`** — container format names, unchanged.

## Explicitly NO alias (clean break)
- Signed `artifact_kind` has NO `library` value — only `package`/`app`. v1 claims/SCI are
  rejected (D-1 drop-v1); the `kind="library"` SCI value is gone from new artifacts → pool
  re-cert.
- No `doc` artifact kind, no `doc/` category (dropped).
- No `DRIFT_PKG_ROOT` / `DRIFT_APP_ROOT` introduced by this break.

## Recommended sequencing
Phase 0 (this migration) is a self-contained commit: the `library→package` flip + `lib→pkg`
docs/convention + the test sweep + a history entry, validated against the existing
library/deploy/verify suites (re-pointed to `package`/`pkg`). Phase 1 (certified app
artifacts) builds on the canonical `artifact_kind`/layout it establishes.
