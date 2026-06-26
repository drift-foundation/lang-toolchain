# PROGRESS — certified runnable app artifacts + `drift verify-app`

Power-loss recovery point. Newest on top. See `PLAN.md`.

## Status table

| Item | State |
|---|---|
| Classification | **FEATURE / trust-v1 extension to `kind: app`** (boundary: sidecars, cert-claim schema, verify, CLI) |
| Determination: toolchain blocker (not workflows ownership) | **DONE** — apps build+deploy but get no author/cert claim; verify is `.zdmp`-only |
| Static review of current app deploy/verify path | **DONE** — facts grounded in code (drift_deploy:2102/2302/2334, verify_deployed_v1:25-36, cert_claim_v1:193-206, provenance:90-129) |
| Trust shape agreed (author=workflows / cert=orch over binary hash) | **DONE** (with user) |
| PLAN.md | **DONE** |
| **Review gate** (lock decisions + phasing) | **CLEAR** — all decisions + consistency fixes folded; ready to implement |
| Review rounds (5 findings + 5 consistency fixes) folded into PLAN | **DONE** |
| D-1 `artifact_kind` REQUIRED in BOTH author+cert bodies (`"package"`/`"app"`, no `library`); claim schema → v2 | **RESOLVED** (user) — no optional/default |
| v1 break: drop v1 cleanly (no legacy reader); pool re-cert to v2 `package` | **RESOLVED** (user) |
| D-2 `source_content_id` REQUIRED in provenance; provenance schema → v4 | **RESOLVED** (user) — no two-way mode |
| D-3 app trust subject = declared `module_namespace` (required; no pkg_id fallback) | **RESOLVED** |
| D-4 SIGNED `artifact_path` locator in cert body (not filename-guess) | **RESOLVED** |
| D-5 app verify adapter (synthetic subject) vs `module_id→trust_subject` refactor | **RESOLVED** — adapter for Phase 1 |
| D-6 canonical kind flip `library → package` | **RESOLVED** — `package` canonical, `library` parser alias |
| D-7 deploy layout `lib/ → pkg/` (+ `app/`); artifact-id≠project-name | **RESOLVED** |
| `doc` as a third artifact kind | **REJECTED** (user) — docs/SQL/templates are declared ASSETS on package/app |
| v2 naming/layout static audit + migration inventory | **DONE** — `MIGRATION-v2-naming.md` |
| **Phase 0 functional migration (`library→package`, `lib→pkg`, docs)** | **DONE + GREEN** — incl. 3 blocking review fixes (SCI reject, stdlib SCI, doc pkg roots) |
| SCI identity boundary: reject non-canonical kind | **DONE** — `library` only canonicalized at manifest; zero stray `kind="library"` callers (only the intended alias check at `manifest.py:256`) |
| Phase 0 cosmetic: ~30 test `tmp_path/"lib"` dirs → `"pkg"` | DEFERRED (layout-agnostic; tests pass either way) — batch at end |
| **Phase 1** (app claims + verify-app) | **IN PROGRESS** |
| P1: author_claim_v1 → v2 (required `artifact_kind`, reject v1) | **DONE + unit green** — `test_author_claim_v1.py` 56 pass (moved to v2 + 4 new artifact_kind tests); stale "v1-only" prose fixed in module + test |
| P1: cert_claim_v1 → v2 (`artifact_kind` + signed `artifact_path`, reject v1) | **DONE + unit green** — `test_cert_claim_v1.py` 75 pass (+7 kind/path tests); prose fixed |
| P1: provenance → v4 (required `source_content_id`) | **DONE (schema)** — `build_provenance` requires SCI@v4; both deploy callers wired; `compute_artifact_sci` now passes `art.kind` (apps carry SCI); deploy computes SCI for app+package |
| **Security-schema + producer-emit unit (author/cert v2, provenance v4, SCI, emit APIs)** | **DONE + GREEN** — **241 pass**: author 56 / cert 82 / provenance 18 / sci 12 / manifest 41 / no-v1-ctor static 3 / drift_author_emit / drift_cert_emit |
| P1: deploy EMISSION passes artifact_kind/artifact_path (all 5 production constructors) | **DONE** — stdlib cert, drift_author publish+publish-raw, cert_cli, drift_deploy `_emit_cert_claim_for_artifact`; static regression pins no v1 ctors |
| P1: SCI fail-hard before provenance (package + app); no warn/skip | **DONE** |
| P1: update verify harnesses to v2/v4 (test_trust_verify_package_cli, test_unpack_cli build claims+provenance inline) | TODO |
| P1: deploy app-claim emission (author+cert+signed provenance) | TODO |
| P1: verify adapter + `verify_deployed_package` app branch | TODO |
| P1: `drift verify-app` CLI | TODO |
| P1 step 1: migrate verify fixtures/harnesses to v2/v4 (factories) | **DONE** — pkg_test_helpers (4), conftest (2), test_verify_v1, test_v1_adversarial, test_trust_verify_package_cli, test_unpack_cli; provenance v3→v4 + SCI in both CLI harnesses |
| P1 step 2: PACKAGE verify cross-checks (kind/path/provenance match; no two-way) | **DONE** — `verify_deployed_v1` step 4b + `_cross_check_provenance`; 8 negative regressions + positive green |
| P1 step 3: app verify adapter (synthetic subject; all-three say app) | **DONE** — `verify_deployed_app` + `drift verify-app` CLI (verify-only); 12 tests |
| P1 step 4: verify regression (v1 fail / v2 verify / mismatch diagnostics / app agreement) | **DONE** — package 8 negatives + app 11 negatives + positives; v1/v3 reject |
| **`drift verify-app` CLI (Phase 1 endpoint; verify-only, no exec)** | **DONE** |
| P1: regression set (positive/negative/migration/boundary) | TODO |
| Phase 1: app author+cert claims + signed provenance | not started (gated on review) |
| Phase 1: `verify_deployed_package` app branch (reuse compose_verify) | not started |
| Phase 1: `drift verify-app` CLI | not started |
| Phase 1 regressions (positive/negative/back-compat/boundary) | not started |
| `drift run` / verify-then-exec | **OUT OF SCOPE** (review) — Phase 1 ends at `drift verify-app`; app exec is the orchestrator's job, no placeholder |
| verified app assets | not part of this slice (revisit separately) |
| Version bump (DRIFTC 0.33.56 → 0.33.57; ABI 18 unchanged) | **DONE** — single bump for Phase 0+1 |
| history.md entry (0.33.57) | **DONE** |

