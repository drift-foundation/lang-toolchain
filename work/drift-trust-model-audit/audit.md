# Drift trust model — audit & minimal-path doc

Date: 2026-05-18
Status: design / audit, not a patch.
Driver: deploy-side trust patches have closed two failure modes
(shadowing, deploy-signer leakage into external dep namespaces), but
each surfaced a deeper gap.  Before any more patches, document what
the trust model actually is today, what we want it to be, and what
the smallest move toward the target is.

## 1. Current reality

### 1.1 Artifacts and what they cover

| Artifact | Coverage | Signer | Verifier consults? | Citation |
| -------- | -------- | ------ | ------------------ | -------- |
| `.sig` (v2 envelope) | digest of decompressed `.dmp` + optional `author_profile_sha256` + optional `provenance_sha256` | one Ed25519 kid; role unlabeled | YES (only trust gate driftc uses) | `lang/driftc/packages/signature_v0.py:44-74, 118-194` |
| `.author-profile` | declared namespaces + signing kid | author | indirect: digest stamped into `.sig` envelope; `drift verify` cross-checks `entry.kid == profile.kid` but `drift deploy` does NOT | `lang/drift/author_profile.py:31-72`; `lang/drift/cli.py:325-341` |
| `.source-attestation` | `source_content_id` (hash of canonical source/build inputs: modules+sha256, declared deps+ranges, unsafe, assets, target_class), `required_deps`, `target_class` | same key as `.sig` today; lock schema has separate `source_attestation_key` slot but emit reuses one seed | NO — `driftc` never reads it; `drift_deploy` resolver consumes it in source-rebuild mode | `tools/drift_deploy/source_attestation.py:319-466`; `tools/drift_deploy/drift_deploy.py:1170-1210` |
| `.provenance.zst` | artifact_sha256, compiler version/abi/commit, build_utc, resolved dep map (`pkg_id → {version, sha256}`), optional VCS metadata, transitive dep provenance/keys bundle | unsigned independently; bound via `provenance_sha256` in `.sig` envelope | integrity only (re-hashes, never parses contents) | `tools/drift_deploy/provenance.py:90-186`; `lang/driftc/packages/signature_v0.py:59-74, 311-333` |
| run snapshot | per-package `source_content_id`, `author_key`, `source_attestation_key` (equality gates); optional `sha256` (evidence-only) | UNSIGNED; explicitly run-local | source-rebuild flow only; cannot cross run boundary | `tools/drift_deploy/run_snapshot.py:1-309`; `tools/drift_deploy/resolver.py:805-815` |

### 1.2 Consumer trust enforcement (`driftc` at package load)

`load_package_v0_with_policy` → `verify_package_signatures` (`provider_v0.py:659-720`):
1.  Hashes `.dmp` bytes, reads `.sig` sidecar.
2.  Cross-checks `.provenance.zst` / `.author-profile` digests against the signed envelope (integrity only).
3.  Reconstructs envelope (v0/v1/v2) and ed25519-verifies each entry against keys in `core_trust` (for reserved namespaces) or the user trust store.
4.  For every `module_id`, requires that AT LEAST ONE verified kid is in `allowed_kids_for_module(mid)`.

The trust store (`trust_v0.py:38-79`) is a flat `{kid → namespaces}` allow-list.  No role partition (author vs certifier).  No quorum logic.  No source-attestation chaining.

### 1.3 Authority overlap today

