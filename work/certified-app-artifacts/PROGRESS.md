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
| P1: cert_claim_v1 → v2 (`artifact_kind` + signed `artifact_path`, reject v1) | TODO |
| P1: provenance → v4 (required `source_content_id`) | TODO |
| P1: deploy app-claim emission (author+cert+signed provenance) | TODO |
| P1: verify adapter + `verify_deployed_package` app branch | TODO |
| P1: `drift verify-app` CLI | TODO |
| P1: update claim-constructing test harnesses to v2 (+ artifact_kind) | TODO — known: test_trust_verify_package_cli, test_unpack_cli |
| P1: regression set (positive/negative/migration/boundary) | TODO |
| Phase 1: app author+cert claims + signed provenance | not started (gated on review) |
| Phase 1: `verify_deployed_package` app branch (reuse compose_verify) | not started |
| Phase 1: `drift verify-app` CLI | not started |
| Phase 1 regressions (positive/negative/back-compat/boundary) | not started |
| Phase 2: `drift run` (verify-then-exec verified copy) | deferred (designed, not built) |
| Phase 2: verified app assets (fold into SCI; drop loose app staging) | deferred |
| Version bump (DRIFTC; ABI 18 unchanged) | not started |

## Release-sequencing constraint (user, 2026-06-25)
Phase 0 may be a SEPARATE COMMIT, but **do NOT release / cert-pool-cut Phase 0 alone.** The
package SCI flip (`library→package`, changes source identity) must ship TOGETHER with the v2
claim/provenance break (Phase 1 schema) — otherwise an interim state exists with package-kind
SCI but old v1 claims. Implement Phase 0 then Phase 1 on the **same branch/slice**, ONE
version bump + ONE toolchain publish at the end.

## Log

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
  `drift verify-app` (no exec), which alone unblocks pushcoin (verify-then-run themselves).
  Phase 2 = `drift run` + verified app assets.
- Wrote `PLAN.md` with the agreed trust model, the 6 consumer checks (mapped to
  `compose_verify` reuse), and open decisions **D-1..D-4** to lock in review.
- **No code changed** — planning only. ABI expected to stay **18** (tooling + claim schema +
  verify + CLI; no runtime boundary).
- **Next:** one more review pass to sign off D-1..D-4 and the Phase-1 scope; then implement
  Phase 1.