## Release-sequencing constraint (user, 2026-06-25)
Phase 0 may be a SEPARATE COMMIT, but **do NOT release / cert-pool-cut Phase 0 alone.** The
package SCI flip (`library→package`, changes source identity) must ship TOGETHER with the v2
claim/provenance break (Phase 1 schema) — otherwise an interim state exists with package-kind
SCI but old v1 claims. Implement Phase 0 then Phase 1 on the **same branch/slice**, ONE
version bump + ONE toolchain publish at the end.

## Log

### 2026-06-25 (cont.) — Step 3 review fixes (3)
- **Provenance schema_version enforced:** `_cross_check_provenance` now rejects
  `schema_version != 4` centrally (`provenance-schema-version`) — a v3 bundle carrying v4
  fields no longer passes (package + app). Regressions added both paths.
- **App binary symlink rejected:** `verify_deployed_app` fails `bin_path.is_symlink()` before
  hashing (`artifact-symlink`) — the verified locator must be a regular file so orchestration
  runs the exact verified bytes. Regression added.
- **Trust-source validated before content IO:** `verify_deployed_app` restructured to a
  step-1b invocation check (exclusivity / no-source / `--trust-store` load / `--author-pubkey-b64`
  shape) BEFORE reading the author claim; namespace-defaulted store build deferred to step 3
  (mirrors verify-package). Regression: no-trust + missing author claim → usage error.
- Green: verify suites **121**.

### 2026-06-25 (cont.) — Step 3 DONE: app verify adapter + `drift verify-app` (Phase 1 endpoint)
- `verify_deployed_v1.verify_deployed_app(opts)`: app consumer path (binary + sidecars, NO
  `.zdmp`). Reads the author claim FIRST (app id/version/sci/namespace/kind); derives the
  **synthetic trust subject** = the namespace prefix (`microflows.*` → `microflows`, covered by
  both the claim's `_namespace_covers` and the trust store's `_allowed_for_role` via the
  `module_id == pfx` rule). Locates the binary by the cert's SIGNED `artifact_path`, hashes it,
  builds a `PackageIdentity`, runs `compose_verify` with the subject, then the app three-leg
  cross-checks: author==cert==provenance kind=="app"; cert path==binary filename; sha(binary)==
  cert==provenance; SCI agreement (no two-way fallback); provenance name/version match;
  evidence binding. Rejects a `.zdmp`-bearing dir as a usage error (use verify-package).