- **Namespace owner** = `profile.kid` from `.author-profile`.
- **Artifact signer** = whoever holds `--sign-key-file`.
- **Source-attestation signer** = same `--sign-key-file` (lock schema separates the slots; emit doesn't).
- **Certifier** = no such role exists in the trust model.

`drift verify` enforces `entry.kid == profile.kid` (`lang/drift/cli.py:329`).  `drift deploy` does NOT.  So a deploy can produce a `.sig` whose kid is unrelated to the author profile, and the consumer-side gate only checks `kid ∈ allowed_kids_for_module(mid)` — it doesn't reconcile with the `.author-profile`.

The code itself flags the unified-key shortcut explicitly (`drift_deploy.py:1170-1174`):

> Signed with the same key as the artifact today (single signer); the lock records this key separately as `source_attestation_key` so the two roles can diverge later (e.g. long-lived org identity key signs source, ephemeral build key signs artifacts).

### 1.4 Orch / multi-package reality

`drift deploy` operates on ONE manifest at a time.  `dep_namespace_map` covers only that manifest's `kind=library` artifacts (`drift_deploy.py:1528-1530`).  Multi-package orchestration (e.g. drift-web rebuilding `web.rest` + `web.jwt` + `web.client` + `net-tls` in one orch run) invokes `drift deploy` once per manifest; cross-manifest siblings are invisible to each other.

`DRIFT_RUN_SNAPSHOT` exists and pins per-package source identity ACROSS the run, but carries no signer-kid map — it can't tell `drift deploy` "this dep was produced THIS run by a different but known kid".

### 1.5 Author-key-in-orch requirement

`drift deploy` requires `--sign-key-file` for every package artifact (`drift_deploy.py:1106-1110, 1492-1496`).  In an orch/CI environment running `DRIFT_CERT_MODE=stage|certify`, the SAME `_resolve_sign_key()` path resolves the seed.  No orch-key vs author-key distinction.  Per the user's framing, **this is a design bug**: the author's private key must never have to enter an orch black box.  Today it does.

## 2. Intended model (per user spec; restated for the record)

The package repository is **untrusted transport**.  A repo URL / folder name adds zero trust.

Two real trust paths:

1.  **Self-verification.** Consumer trusts the author for namespace + source + release intent; rebuilds from author-approved source.
2.  **Certified shortcut.** Consumer trusts the author for namespace + source + release intent AND explicitly trusts a certifier for build/test claims; consumer skips rebuild because they trust the certifier's evidence.

Two distinct signed claims:

```
Author claim (long-lived identity):
  package_id, version, namespaces, source_content_id,
  declared deps/ranges, target class, release intent

Certifier claim (per-build evidence):
  package_id, version, artifact sha256, source_content_id,
  exact dep graph identities, toolchain identity, target,
  certification suite identity/result, run id / timestamp,
  evidence/provenance digest
```

Consumer acceptance policy:

```
accept iff
  author claim is valid AND author kid is trusted for the namespace
  AND ( artifact is directly author-signed
     OR trusted certifier attests artifact + source + build + test
     OR consumer self-verifies by rebuilding )
```

The author's signing key must NOT be required to be present in an orch environment.  The orch holds the certifier key; the author holds the author key; they are different keys with different trust scopes.

## 3. Gaps — current reality vs intended model

| # | Gap | Surface | Severity |
| - | --- | ------- | -------- |
| G1 | No role distinction in `.sig` / trust store.  One kid set per namespace; verifier cannot enforce "trust kid K only for namespace ownership claims, not for artifact signing". | `trust_v0.py:38-79`; `signature_v0.py:118-194` | **Fundamental.** Blocks the entire two-claim model. |
| G2 | `.source-attestation` reuses the artifact-signing key.  Lock schema has the separation, emit path doesn't.  The author's identity key and the per-build artifact key cannot be separated today. | `drift_deploy.py:1170-1210`; `lockfile.py:15,49` | **Fundamental.** Blocks the orch-as-certifier story; forces author key into orch. |
| G3 | No certifier claim artifact exists.  Provenance is unsigned-and-bound; run snapshot is unsigned-and-run-local.  No object that says "I (certifier kid C) attest that artifact A was built from source S with toolchain T and passed suite Q". | new artifact needed | **Fundamental.** Without this, "certified shortcut" path can't be expressed. |
| G4 | Consumer policy is single-kid intersection, not author+certifier composition.  Cannot enforce "needs author kid OR (author kid for source AND certifier kid for build)". | `signature_v0.py:425-443`; `provider_v0.py:659-720` | **Fundamental.** Enforcement of the intended policy doesn't exist. |
| G5 | `drift deploy` is single-manifest; orch's multi-package run is invisible to it.  Cross-manifest, same-run, different-signer deps fall into "external", which requires baseline coverage that hasn't been promoted yet. | `drift_deploy.py:1528-1530, 569-613`; `staged_trust.py:243-264` | **Pragmatic.** Manifests today as the drift-web/net-tls smoke failure. |
| G6 | `drift verify` enforces `entry.kid == profile.kid`; `drift deploy` does not.  Authority alignment is checked only by an offline inspector, not at emit. | `lang/drift/cli.py:325-341`; `drift_deploy.py:1106-1163` | **Hygiene.** Catches some sloppiness; doesn't block the model gaps. |
| G7 | `.author-profile` declares namespaces, but the verifier never reconciles `.sig` kid back to the profile's namespace list — the namespace-author claim is implicit (via trust-store namespace-to-kid map), not signed by the author. | `author_profile.py`; `provider_v0.py:659-720` | **Pragmatic.** Today, "did the author SAY this package is in their namespace" is a trust-store side-channel, not an author-signed claim. |

## 4. Minimal path to bring Drift on par

The full model is a large change.  A minimum viable progression:

### Step 1 — Codify roles in the trust store (G1, G4)

Extend `TrustedKey` and `allowed_kids_by_namespace` with a role tag.  Concretely:

```python
# trust_v0.py
TrustedKey = { kid, algo, pubkey, role: "author" | "certifier" }
allowed_kids_by_namespace = {
    "<ns>": { "authors": [kid, ...], "certifiers": [kid, ...] }
}
```

Verifier sets two enforcement modes:
- **author-signed direct:** sig kid ∈ allowed_authors[ns].
- **certifier shortcut:** sig kid ∈ allowed_certifiers[ns] AND the package payload carries a verified `author-claim.sig` whose kid ∈ allowed_authors[ns].

Backward compat: an untagged trust entry maps to "author" role.  Existing stores keep working unchanged.

Outcome: G1 closed; G4 enforceable in a follow-up.

### Step 2 — Split signing keys at the deploy emit path (G2)

`drift deploy` already accepts a single `--sign-key-file`.  Add `--source-attestation-key` (or read it from `.author-profile`'s configured `source_attestation_key` slot) so `.source-attestation` and `.sig` can be signed by different seeds.  The artifact-signing key becomes "the kid that signs THIS build" (could be a short-lived orch key); the source-attestation key remains the author's identity key.

Outcome: the author's identity key only has to sign the `.author-profile` and `.source-attestation` — not the artifact.  Author key out of the orch black box for normal build flows.

### Step 3 — Introduce a certifier-claim artifact (G3)

New sidecar `.certification` (or `.cert-claim`), shape roughly:

```json
{
  "format": "drift-cert-claim",
  "version": 0,
  "package_id": "...",
  "version": "...",
  "artifact_sha256": "sha256:...",
  "source_content_id": "sha256:...",   // ties to .source-attestation body
  "dep_graph": [ { pkg, version, sha256, author_key }, ... ],
  "toolchain": { driftc_version, abi, commit },
  "target": "...",
  "cert_suite": { id, result_hash },
  "run_id": "...",
  "evidence_sha256": "sha256:..."       // hash of run-snapshot + suite logs bundle
}
```

Signed by the certifier kid.  Verified independently of `.sig`: a consumer in "certifier shortcut" mode requires (a) `.source-attestation` author-signed, (b) `.certification` certifier-signed, both binding the same `source_content_id` and `artifact_sha256`.

Outcome: G3 closed; consumer-side composition policy from Step 1 becomes meaningful.

### Step 4 — Orch run manifest for in-flight cross-manifest deps (G5)

Extend `DRIFT_RUN_SNAPSHOT` (or add a parallel `DRIFT_RUN_MANIFEST`) to carry per-package signer-kid, not just source identity.  Add a third class to `_classify_deps_for_trust_overlay`:

```python
co_deployed_namespaces:         # signed by THIS deploy signer this session (manifest siblings)
external_dependency_namespaces: # already-published, baseline trust authoritative
fresh_in_run_namespaces:        # NEW: produced this orch run by a known different kid;
                                # staged overlay grants THAT kid for the namespace, scoped to this run
```

`build_staged_trust` accepts the third list and emits one overlay entry per fresh-in-run namespace authorizing only the run-manifest's recorded signer for that namespace (NOT the current deploy signer).

Outcome: G5 closed; orch can sequence cross-manifest builds without promoting each intermediate to baseline trust BEFORE smoke.  This is what unblocks the drift-web/net-tls case cleanly.

### Step 5 — Author-claim signing in `.author-profile` (G7) and deploy-side kid reconciliation (G6)

Have `.author-profile` carry a signed author claim per package release: `{package_id, version, source_content_id, namespaces}` signed by author kid.  Have `drift deploy` enforce `entry.kid ∈ profile.authorized_signers` at emit (not just `drift verify` after-the-fact).

Outcome: G6/G7 closed; "did the author release this version into their namespace" becomes an explicit signed claim rather than a trust-store side-channel.

## 5. The drift-web / net-tls failure — classify

The user described: orch runs `drift deploy` on drift-web's manifest; manifest depends on `net-tls`; `net-tls` was JUST built in the same orch run, signed by Foundation (different kid from drift-web's deploy signer); baseline trust hasn't been promoted yet to include this version of `net-tls`.  Current behavior: `build_staged_trust` raises "external dep has no baseline coverage" (post-redesign) OR silently over-authorizes drift-web's kid for `net.tls.*` (pre-redesign).

**Classification:** this is a **G5-flavored gap**.  It is NOT a regression of the PushCoin/MariaDB fix — PushCoin's `singular` deploy used `mariadb-rpc` as a TRULY external, already-published-in-baseline dep, and the policy is correct to refuse adding pushcoin_kid to mariadb namespaces.  drift-web/net-tls differs structurally: net-tls was produced in the SAME run by a known kid (Foundation), it's just not in baseline yet.

**Two options to unblock drift-web specifically:**

- **Local compat patch (short-term):** add a CLI flag / env knob to `drift deploy` accepting `--trust-fresh-in-run=<pkg>=<kid-or-keyfile>` (or read from `DRIFT_RUN_SNAPSHOT` if it grows a signer-kid map per package).  Pass through to `build_staged_trust` as the new `fresh_in_run_namespaces` parameter.  Implements Step 4 minimally; no role-tagging or new claim artifacts yet.  Estimated diff: ~150 LOC plus tests.

- **Skip-baseline-coverage flag (worse):** an opt-out that suppresses the external-coverage check.  Restores pre-redesign permissiveness, weakens the PushCoin/MariaDB protection.  **Not recommended** — violates the no-workaround policy.

Recommend the local compat patch.  It's a forward-compatible carve-out of the eventual G5 fix, doesn't relax the PushCoin guarantee, and gives orch a clean knob to express what it already knows.

**Larger-model changes (Steps 1–3, 5) are NOT required to unblock drift-web.**  They are required for the intended model to actually exist.  Sequence them after the local compat patch.

## 6. Recommendation

1.  Land the **local compat patch** for the drift-web/net-tls case (Step 4 minimal, as a deploy-tool change; no compiler/runtime/abi impact).
2.  Sequence the trust-model work in this order, separate slices:
    - Step 1: role-tagged trust store (compiler-side; back-compat with untagged).
    - Step 2: split source-attestation key at deploy emit.
    - Step 3: certifier-claim artifact + verifier composition.
    - Step 5: author-claim signing in `.author-profile` + deploy-side reconciliation.
3.  Each slice produces its own audit doc + design doc + tests.  Do not bundle.

Stop-the-line clause (echo of feedback_compiler_bugs.md): when a deploy or trust path fails for a user, prefer documenting the gap over patching the symptom.  The two patches landed today (shadowing fix, deploy-signer leakage) were correct insofar as they removed broken behavior — but each was a marker that the underlying model was unbuilt.  Do not let the next failure trigger a third patch without revisiting this doc.

## Appendix — audit citation index

| Question | Verdict | Primary citations |
| -------- | ------- | ----------------- |
| Q1 `.sig` semantics | role-agnostic, single-key | `signature_v0.py:44-74, 118-194, 308, 398-402`; `drift_deploy.py:693-714, 1170-1174` |
| Q2 `.source-attestation` | author body, key reuse | `source_attestation.py:319-466`; `drift_deploy.py:1179-1210`; `resolver.py:155-260` |
| Q3 `.provenance.zst` | unsigned, integrity-bound | `provenance.py:90-186`; `signature_v0.py:311-333` |
| Q4 run snapshot | UNSIGNED, run-local | `run_snapshot.py:1-309`; `resolver.py:805-815` |
| Q5 consumer trust | flat per-namespace allow-list | `trust_v0.py:38-79`; `signature_v0.py:425-443` |
| Q6 authority mixing | three roles, one key | `drift_deploy.py:1170-1210`; `lang/drift/cli.py:325-341`; `trust_v0.py:38-79` |
| Q7 in-flight orch dep | two-class API insufficient | `drift_deploy.py:569-613, 1528-1530`; `staged_trust.py:243-264` |
