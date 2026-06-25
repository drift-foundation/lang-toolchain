# PLAN — certified runnable app artifacts (`kind: app`) + `drift verify-app`

**Classification:** FEATURE / trust-v1 extension to **app** artifacts. Boundary-contract
change (signed sidecar set for apps, cert-claim body schema, a new verify mode, a new
consumer CLT surface). NOT a LANGUAGE_BUG.
**Requested by (indirectly):** pushcoin — a certified-path runnable `microflows-daemon`
(`2026-06-25T17:41:04Z-pushcoin-release-notes.md`). The daemon's source/HTTP/operation
protocol are drift-workflows' to own; the **certified distribution mechanism** is ours.
**Determination (static review, this slice's premise):** `kind: app` artifacts can be
built + deployed but **cannot be certified or verify-consumed from the cert pool today** —
a toolchain/Foundation blocker, not a workflows ownership issue.

## Goal
A runnable app binary that is a **first-class certified artifact**: its exact executable
bytes + source identity/provenance are cryptographically bound by author + cert claims and
checkable by a consumer, **without embedding the binary in a `.zdmp`** (sidecars bind its
exact hash). Phase 1 delivers *verify*; Phase 2 delivers *run* + verified app assets.

## Established facts (code-grounded, 2026-06-25)
1. **Apps deploy but aren't certified.** `_deploy_artifact_impl`: author claim + v1 trust
   sidecars are LIBRARY-only (`drift_deploy.py:2102` `if art.kind == "library"`), and app
   artifacts produce **no cert claim** — `drift_deploy.py:2334` ("v1: app artifacts do not
   currently produce a cert claim … out of scope for the trust-v1 slice. The unsigned
   provenance bundle is still emitted"). A deployed app dir is
   `{<binary>, <app>.provenance.zst (UNSIGNED), assets/… (loose)}`.
2. **Verify is `.zdmp`-only.** `verify_deployed_package` hard-globs exactly one `*.zdmp`
   and hashes the decompressed `.dmp` (`verify_deployed_v1.py:25-36`) → it (and
   `drift unpack`, `drift trust verify-package`) **reject app dirs**. No consumer-side
   app run/verify/install CLI exists.
3. **The binary hash is already computed** at deploy: `app_sha256` over the binary bytes
   (`drift_deploy.py:2302`), and fed into provenance `artifact_sha256` (`:2316`).
4. **Claim machinery reuses crypto, but the verifier is module-shaped.** Author/cert claims
   are signed JSON sidecars binding `package_id/version/artifact_sha256/source_content_id`;
   they do NOT require a `.dmp` (`make_author_claim` / `make_cert_claim`). The two-role trust
   model (authors vs certifiers, per-namespace kid lists) is already structurally enforced.
   BUT the composition verifier is **package/module-shaped, not artifact-agnostic**:
   `verify_v1.compose_verify` takes `module_id: str` and emits "module/package load"
   diagnostics (`verify_v1.py:202+`), and `verify_harness_v1.verify_package_modules` routes
   trust **per module** with reserved-namespace handling (`verify_harness_v1.py:135+`). So
   the SIGNATURE/crypto layer reuses cleanly, but Phase 1 needs an **app adapter** (synthesize
   a single trust subject for the app) OR a small `module_id → trust_subject` generalization.
   "All via compose_verify" is *not* free — this is explicit Phase-1 + regression scope (see
   §Finding-2 / D-5).
5. **Both signed bodies are STRICT-v1 and neither carries a kind.** `CertClaimBody` =
   `{package_id, version, artifact_sha256, source_content_id, target, toolchain, dep_graph,
   cert_suite, run_id, run_started_utc, evidence_sha256}` — no `artifact_kind`
   (`cert_claim_v1.py:193-206`). `AuthorClaimBody` = `{schema_version, package_id, version,
   namespaces, source_content_id, required_deps, release_utc}` — no kind either. Both reject
   unknown keys (`author_claim_v1.py:101-116` `_reject_unknown_keys`; cert claim has the
   peer guard), so adding a field is a real allowed-key-set schema change in BOTH, not a
   silent add. `build_provenance` carries `artifact_kind` + `artifact_sha256` but **not**
   `source_content_id` (`provenance.py:90-129`; its `source` is vcs/commit only).
6. **SCI is computable for apps** today via `compute_source_content_id(kind="app", …)` —
   the helper is kind-generic; only `compute_artifact_sci` hardcodes `kind="library"` and
   callers gate on `art.kind == "library"`.

## Agreed trust shape (the certified-app model)
- **Author claim** (signed by **workflows** author kid): "I authored `microflows-daemon@0.2.0`
  from this `source_content_id`." Source identity, not binary bytes.
- **Cert claim** (signed by **orch / cert-pool** certifier kid): "I certified
  `microflows-daemon@0.2.0`; `artifact_sha256 = sha256:<binary>`; from `source_content_id …`;
  passed suite X on toolchain Y; dep_graph = resolved closure." Binds the **exact executable**.
- **Provenance** (now SIGNED-bound): records build inputs/deps/toolchain/target/`artifact_kind=app`;
  its bytes are bound by the cert claim `evidence_sha256`, and its `artifact_sha256` cross-checks
  the binary hash.
- The binary is **not** embedded in a `.zdmp`; it carries the sidecar set
  `{<app>.author-claim, <app>.cert-claim.<kid>.json, <app>.provenance.zst, <app>.author-pubkey.b64}`.

### Consumer verification (`drift verify-app <app-dir>`) — crypto via `compose_verify`, app-shaped subject
1. Binary's current sha256 == cert-claim `artifact_sha256`.
2. Cert-claim signature from a trusted **certifier** key (orch/cert pool) for the namespace.
3. Author-claim signature from a trusted **author** key (workflows) for the namespace.
4. SCI equality across the available legs (see D-2).
5. Provenance inner `artifact_sha256` == binary hash; cert-claim `evidence_sha256` == sha256(provenance.zst).
6. Dependency/cert closure valid (the certified libs the app links).

## Open decisions (LOCK in review before code)
- **D-1 — `artifact_kind` is REQUIRED in signed author + cert bodies; bump the claim schema
   (RESOLVED in review).** Add `artifact_kind: "package" | "app"` (NO `library` value in
   signed v2 claims) to BOTH the author-claim and
   cert-claim signed bodies and **bump the claim `schema_version` → 2**. It is REQUIRED in v2
   — **no optional / default `"package"` path.** `drift verify-app` requires
   `artifact_kind == "app"` in BOTH claims and **rejects old package-only / v1 claims rather
   than guessing**; `compose_verify` asserts author-kind == cert-kind. **Existing library
   compatibility is NOT required for this slice.**
  - GATE RESOLVED (review): **drop v1 — clean schema break, no v1-legacy reader.** Existing
    certified artifacts re-issue as v2 claims (pool-wide re-cert accepted). v1 author/cert
    claims are rejected outright.
- **D-2 — `source_content_id` is REQUIRED in provenance for certified artifacts; bump
   provenance schema (RESOLVED in review).** Add `source_content_id` to `build_provenance` and
   **bump provenance `schema_version` → 4**, REQUIRED (no optional@v3 path). `verify-app`
   requires **author == cert == provenance SCI**; a missing provenance SCI is a **verification
   failure** for app artifacts (NO two-way mode). Libraries also emit provenance SCI (already
   available to them) at v4; existing provenance readers/tests move to the v4 shape.
- **D-3 — exact app trust SUBJECT syntax (per review).** `module_namespace` (`microflows.*`)
  and app/`package_id` (`microflows-daemon`) are **not interchangeable**. **Canonical app
  trust subject = the declared `module_namespace`** (the prefix-routable form the trust store
  already understands); `package_id` is NOT used for routing. **Require `module_namespace`
  for a certified app** — if absent, **reject** at deploy (no implicit `package_id`
  fallback). The app adapter (D-5) feeds this single subject into the verifier.
  - **Orthogonal to the artifact id (D-7):** the app id `art.name` (`uflowsd`) is the
    filesystem/sidecar/`package_id` identity; the `module_namespace` (`microflows.*`) is the
    trust-routing subject. `uflowsd` and `microflows.*` are deliberately decoupled — the
    binary's filename identity must never be used for trust routing.
- **D-4 — SIGNED binary locator (per review — "file named artifact_name" is too loose).**
  Platform naming / future wrappers mean the binary filename may not equal `artifact_name`,
  and an unsigned locator lets an attacker redirect verify to a different file. **Add a
  SIGNED `artifact_path` (project/deploy-relative) to the cert-claim body** (covered by the
  cert signature, alongside `artifact_kind`); verify resolves the binary by that signed path,
  hashes it, compares to `artifact_sha256`. Deploy writes the binary at that path and signs
  it. (Simpler alternative if we constrain layout: require exactly one executable primary
  artifact named `== package_id`, still recorded in the signed body so the locator is
  attested — NOT inferred. No reliance on unsigned provenance for the locator.)
- **D-5 — app verify adapter vs `module_id → trust_subject` refactor (NEW, per review).**
  `compose_verify`/`verify_package_modules` are module-shaped (`module_id`, "module load"
  diagnostics, per-module reserved routing). Phase 1 must EITHER (a) add a thin **app adapter**
  that calls the verifier with a single synthetic subject (the D-3 `module_namespace`) and
  app-flavored diagnostics, OR (b) do a small **`module_id → trust_subject`** generalization
  in `verify_v1`/`verify_harness_v1`. **Recommend (a) adapter for Phase 1** (smallest blast
  radius, leaves the package/importable path untouched); (b) is a follow-up if a second app-shaped
  caller appears. Either way this is explicit Phase-1 + regression scope.
- **D-6 — `package` is the CANONICAL importable kind in v2 (RESOLVED in review); flip the
  current `library`-canonical normalization.** These artifacts are certified Drift packages
  (modules + contract assets like SQL schemas/migrations/templates/docs), not C-style
  libraries. The v2 vocabulary is `artifact_kind: "package" | "app"` (package = importable/
  distributable, may include assets; app = runnable binary/service).
  - **This REVERSES today's code:** `manifest.py:108,124,235-238` currently make `library`
    canonical and treat `package` as the DEPRECATED alias (warns "'kind: package' is
    deprecated; use 'kind: library'"). Flip it: **`package` canonical, `library` a temporary
    parser alias** (normalize `library → package`; flip/relax the deprecation warning).
  - **Signed canonical identity is `package`.** `compute_artifact_sci` (`manifest.py:687`)
    and `SourceContentInputs.kind` (`source_content_id.py:166`) change `"library" → "package"`.
    `"kind"` IS hashed into the canonical SCI body (`source_content_id.py` canonical dict),
    so **every importable artifact's `source_content_id` changes** — a source-identity break.
    This is intentionally part of the SAME v2 pool re-cert (D-1 drop-v1), the right moment.
  - **Branch sweep:** every `art.kind == "library"` check becomes `== "package"` (canonical
    after normalization) — `drift_deploy.py`, `drift_build.py:478,543,605`,
    `drift_prepare.py:265,286`, `manifest.py:124,212,214,235-239,676,687`. Prefer routing
    through a single `is_importable_kind()`/canonical-constant helper over scattering string
    literals. (NB: `_resolve_source_path(kind="module"/"asset")` in `source_content_id.py` is
    a DIFFERENT `kind` axis — path-kind, not artifact-kind — and is untouched.)
- **D-7 — deploy category `lib/` → `pkg/` (+ `app/`) (RESOLVED in review).** Canonical v2
  layout:
  - `<deploy-root>/pkg/<package-id>/<version>/…` (importable certified packages)
  - `<deploy-root>/app/<app-id>/<version>/…` (runnable app artifacts)
  Rationale: once packages carry SQL/schema/assets, `lib/` is misleading the same way
  `"library"` is; `pkg/` says "importable certified package" and aligns with the canonical
  `DRIFT_PACKAGE_ROOT` env. **Do NOT emit new certified packages under `lib/`**; existing
  `lib/` paths are pre-v2/stale and reissued through cert.
  - **Artifact id names the path/binary/sidecars, independent of the project name.** The app
    deploy id is `art.name`. A project `microflows` whose runnable artifact is `uflowsd`
    deploys as two SEPARATE trees:
    ```
    $DRIFT_ROOT/pkg/microflows/0.2.0/    # importable package + schema assets
    $DRIFT_ROOT/app/uflowsd/0.2.0/       # runnable daemon
          uflowsd                        # the binary (== art.name)
          uflowsd.author-claim
          uflowsd.cert-claim.<kid>.json
          uflowsd.provenance.zst
          uflowsd.author-pubkey.b64
    ```
    Matches the code: `_publish_app` writes `app_dest/<art.name>/<version>`, binary at
    `staged_install/<art.name>` (`drift_deploy.py:2300,2828`); app sidecars follow the same
    `<art.name>.<sidecar>` naming libraries use for `<pkg-id>.<sidecar>`. So `claim.package_id
    == <art.name>` (the app id `uflowsd`), the signed `artifact_path` (D-4) is `<art.name>`,
    and the app id is NOT the project/package name. **Orthogonal to the trust subject** — see
    D-3: who may author/certify `uflowsd` is governed by its declared `module_namespace`
    (e.g. `microflows.*`), not by the artifact id.
  - **Small CODE delta (important):** the deploy tool already takes caller-supplied `--dest`
    (packages) and `--app-dest` (apps) as SEPARATE args (`drift_deploy.py:2426,2431,2477-2481`);
    there is **no hardcoded `"lib"` in the publish path** (`_publish_package`/`_publish_app`
    write `<dest|app_dest>/<name>/<version>`). So the category name is whatever the caller
    passes. The rename is therefore **convention + docs + tests + ORCH coordination**, not
    publish-code surgery:
    - ORCH passes `--dest <root>/pkg` (was `<root>/lib`) — ORCH-side coordination dependency.
    - Update the `lib/` convention in docs/comments (`resolver.py:320` "run `lib/` root"),
      deploy help text, `RUN_LOCAL`, and test harnesses hardcoding `tmp_path/"lib"`
      (e.g. `test_trust_verify_package_cli.py::_build_good_dir`).
  - `discover_package_files` globs whatever roots it is given (layout-name-agnostic,
    `provider_v1.py:73`) → consumers just point `--package-root`/`$DRIFT_PACKAGE_ROOT` at
    `pkg/`; **no loader change.**
  - Naming: **`pkg`** chosen over `lib` (and over `packages`) per review. The canonical
    package-root env stays **`DRIFT_PACKAGE_ROOT`** (the real one). Do NOT canonize
    `DRIFT_PKG_ROOT` — it is not a real drift-lang env var (only a stray `drift unpack`
    docstring + pushcoin's note); fix that docstring to `DRIFT_PACKAGE_ROOT`. No
    `DRIFT_APP_ROOT` is introduced.

## Phasing
- **Phase 0 — v2 vocabulary/layout break (shared prerequisite; D-6 + D-7):** flip canonical
  kind `library → package` (manifest normalization, `compute_artifact_sci`/`SourceContentInputs`
  kind, the `art.kind == "library"` branch sweep via a canonical helper) and the deploy
  category `lib/ → pkg/` + `app/` (convention/docs/tests + ORCH coordination; no publish-code
  literal to change). This is the source-identity break that forces the pool re-cert; the
  app-cert work (Phase 1) rides on top. **Could land as its own commit before Phase 1** —
  decide at the gate (one slice vs prerequisite slice).
- **Phase 1 (unblocks pushcoin) — certify + verify, no exec, no container:**
  1. App deploy emits author + cert claims over the binary hash + app SCI; signs/binds the
     provenance (closes the unsigned-provenance gap). (`drift_deploy.py` app branch, parallel
     to the package branch; reuse `make_author_claim`/`make_cert_claim`/cert sidecar naming.)
     Requires a signing key for apps (current code gates sidecars on the importable kind at
     `:2102`). `claim.package_id == art.name` (app id, e.g. `uflowsd`); signed
     `artifact_path == art.name`.
  2. Schema (clean bumps, no compat shims): `artifact_kind` REQUIRED in BOTH author + cert
     bodies at claim `schema_version` 2 (D-1, strict allowed-key-set + parser allow-list
     update); signed `artifact_path` in the cert body (D-4); `source_content_id` REQUIRED in
     `build_provenance` at provenance `schema_version` 4 (D-2); compute app SCI via
     `compute_source_content_id(kind="app", …)`; require + route on `module_namespace` (D-3).
  3. App verify ADAPTER (D-5): synthesize the single `module_namespace` trust subject, call
     the existing signature/`compose_verify` crypto with app-flavored diagnostics. `verify_
     deployed_package` gains a kind-gated branch: read signed `artifact_kind`+`artifact_path`,
     locate + hash the binary, verify. Package/importable path untouched.
  4. `drift verify-app <app-dir>` CLI — pure verify, mirrors `drift trust verify-package`
     (same trust-source flags, JSON, exit codes). NO execution.
- **Phase 2 (later):**
  5. `drift run <app-dir> -- <args>` — verify-then-exec the VERIFIED copy (materialize to a
     verified path, re-hash-before-exec; same TOCTOU discipline `drift unpack` uses). Explicit
     exec-trust decision required.
  6. Verified app assets — fold app asset `(path, sha)` into the app SCI / a signed asset
     list so a daemon's deployment manifest stops being loose+unverified; stop loose
     `_stage_assets` for apps once covered. (Auxiliary to the app artifact, not a disguise.)

## Explicitly NOT in scope
- The `microflows-daemon` source, its `/v1/workflows` HTTP surface, the participant
  operation-protocol envelope (`pending|succeeded|failed|indeterminate`, `result`/`reason`,
  404-vs-indeterminate) — drift-workflows owns these (pushcoin's Q2/Q3).
- Embedding the binary in a `.zdmp` (rejected: "binary disguised as an asset" — trust
  statements must be about the real artifact).
- Phase 2 (`drift run`, verified app assets) — designed here, built after Phase 1 lands.

## Versioning / blast radius
- `DRIFTC_VERSION` bumps (new behavior). **`DRIFT_RT_ABI` unchanged** — deploy tooling +
  sidecar/claim schema + provenance schema + verify + CLI; no runtime boundary.
- **Claim `schema_version` → 2** (required `artifact_kind` in both bodies) and **provenance
  `schema_version` → 4** (required `source_content_id`). Sidecar/provenance schema bumps, NOT
  runtime ABI.
- Blast radius is **larger than the asset slice** and that is by design (sharpness over
  back-compat, per review): **clean v1 break — no legacy reader.** Every existing certified
  artifact re-issues as a v2 `package` claim (pool-wide re-cert); v1 author/cert claims are
  rejected outright. The package deploy/verify/unpack *code* paths stay behavior-equivalent
  (only the canonical kind/layout names and the schema versions change).

## Regression-first plan (AGENTS.md)
1. Positive: `drift deploy` an app → `drift verify-app` passes (author+cert+SCI+provenance+
   dep-closure), via the signed-primitives harness (reuse `test_trust_verify_package_cli.py`
   shape, artifact = binary; subject = `module_namespace`).
2. Negative: tampered binary → fail; cert/author key not trusted for the `module_namespace`
   → fail; SCI mismatch (any leg) → fail; **author-kind != cert-kind → fail**; **signed
   `artifact_path` pointing off the real binary → fail**; nothing materialized/executed.
3. Migration + non-regression (CRITICAL — claim schema v2 + provenance v4 + module-shaped
   verifier all touched): the package `compose_verify`/`verify_package_modules`/`unpack`
   *code* path is behavior-equivalent under the app adapter (D-5). Clean v1 break (no legacy
   reader): **v1 author/cert claims are rejected cleanly**, and a **re-issued v2 `package`
   claim verifies** (package deploy→re-cert→`verify-package` round-trip pinned at v2 with
   `artifact_kind="package"` + provenance v4 required SCI).
4. Boundary contract: positive+negative schema tests for the author AND cert body
   `artifact_kind`/`artifact_path` additions (unknown-key strictness preserved; signed
   `artifact_kind` accepts only `"package"`/`"app"`, never `"library"`); a provenance v4 test
   (required SCI present → OK, absent → fail); update the now-stale "app artifacts are out of
   scope for the trust-v1 slice" comment at `drift_deploy.py:2334` and the importable-only
   sidecar comment at `:2102`.

## Validation criteria (definition of done — Phase 1)
- An app deployed with a signing key produces `{binary, author-claim, cert-claim.<kid>.json,
  provenance.zst, author-pubkey.b64}`; `drift verify-app` returns OK with the binary identity.
- Tamper/untrusted-key/SCI-mismatch → clean exit-1 failure.
- Package deploy/verify/unpack suites stay green (re-pointed to `package`/`pkg`). **v1 claims
  reject cleanly; re-issued v2 `package` claims verify; no frozen legacy reader.**
- `DRIFT_RT_ABI` stays 18.