- **`drift verify-app <app-dir>` CLI** (`tools/drift_deploy/verify_app_cli.py` + cli.py dispatch
  + help stub): mirrors `verify-package` trust flags / JSON / exit codes. **Read-only — never
  executes the binary** (app exec stays the orchestrator's job). No `drift run`.
- Tests `test_verify_app.py` (12): happy + author/cert kind!=app + cert path!=binary + binary
  tamper + provenance kind/sci-missing/name mismatch + untrusted namespace + zdmp-present usage
  error + v1 cert reject + CLI end-to-end (ok=0/fail=1/no-trust=2).
- Green: full verify+schema set **276** (no regression to verify-package/unpack/verify_v1).
- **Phase 1 is feature-complete** (schema v2/v4 + emission + package & app verify integration +
  `drift verify-app`). Remaining before ship: full-suite run (user) + version bump (Phase 0+1
  together) + history/release note.

### 2026-06-25 (cont.) — Step 2 fix: author-kind check was DEAD (review)
- Bug: `discover_author_claim_path(d)` was called WITHOUT the required
  `package_id=` kwarg → TypeError → swallowed by a broad `except Exception` →
  `author_kind=None` → the author-kind cross-check never ran (a signed author
  claim with `artifact_kind="app"` could pass verify-package).
- Fix: call `discover_author_claim_path(d, package_id=pkg_id)`; removed the broad
  except — only EXPECTED `(ValueError, OSError)` load/parse failures fold into the
  report (`malformed-sidecar`); a missing author claim under accepted certs is now a
  hard `author-claim-missing`; programmer errors surface instead of being swallowed.
- Regression added: `test_author_artifact_kind_mismatch_fails` (re-signs author claim
  with `artifact_kind="app"` → expects `artifact-kind-mismatch`). verify-package+unpack
  now **40**. (Reviewer's 263-suite → 264 with this test.) **Step 3 unblocked.**

### 2026-06-25 (cont.) — Step 2 DONE: package verify cross-checks
- `verify_deployed_v1.py` consumer enforcement (package path):
  - **step 4b:** author.artifact_kind == cert.artifact_kind == "package"; cert `artifact_path`
    == deployed `.zdmp` filename exactly; cert artifact_sha256 == deployed; cert SCI == identity
    SCI (re-asserted on top of compose_verify).
  - **step 5 `_cross_check_provenance`:** provenance inner artifact_kind=="package",
    artifact_sha256==deployed, source_content_id present+valid+==identity (NO two-way fallback —
    missing/malformed SCI is a hard `provenance-sci-invalid`), artifact_name==pkg_id,
    artifact_version==version. New helper `_load_provenance_fields`.
- Regressions in `test_trust_verify_package_cli.py` (parameterized `_build_good_dir` /
  `_write_cert_sidecar` / `_write_provenance` to inject ONE mismatch with signatures+evidence
  still valid): cert kind, cert path, provenance kind/sci/name/version mismatch, provenance
  SCI-missing (no fallback), v1 cert claim rejected cleanly. **8 negatives + positive.**
- Green: verify-package+unpack CLI **39**; in-memory verify + schema **221**. Package-only;
  app adapter is step 3. No exec surface.

### 2026-06-25 (cont.) — SCOPE: `drift run` removed; Phase 1 ends at `drift verify-app`
- Review decision: **no `drift run` / verify-then-exec, no future placeholder.** App execution
  is the orchestrator/service-manager's job after it independently verifies. Removed all
  `drift run`/exec references from PLAN.md + PROGRESS.md; PLAN "Explicitly NOT in scope" now
  states this. Verified-app-assets dropped from this slice.
- Phase 1 verify-integration order locked (consumer side): (1) migrate verify fixtures→v2/v4,
  (2) PACKAGE cross-checks (author==cert kind, cert path==deployed filename, provenance
  kind/sha/sci/name/version all match; no two-way fallback), (3) app verify adapter (synthetic
  `module_namespace` subject; all-three say app; cert path → binary; verify only), (4) regression.
- **Step 1 DONE — verify fixtures migrated to v2/v4 (factories):** pkg_test_helpers (4 claim
  blocks, artifact_path=`pkg_path.name`/`std.dmp`), conftest (2), test_verify_v1 + test_v1_adversarial
  (`_author_body`/`_cert_body` helpers), test_trust_verify_package_cli + test_unpack_cli (incl.
  provenance bundle bumped to schema v4 + `source_content_id`). All via `make_author_claim_body`/
  `make_cert_claim_body`. Green: verify_v1+adversarial **65**, verify-package+unpack CLI **31**.
  (Driver tests that compile via conftest/pkg_test_helpers now emit v2 claims; their consumer
  compile path doesn't check the new fields, so they load fine — full-suite run is the user's.)
- **NEXT: step 2 — package verify cross-checks** (author kind==cert kind=="package"; cert
  `artifact_path` == deployed filename; provenance kind/sha/sci/name/version all match claims +
  artifact; no two-way fallback on missing provenance SCI).

### 2026-06-25 (cont.) — producer-emit unit tests migrated to v2 (review)
- `test_drift_author_emit.py` + `test_drift_cert_emit.py` (the core `sign_and_write_*` emit-API
  unit tests) had v1 `_sample_body()` helpers → migrated to `make_author_claim_body` /
  `make_cert_claim_body` (artifact_kind=package, cert artifact_path=`<pkg>.zdmp`). These are
  producer-side, so they move WITH the factory change (not deferred to verify).
- **Count corrected (honest):** the producer/schema set INCLUDING the emit-API tests is
  **241 pass** (prior "212" excluded the two emit files, which were red — 194/15 in the
  reviewer's run). The 241 figure now explicitly includes them.

### 2026-06-25 (cont.) — body factories kill the schema-drift class (review batch, 5)
- **Root cause of two High bugs:** `drift_author/cli.py` and `cert_cli.py` each defined a LOCAL
  `_BODY_SCHEMA_VERSION = 1` shadowing the canonical → they signed v1 bodies despite passing
  artifact_kind. Fixed by the user's preferred design:
- **NEW factories** `make_author_claim_body(...)` / `make_cert_claim_body(...)` in the schema
  modules stamp `schema_version` internally; callers never pass it. Public `BODY_SCHEMA_VERSION`
  alias added to both modules.
- **All 5 emitters migrated to factories**; local `_BODY_SCHEMA_VERSION=1` constants removed
  from both CLIs (stdlib, drift_author publish+raw, cert_cli, drift_deploy `_emit_cert_claim_for_artifact`).
- **Static regression strengthened** (review #3): now also asserts (a) no production module
  outside the two schema modules defines `_BODY_SCHEMA_VERSION`, and (b) no production claim-body
  ctor passes `schema_version=` (must use the factory). Canonical pinned == 2.
- **stdlib (review #4):** `_validate_external_stdlib_author_claim` now requires the external
  author claim's `artifact_kind == "package"` (catch author/cert kind mismatch at producer).
- **stdlib (review #5):** "installed with v1 author + cert claims" → "author + cert claims".
- Green: schema set **212 pass**; edited emitters import clean.
- KNOWN (next step): the verify/integration harnesses (test_verify_v1, test_v1_adversarial,
  test_drift_author_emit, test_pkg_consumer_e2e, test_co_artifact_identity_binding, …) still build
  v1 claims inline → migrate to v2 (factories + artifact_kind/path) as part of the verify adapter
  + regression step.

### 2026-06-25 (cont.) — deploy EMISSION moved to v2 + review batch (6 findings)
- **High — all 5 production claim constructors → v2** (the missed stdlib included):
  `tools/deploy/steps/stdlib.py` (cert: kind=package, path=`std.dmp` — stdlib ships uncompressed
  `.dmp`), `drift_author/cli.py` publish (`art.kind`) + publish-raw (new `--artifact-kind`,
  default package), `cert_cli.py` (new required `--artifact-kind`/`--artifact-path`),
  `drift_deploy.py::_emit_cert_claim_for_artifact` (new `artifact_kind` param; signed locator
  `<pkg>.zdmp`). **Static regression** `test_no_v1_claim_constructors.py` scans production tree —
  no v1 Author/Cert claim ctor remains.
- **High — SCI fail-hard:** deploy now raises `DeployError` if SCI can't be computed for a
  package OR app, BEFORE any `build_provenance`; removed the v0 warn/skip path.
- **provenance canonical fields:** (done earlier this session) kind+sha+sci all validated.
- **doc + prose:** `provenance-bundle.md` → schema v4 + `source_content_id` row; `cert_claim_v1`
  module docstring now says artifact = package|app, envelope-v1/body-v2, signed `artifact_path`.
- **PROGRESS counts reconciled** (review #6): **authoritative security-schema tally = 211 pass**
  — author 56 / cert 82 / provenance 18 / sci 12 / manifest 41 / no-v1-ctor static 2. (Reviewer's
  156 = author+cert+provenance subset; the old 147/184 figures were stale and are removed.)
- **NOTE for step 4 (verify adapter, review #3):** `verify_deployed_package` must enforce the
  full three-leg cross-check — provenance inner `artifact_kind` / `artifact_sha256` /
  `source_content_id` (and ideally `artifact_name`/`version`) MUST match the author+cert claims
  and the deployed artifact; no two-way fallback. Producer is strict now; consumer enforcement
  is the remaining gap.

### 2026-06-25 (cont.) — security-schema review fixes (3)
- **provenance SCI shape:** `build_provenance` now uses full `validate_sci` (sha256:+64 lc hex),
  not loose `startswith`. New `tools/drift_deploy/test_provenance.py` (7): schema_version==4,
  SCI/kind/sha present, missing/None rejected, malformed SCI (non-hex/len/upper/no-prefix/empty)
  rejected.
- **cert artifact_path canonical spelling:** signed locator must EQUAL its normalized form —
  reject `./x`, `x/`, backslashes, `sub/../x` (no silent rewrite of signed content). Tests:
  4 reject + 3 accept (parametrized).
- **author test wording:** `test_strict_v1_rejects_v2_envelope` → `test_rejects_unknown_envelope_version`;
  clarified envelope `version` (still 1) is a separate axis from body schema (v2).
- Suites green at this step: cert + author + provenance (counts superseded by the
  authoritative tally below — see the deploy-emission log entry).

### 2026-06-25 (cont.) — provenance canonical policy on all signed fields (review)
- `build_provenance` now also enforces `artifact_kind ∈ {package,app}` + `validate_sci(artifact_sha256)`
  (all three signed/cross-checked fields held to claim-grade policy, fail early at producer).
  `test_provenance.py` 7→**18** (added bad-kind ×4, malformed-artifact-sha ×5; renamed the
  missing-SCI test to mark it a required-kwarg TypeError, distinct from the validator ValueError path).

### 2026-06-25 (cont.) — cert_claim_v1 v2 + provenance v4 (security-schema unit complete)
- **cert_claim_v1 → v2:** required `artifact_kind` + signed `artifact_path` (safe relative path
  via `_validate_artifact_path`/`_normalize_canonical_path`); body schema_version 1→2; reject
  v1 + `library` + missing/unsafe path; canonical-dict + load/dump thread the fields; stale
  "strict v1" prose fixed. `test_cert_claim_v1.py` moved to v2 + 7 new tests → **75 pass**.
- **provenance → v4:** `build_provenance` requires `source_content_id` (raises on missing),
  schema_version 3→4; both deploy callers wired; `compute_artifact_sci` now passes canonical
  `art.kind` so **apps carry SCI**; deploy computes SCI for app+package.
- **Security-schema unit GREEN** (authoritative tally recorded in the later deploy-emission entry).
- REMAINING (integration, steps 3-5): deploy EMISSION must pass artifact_kind/artifact_path
  (cert_cli `CertClaimBody(...)` + author publish `AuthorClaimBody(...)`); verify adapter +
  `verify_deployed_package` app branch (match kind/path/sha/SCI across claims+provenance, no
  two-way fallback); update verify harnesses (test_trust_verify_package_cli, test_unpack_cli
  build v1 claims + v3 provenance inline → v2/v4) + asset-slice; regression pass. NO CLI yet.
- **Known transient:** the asset-slice + deploy-emission harnesses build v1 claims / v3
  provenance and will fail to construct until the emission + harness updates land (expected;
  the schema layer is intentionally strict-break).

### 2026-06-25 (cont.) — author_claim_v1 v2: unit suite + prose moved with the schema (review)
- `test_author_claim_v1.py` moved to v2: `_example_body` + all 7 inline loader-bodies + 5 direct
  `AuthorClaimBody(...)` reject-tests now carry `schema_version=2`/`artifact_kind`; added
  `test_signing_bytes_change_with_artifact_kind` / `test_artifact_kind_round_trips` /
  `test_reject_missing_artifact_kind` / `test_reject_legacy_library_artifact_kind`. **56 pass.**
- Fixed stale security-schema prose (module + test): "strict v1-only" / "schema_version != 1" →
  format-v1 / body-schema-v2 framing; module docstring body-schema block now shows v2 +
  `artifact_kind` + the clean-break note. (The author-claim suite moves WITH the schema, not
  later with cert/deploy harnesses.)

### 2026-06-25 (cont.) — Phase 1 started: author_claim_v1 → v2
- Added REQUIRED `artifact_kind` ("package"|"app") to `AuthorClaimBody` + `_BODY_KEYS`; bumped
  `_BODY_SCHEMA_VERSION` 1→2; `validate_body_shape` + `_parse_body` enforce kind ∈ {package,app}
  (rejects `library`); canonical-dict + load/dump thread the field. Clean v1 break: a
  schema_version-1 body is rejected at load. Smoke: app claim round-trips; v1 + `library` rejected.
- NOTE: existing claim-constructing test harnesses (test_trust_verify_package_cli,
  test_unpack_cli) build v1 `AuthorClaimBody` (no artifact_kind) → they will fail to construct
  until updated to v2 (part of P1 test-harness todo). Deferred until the cert_claim v2 + deploy
  pieces land so the harnesses are updated once, coherently.

### 2026-06-25 (cont.) — Phase 0 doc cleanups (2, batched into the naming sweep)
- `tools/deploy/steps/bundle.py:266-267` package-root examples `~/opt/drift/lib → ~/opt/drift/pkg`.
- `source_content_id.py:34` removed confusing `library, app` from the "target/build class"
  examples → `drift-dev`/`drift-linux-x86_64` + a note that `kind` (package/app) IS in SCI,
  distinct from the build target.
- Residual `opt/drift/lib` only in append-only `doc/history.md` (leave) + the deferred cosmetic
  test-root bucket (`test_build.py` `--dest` args, `test_driftc_package_v0.py` comment).
- **Phase 0 fully swept. Proceeding to Phase 1.**

### 2026-06-25 (cont.) — Phase 0 blocking review fixes (3)
- **#2 (keystone) — SCI layer now REJECTS non-canonical kind.** `compute_source_content_id`
  raises if `kind ∉ {package, app}` — `library` is canonicalized ONLY at the manifest boundary;
  a stray `library` can never reach the signed hash. Regressions: `test_source_content_id.py`
  reject-library / reject-unknown / accept-app (now 12 pass). This surfaced every stray caller.
- **#1 — stdlib deploy SCI flipped.** `tools/deploy/steps/stdlib.py:69` `kind="library"→"package"`
  (was minting old-identity stdlib SCI; not the toolchain `lib/` layout — real source identity).
- Swept all remaining `kind="library"` test fixtures → `package` (`test_source_content_id`,
  driver `conftest`/`pkg_test_helpers`/`test_deploy_runtime_readonly`/`test_deploy_stdlib_roles`).
  Repo now has ZERO `kind="library"` callers.
- **#3 — workflow doc package-deploy roots `lib → pkg`** (`toolchain-build-workflow.md` 240/248/
  367/468/480/507: `DRIFT_PACKAGE_ROOT`, `--dest`, deployed package paths). Toolchain `bin/lib`
  distribution refs (lines 11/15) intentionally kept.

### 2026-06-25 (cont.) — Phase 0 implementation STARTED
- **Core code flip DONE:** `manifest.py` — `ARTIFACT_KIND_PACKAGE/APP` + `normalize_artifact_kind`/
  `is_importable_kind`; `__post_init__` normalizes `library→package` (reversed); deprecation
  warning flipped (`library` deprecated → `package`); accepted set `{package,app}`(+library alias);
  `compute_artifact_sci(kind="package")` — the SCI source-identity flip. `source_content_id.py`
  kind comment. All `art.kind == "library"` → `"package"` across `drift_deploy.py`/`drift_build.py`/
  `drift_prepare.py`/`trust.py` (sed; "native library search" left intact). Help text/messages/
  kind-comments → "package". `lib/` convention comments → `pkg/` (resolver/lock/build_cmd). Unpack
  docstring `DRIFT_PKG_ROOT → DRIFT_PACKAGE_ROOT`.
- Smoke: normalization verified (`library→package`, `app` unchanged). `test_manifest.py` 41 pass
  (inverted the two kind-direction tests; assertions → `package`).
- Also swept `tools/drift_author/cli.py` (missed initially) — `kind=="library"`→`package` +
  "library artifact" messages; fixed 1 test assertion (`multiple package artifacts`).
- Docs: `trust-v1.md` + `toolchain-build-workflow.md` artifact-kind/`lib/<pkg>` layout →
  `package`/`pkg/` (carefully LEFT `/var/lib/`, `stdlib/`, and the toolchain `bin/lib` install
  path — a different `lib/`). `provenance-bundle.md` already said `package`/`app`.
- **Phase 0 functional GREEN:** test_manifest 41, source_content_id 9, test_build 143, asset
  slice (verify-package+unpack) 31, packages+prepare 445 + author 30. No test asserts
  `kind=="library"` anymore; SCI flip ripples cleanly (golden hashes are computed, not pinned).
- REMAINING (Phase 0 cleanup, low-risk/cosmetic): ~30 test files use a `tmp_path/"lib"`
  package-root dir — layout-agnostic (loader globs given roots), so tests pass either way;
  rename `lib→pkg` for canonical compliance is a final mechanical sweep (deferred to batch).
- v1 decision RESOLVED: **drop v1 cleanly, no legacy reader** — every existing certified
  artifact re-issues as a v2 `package` claim (pool re-cert); v1 claims reject. (Supersedes the
  "drop-v1 vs frozen-reader" sub-question in the earlier entries below.)
- Applied 5 consistency fixes to PLAN: (1) D-1 v2 kind set is `"package" | "app"` (no
  `library` in signed claims); (2) drop-v1 fully folded into Versioning / Regression #3 /
  Validation (v1 rejects, re-issued v2 `package` verifies, no frozen reader); (3) PROGRESS
  gate → CLEAR; (4) D-7 keeps `DRIFT_PACKAGE_ROOT` canonical and does NOT bless
  `DRIFT_PKG_ROOT`; (5) forward-looking "library path" wording → "package/importable path"
  (D-5, Phase 1) while current-code facts may still say `library`.
- **Gate is CLEAR.** No open decisions. **Next: implement Phase 0** (the `library→package` /
  `lib→pkg` naming-layout break per `MIGRATION-v2-naming.md`) as its own commit, then Phase 1
  (certified app artifacts). No code changed yet.

### 2026-06-25 (cont.) — v2 naming/layout audit; `doc` kind rejected
- User extended the break: `package` canonical (not `library`), deploy `pkg/` (not `lib/`),
  `app/` for apps; artifact id (`uflowsd`) names path/binary/sidecars and is independent of
  the project/package name (`microflows`) and of the trust subject (`module_namespace`).
  `doc` is NOT an artifact kind — docs/SQL/templates/migrations/static files are declared
  ASSETS carried by a package or app.
- Ran the full static audit (`MIGRATION-v2-naming.md`): **6 non-test code files** touch
  canonical `"library"` (manifest, source_content_id, drift_build, drift_prepare,
  drift_deploy, trust.py — route via one `is_importable`/constant helper); **~62 test files**
  encode `lib/`-dir or `kind library/package`; **~6 docs**. Key findings: the code currently
  normalizes the OPPOSITE way (`package → library`) and must REVERSE; the SCI `kind` flip is
  the source-identity break (pool re-cert); **no hardcoded `"lib"` in publish code**
  (`--dest`/`--app-dest` caller-supplied) → layout rename is convention/docs/ORCH;
  **`DRIFT_PACKAGE_ROOT` is the real env** (keep), `DRIFT_PKG_ROOT` is only my unpack
  docstring (fix), no `DRIFT_APP_ROOT`; `doc` is only a CLI doc-GENERATOR, not an artifact kind.
- Aliases to keep (explicit): `kind: library` parser alias → `package`; `.dmp/.zdmp` (format
  names); `DRIFT_PACKAGE_ROOT`. Clean-break (no alias): signed `artifact_kind` has no
  `library`; v1 claims/SCI rejected.
- Sequencing: Phase 0 (the naming/layout break) as its own commit, then Phase 1 (cert app)
  on top.
- **No code changed** — audit/planning only.

### 2026-06-25 (cont.) — review round 2 folded; decisions sharpened
- Verified the two High findings in code: `AuthorClaimBody` is strict-v1 (no `artifact_kind`,
  `_reject_unknown_keys` at `author_claim_v1.py:101-116`) → kind is a real schema change in
  BOTH bodies; `compose_verify`/`verify_package_modules` are module-shaped (`module_id`,
  "module load" diagnostics, per-module reserved routing at `verify_v1.py:202+` /
  `verify_harness_v1.py:135+`) → "all via compose_verify" corrected; Phase 1 needs an app
  adapter (D-5).
- Folded all 5 review findings, then applied the user's sharpening (sharpness over back-compat):
  - **D-1:** `artifact_kind` REQUIRED in author + cert bodies; **bump claim schema → v2**; no
    optional/default path; verify-app rejects old/package-only claims. Library compat not
    required for this slice. ONE open sub-question: drop v1 vs frozen v1-legacy reader (pool
    re-cert consequence flagged).
  - **D-2:** `source_content_id` REQUIRED in provenance; **bump provenance schema → v4**; no
    two-way SCI mode; missing provenance SCI → app verification failure.
  - **D-3:** canonical app trust subject = declared `module_namespace` (required; no `package_id`
    fallback — they are not interchangeable).
  - **D-4:** SIGNED `artifact_path` in the cert body (locator is attested, not filename-guessed
    / not from unsigned provenance).
  - **D-5:** Phase-1 app verify adapter (synthetic single subject) over the reused crypto;
    `module_id→trust_subject` refactor deferred.
- Updated Phase 1 schema step, the migration/non-regression item, and versioning (claim v2 +
  provenance v4; ABI 18 unchanged; blast radius larger by design).
- **No code changed** — still planning. **Next:** confirm the single D-1 sub-question
  (drop-v1 vs frozen-v1 reader); then implement Phase 1.

### 2026-06-25 — static review done; PLAN written; awaiting review gate
- Confirmed in code that `kind: app` is a **certification blocker owned by toolchain**: app
  deploy emits a binary + UNSIGNED provenance and **no author/cert claim**
  (`drift_deploy.py:2102` library-only sidecars, `:2334` no app cert claim);
  `verify_deployed_package` is `.zdmp`-only (`verify_deployed_v1.py:25-36`) so
  `verify-package`/`unpack` reject apps; no consumer app run/verify CLI exists.
- Verified the reuse surface: binary sha already computed (`:2302`); claim machinery is
  shape-agnostic; verification is `compose_verify` (no new crypto). Schema gaps pinned:
  `CertClaimBody` has no `artifact_kind`; `build_provenance` carries `artifact_kind`+
  `artifact_sha256` but no `source_content_id`; SCI is computable with `kind="app"`.
- Decided shape **A-lite** over B (binary-as-asset rejected — "disguise"): binary is the
  primary certified artifact; sidecars bind its exact hash; no `.zdmp`. Phase 1 = certify +
  `drift verify-app` (no exec), which unblocks pushcoin: they verify, then the orchestrator
  runs the binary itself. [SUPERSEDED: `drift run`/verify-then-exec is OUT of scope — Phase 1
  ends at `drift verify-app`; app execution stays the orchestrator's job.]
- Wrote `PLAN.md` with the agreed trust model, the 6 consumer checks (mapped to
  `compose_verify` reuse), and open decisions **D-1..D-4** to lock in review.
- **No code changed** — planning only. ABI expected to stay **18** (tooling + claim schema +
  verify + CLI; no runtime boundary).
- **Next:** one more review pass to sign off D-1..D-4 and the Phase-1 scope; then implement
  Phase 1.
