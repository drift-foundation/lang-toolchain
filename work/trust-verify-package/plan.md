# `drift trust verify-package` — plan

Read-only CLI verb to verify an already-deployed package artifact end to
end, without standing up a throwaway consumer project.

Status: IMPLEMENTED (patch-only, ABI unchanged — 0.33.8 → 0.33.9).
Additive CLI surface over the existing trust-v1 verifier; no change to
consume/build/deploy behavior, verifier acceptance semantics, package
format, runtime ABI, or compiler output.

---

## 1. Request (the thing to build)

```
drift trust verify-package <deployed-package-dir> \
    [--trust-store <path> | --author-pubkey-b64 <b64> | --author-profile <path>] \
    [--expect-version X] \
    [--expect-sci sha256:...] \
    [--json]
```

Takes the deployed package **directory** — the `drift deploy` output
layout:

```
<dir>/
├── <pkg>.zdmp                      # compressed artifact
├── <pkg>.author-claim              # author-claim sidecar
├── <pkg>.cert-claim.<kid>.json     # one per certifier
├── <pkg>.author-pubkey.b64         # author pubkey companion
├── <pkg>.author-profile            # author profile
├── provenance.zst                  # provenance record
└── assets/
```

The **directory is the verification unit**. A standalone `.zdmp` carries
none of its sidecars or provenance and cannot be verified in isolation —
so the verb accepts only the directory. Passing a `.zdmp` path should
fail with a one-line hint ("pass the package directory, not the
artifact"), not a generic error.

## 2. Context / motivation

While certifying & publishing **net-tls 0.5.2** to a staged libs dir on
**0.33.8+abi14**, we wanted a post-deploy "is this artifact intact and
correctly signed?" gate: author-claim and cert-claim signatures valid
against the author pubkey, and `version` / `source_content_id` (SCI) /
`artifact_sha256` consistent across author-claim ↔ cert-claim ↔
provenance ↔ the `.zdmp` bytes.

Publishers (us, mariadb next) and the certification orchestrator both
need this as a CI gate. The verification logic already exists and runs on
the consume path — this surfaces it as an operator-facing command. It is
**not** new crypto and **not** a looser trust path.

## 3. Gap analysis (verified against 0.33.8 source)

- `drift trust check` is a **repo/manifest-side preflight**
  (`lang/drift/trust.py`, parser at `lang/drift/cli.py:551`): its inputs
  are `--manifest` / `--trust-store`; it takes no published-artifact
  path.
- `drift doctor` inspects a **consumer workspace** (sources/lock/vendor),
  not a deployed artifact's claim signatures.
- The actual verifier, `verify_package_from_sidecars()`
  (`lang/driftc/packages/verify_v1.py:414`), is **internal plumbing** —
  its only callers are `provider_v1.py` (consumer load),
  `tools/drift_deploy/resolver.py` (index-time gate), and tests. No CLI
  surface reaches it.
- Today a publisher's only options are (a) a throwaway consumer project +
  trust-bootstrap + `drift prepare` to trigger resolver verification, or
  (b) calling `verify_v1` / `crypto.verify_ed25519` internals from a
  script. We did (b) to confirm the net-tls release; both are heavier
  than the task warrants.

## 4. What it verifies

1. **author-claim & cert-claim Ed25519 signatures** against the resolved
   key material, including **kid ↔ pubkey binding**.
2. **`source_content_id` equality** across author-claim and cert-claim.
3. **cert-claim `artifact_sha256` matches the on-disk `.zdmp` bytes**
   (sha256 of the decompressed payload).
4. **provenance `artifact_sha256` matches the on-disk `.zdmp` bytes**, if
   `provenance.zst` is present.
5. **version consistency** across manifest-recorded claims.
6. **trust-store role checks** (author vs certifier), with a clear
   diagnostic for the **dev / no-evidence sentinel** cert case.

### Explicit non-goal: consumer dep-graph closure (O3)

The verifier's `resolved_closure` argument checks a cert claim's
`dep_graph` against **a specific consumer's** resolved deps. Standalone
there is no consumer to check against — the existing index-time gate
already passes `resolved_closure=[]`, which makes that check pass
vacuously by design (`resolver.py:433`: "O3 NOT enforced here … enforced
at consumer load time"). `verify-package` therefore verifies artifact
integrity, signatures, key binding, SCI/version/provenance consistency,
and roles — **not** dep-graph closure, which is only meaningful against a
particular consumer. This is stated so a reviewer doesn't read its
absence as a gap.

## 5. Implementation plan

The verification core already exists; this is orchestration + one new
provenance cross-check + CLI wiring. The canonical template is the
`_load_verifier` closure in `tools/drift_deploy/resolver.py:413`.

### 5.1 Input handling
- Require a directory argument. Glob for `*.zdmp`; error clearly on zero
  or more than one (standard `lib/pkg/ver/` layout has exactly one).
- If the argument is a `*.zdmp` file, fail with a hint to pass its parent
  directory.

### 5.2 Verification core (reuse, don't reinvent)
Mirror `resolver._load_verifier` (`resolver.py:413-485`):
- `decompress_zdmp(path.read_bytes())`
  (`lang.driftc.packages.zdmp.decompress_zdmp`).
- Load the manifest
  (`lang.driftc.packages.dmir_pkg_v0.load_dmir_pkg_v0_from_bytes`).
- Build `PackageIdentity` (`verify_v1.py:72`):
  `package_id`, `version`, `source_content_id` from the manifest stamp,
  `artifact_sha256 = "sha256:" + sha256(decompressed bytes)`.
- Iterate `manifest["modules"]`; skip `*.__instantiations`. For each
  `module_id`, route reserved (`std`/`lang`/`drift` and their `.`
  prefixes) to the core trust store, others to the user trust store
  (same rule as `resolver.py:471-475`).
- Call `verify_package_from_sidecars(sidecar_dir=<dir>,
  package_identity=…, module_id=…, trust=…, resolved_closure=[])`
  per module. This delivers checks 1, 2, 3, 5, 6 via `compose_verify`
  (`verify_v1.py:192`).

### 5.3 Trust resolution (three mutually-exclusive forms)
- `--trust-store <path>` → `trust_v1.load_trust_store_json(path)`
  (`trust_v1.py:165`). Cleanest; used by CI with a known store.
- `--author-pubkey-b64 <b64>` → synthesize a single-key `TrustStore`
  granting that kid the roles needed to accept the package's module
  namespaces. **To confirm against the reference script**: in the
  foundation-bootstrap pattern the same kid plays both author and
  certifier — the synthetic store likely grants both for the package's
  module namespaces. Pin exact roles/namespaces from the script.
- `--author-profile <path>` → load the `<pkg>.author-profile` format and
  derive the pubkey/kid. **Format + loader to confirm** during impl.
- Default when none given: fall back to the project trust store
  (`drift/trust.json`) like `trust check`, or require one explicitly —
  decide from the reference script's behavior.

### 5.4 New logic: provenance cross-check (check 4)
Not covered by `compose_verify`. Read `provenance.zst` (zstd), extract
its recorded `artifact_sha256`, compare to the computed on-disk value.
Absent provenance → report as "not present / skipped", not a failure
(check is conditional per the spec). **Provenance record format to
confirm** during impl.

### 5.5 Assertions
- `--expect-version X` → assert manifest/claim version equals X.
- `--expect-sci sha256:…` → assert `PackageIdentity.source_content_id`
  equals the expected value.

### 5.6 Dev / no-evidence sentinel (check 6 nuance)
Surface the dev-lane sentinel cert (no real evidence) explicitly in the
diagnostic rather than silently treating it as a pass/fail. **Exact
sentinel marker to confirm** during impl (grep `verify_v1` /
`cert_suite` / trust paths).

### 5.7 Output
- Human form: per-module ✓/✗ lines + overall verdict, mirroring
  `trust check`'s renderer (`cli.py:943-955`).
- `--json`: one machine-readable result object for CI, e.g.
  `{ ok, package_id, version, source_content_id, artifact_sha256,
     author_kid, certifier_kid, mode, provenance_ok, modules: [...],
     reason }`. Final schema to settle with the orchestrator's needs.

### 5.8 Where the code lives  (AS BUILT)
The verification engine (`verify_v1`/`trust_v1`) and container reader
(`dmir_pkg_v0`/`zdmp`) are compiler-internal; the CLI layer
(`lang/drift`) is barred from importing them directly
(`test_import_boundaries.py::test_drift_layer_does_not_import_driftc_internals`).
So the orchestration lives in the package layer behind a single facade:

- `lang/driftc/packages/verify_deployed_v1.py` (NEW): `VerifyPackageOptions`
  + `verify_deployed_package(opts) -> report dict`. Freely uses the
  engine/reader (same layer). The operator-facing analog of
  `provider_v1`'s consumer-side use of the same verifier.
- `lang/drift/cli.py`: `verify-package` subparser under `trust_sub`
  (mutually-exclusive trust-form group) + dispatch branch. The CLI
  translates `--author-profile` → (pubkey_b64, namespaces) via
  `lang.drift.author_profile` and calls the facade; it never touches the
  engine directly.
- `lang/tests/driver/test_import_boundaries.py`: one allowlist entry
  (`lang.driftc.packages.verify_deployed_v1`) — the single sanctioned
  CLI↔packages coupling for this feature.
- `lang/drift/crypto` is the already-allowlisted neutral crypto leaf the
  facade uses for kid derivation; no new reverse dependency.

### 5.9 Exit codes
- `0` verified; `1` verification failed (signature/SCI/artifact/version/
  role/provenance mismatch, or an `--expect-*` assertion failed);
  `2` usage error (bad dir, no/many `.zdmp`, conflicting trust flags).

## 5b. Review hardening (applied)

Code review found four issues; all fixed without forking the verifier —
the facade stays a thin harness around `verify_package_from_sidecars`.

- **HIGH — provenance was not pinned to the signed cert.** The first cut
  compared only the bundle's unsigned inner `provenance.artifact_sha256`.
  Deploy signs `cert.body.evidence_sha256 = sha256(<on-disk
  provenance.zst bytes>)` (drift_deploy.py:1539), so a hostile mirror
  could swap the bundle keeping the inner field. Fix: locate the
  accepted cert (by the `certifier_kid` that `verify_package_from_sidecars`
  already accepted — no re-verification) and assert
  `sha256(provenance.zst bytes) == accepted_cert.body.evidence_sha256`
  (authoritative), with the inner-field check kept as a secondary
  cross-binding. Regression:
  `test_provenance_content_tamper_preserving_artifact_sha_fails`.
- **HIGH — reserved namespaces used the wrong trust policy.** One store
  was used for all modules; `std.*`/`lang.*`/`drift.*` must route to
  `load_core_trust_store()`. Fix: reuse the consumer path's predicate
  `provider_v1._module_is_reserved` verbatim and route reserved modules
  to the core store. Regression:
  `test_reserved_namespace_module_routes_to_core_trust`.
- **MEDIUM — bundled-pubkey default could be misread as "trusted".** No
  trust source is now a usage error (exit 2); the bundled self-
  consistency mode is opt-in via `--allow-bundled-pubkey` and still
  warns it is not third-party trust. Regression:
  `test_no_trust_source_is_usage_error`.
- **LOW — version bump.** `lang/versions.py` → `0.33.9` (ABI stays 14).

Second review pass:

- **HIGH — provenance binding only checked one certifier.** The first
  hardening resolved a single accepted cert from the last successful
  module's top-level kid. A multi-module package can verify different
  modules through different cert claims, each pinning the provenance.
  Fix: collect EVERY accepted cert (signer kid → claim, deduped) and
  require `sha256(provenance.zst) == evidence_sha256` for all of them
  (report key `certifier_kids`; sentinel surfacing also spans all).
  Regression `test_provenance_binding_holds_for_every_accepted_cert`
  puts the wrong-evidence cert on a NON-last module (two certifiers via
  a real trust store) so the old last-kid logic would have passed.
- **LOW — stale docstring.** `VerifyPackageOptions` doc said no trust
  source falls back to the bundled pubkey; updated to the
  `--allow-bundled-pubkey` opt-in (usage error otherwise).

Third review pass — shared-harness refactor + exit-code contract:

- **STRUCTURE — split-brain verifier orchestration.** The facade had
  reconstructed the verifier's caller harness (build identity, enumerate
  modules, route reserved namespaces to core trust, call
  `verify_package_from_sidecars`) — the third copy alongside
  `provider_v1.load_package_v1_with_policy` and
  `resolver._load_verifier`. The setup, not just the crypto, was the
  drift risk. Fix: extracted `lang/driftc/packages/verify_harness_v1.py`
  (`module_is_reserved`, `iter_trust_module_ids`,
  `build_package_identity`, `verify_package_modules`, `first_failure`).
  All three callers now funnel through it. The CLI facade keeps only
  directory handling, trust-source selection, provenance/expect checks,
  sentinel surfacing, and reporting.
- **MEDIUM — accepted cert inferred by signer kid.** The facade re-located
  the accepted cert by mapping signer kid → first sidecar claim with that
  signature, which is only safe if a kid signs exactly one claim. Fix:
  `verify_v1.VerifyResult` now carries `accepted_cert_claim` (the exact
  claim `compose_verify` accepted on the certifier-shortcut path); the
  facade reads that, deduped by `(kid, evidence_sha256)`. No more
  reconstruction.
- **MEDIUM — exit-code contract.** The CLI mapped ALL facade exceptions
  to argparse usage (exit 2, no JSON even under `--json`). New contract:
  exit 0 verified; exit 1 verification ran and the PACKAGE is invalid /
  incomplete (corrupt artifact, bad/missing/malformed sidecars, trust
  rejection, provenance mismatch, bundled-pubkey missing) — always an
  `ok=false` report, valid JSON under `--json`; exit 2 ONLY for command-
  invocation problems (not a directory / is a `.zdmp`, zero/many `.zdmp`,
  no/conflicting trust source, unreadable CLI trust input). Mechanism:
  the facade folds package-content problems into the report and raises a
  dedicated `VerifyPackageUsageError` (subclass of `ValueError`) only for
  invocation problems; the CLI maps `VerifyPackageUsageError` → exit 2
  and converts any other escaping exception into an `ok=false` report
  (exit 1) instead of routing it through `argparse.p.error`. Regressions:
  `test_malformed_artifact_is_verification_failure_not_usage`,
  `test_malformed_author_sidecar_is_verification_failure`,
  `test_missing_author_sidecar_is_verification_failure`,
  `test_missing_bundled_pubkey_is_verification_failure`,
  `test_not_a_directory_is_usage_error`.

Fourth review pass — exit-code determinism + backstop schema:

- **MEDIUM — no-trust-source not consistently exit 2.** The facade parsed
  / decompressed the artifact before validating the trust source, so
  `verify-package <corrupt-dir>` with no trust flag returned exit 1
  (`malformed-artifact`) instead of exit 2. Trust-source presence/conflict
  is purely an invocation property, so it is now validated in a new step
  1b BEFORE the artifact is read (the store is still BUILT later, where the
  manifest's module ids are available as default namespaces). Regression
  `test_no_trust_source_is_usage_error_even_for_corrupt_package`.
- **LOW — backstop report shape differed.** The CLI's unexpected-error
  backstop emitted only `{ok, package_dir, errors}`. The report schema is
  now a single source — `verify_deployed_v1.new_report()` /
  `error_report()` — used by both `verify_deployed_package` and the CLI
  backstop, so every failure (anticipated or not) carries the same keys.
  Regression `test_backstop_error_report_has_full_schema`.

Fifth review pass — invocation-input ordering + CLI-path backstop test:

- **MEDIUM — bad CLI trust inputs masked by corrupt package bytes.** Step
  1b validated trust-source PRESENCE before artifact IO, but the actual
  loading of `--trust-store` and validation of `--author-pubkey-b64` still
  happened in step 3, AFTER decompress — so a corrupt package returned
  exit 1 `malformed-artifact` instead of exit 2 for the bad input. Fix:
  step 1b now LOADS `--trust-store` and VALIDATES `--author-pubkey-b64`
  (via a new `_decode_ed25519_pubkey` split out of
  `_synth_single_key_trust_store`) before any artifact read; only
  namespace defaulting (needs the manifest's module ids) and the bundled-
  pubkey path (package content) stay in step 3. Regressions
  `test_bad_trust_store_path_is_usage_error_even_for_corrupt_package`,
  `test_malformed_author_pubkey_is_usage_error_even_for_corrupt_package`.
- **LOW — backstop test pinned the helper, not the CLI.** Added
  `test_json_backstop_emits_full_schema_on_unexpected_error`, which
  monkeypatches `verify_deployed_package` to raise an unexpected error and
  asserts the real CLI `--json` path emits one full-schema `ok=false`
  report with exit 1.

Sixth review pass — pubkey decode contract + facade source-exclusivity:

- **MEDIUM — invalid-base64 `--author-pubkey-b64` could escape to exit 1.**
  `base64.b64decode(..., validate=True)` raises `binascii.Error` for bad
  input; it happens to subclass `ValueError` (so step 1b caught it today),
  but relying on that was fragile. Fix: `_decode_ed25519_pubkey` now wraps
  decode failures and re-raises a plain `ValueError`, GUARANTEEING its
  error contract platform-independently. Regression
  `test_invalid_base64_author_pubkey_is_usage_error`.
- **LOW — facade didn't enforce all trust-source conflicts.** It checked
  only `trust_store_path` + `author_pubkey_b64`; `allow_bundled_pubkey`
  alongside an explicit source was silently ignored (explicit preferred).
  As the sanctioned integration surface, the facade now enforces "exactly
  one trust source" over all three. Regression
  `test_facade_rejects_multiple_trust_sources` (direct API, bypassing the
  CLI's argparse mutually-exclusive group).

## 6. Testing
- Unit: a `drift deploy` fixture dir that verifies clean; mutations that
  each trip exactly one gate (bad author sig, bad cert sig, SCI mismatch,
  artifact_sha256 tamper, provenance tamper, wrong version, wrong-role
  kid, dev sentinel). Reuse fixtures from
  `lang/tests/packages/test_verify_v1.py` and
  `lang/tests/driver/test_deploy_stdlib_roles.py`.
- Regression self-check: one CLI-level test must create or copy a
  known-good deployed package directory, assert
  `drift trust verify-package <dir>` succeeds, then apply targeted
  on-disk mutations and assert the command fails with the expected gate
  name for each mutation. This is the guard that proves the new CLI is
  actually validating the package/sidecar/provenance set, not merely
  parsing it or returning the existing happy path. At minimum cover:
  `.zdmp` byte tamper, cert-claim `artifact_sha256` tamper,
  author/cert SCI mismatch, wrong expected version, and provenance
  `artifact_sha256` mismatch when `provenance.zst` is present.
- `--json` schema stability test for the CI consumers.
- Lives in `lang/tests/driver/` (CLI-level) + `lang/tests/packages/`
  (verify-core reuse).

## 7. Needs / open items
- **The ad-hoc verifier script** the team used for net-tls 0.5.2 — it is
  the behavioral reference for: the provenance cross-check, the dev/
  no-evidence sentinel diagnostic, and exactly how `--author-pubkey-b64`
  / `--author-profile` synthesize the trust store and resolve roles.
- Confirm `provenance.zst` record format and field name for
  `artifact_sha256`.
- Confirm `--author-profile` file format + loader.
- Settle the `--json` schema with the certification orchestrator.
