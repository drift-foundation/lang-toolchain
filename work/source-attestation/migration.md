# 0.30.0 source-attestation migration

Guidance for downstream teams consuming Drift after the 0.30.0
toolchain bump.  Operational only — full design rationale lives in
`docs/history.md` under the 2026-04-20 entry.

## What changed

`drift/lock.json` v3 → v4.  Each non-co-artifact dep entry now
carries two signed identities:

| Half               | Fields                                                     | Who signs it             |
|--------------------|------------------------------------------------------------|--------------------------|
| Artifact identity  | `version`, `sha256`, `author_key`                          | the package author       |
| Source identity    | `source_content_id`, `source_attestation_key`              | the package author       |

Each published package now also ships a `<package>.source-attestation`
sidecar — a signed JSON envelope binding `(package_id, version,
source_content_id, required_deps, target_class)` to the author's key.
The sidecar lives next to the existing `.zdmp` / `.sig` /
`.provenance.zst` / `.author-profile`.

The split lets default consumption stay byte-strict (the lock's
`sha256` must equal the on-disk `.dmp`'s sha) while a new
`drift build --source-rebuild` / `drift deploy --source-rebuild`
mode tolerates `sha256` drift as long as the source identity
re-verifies — for source-from-commit certification workflows
(orchestrator rebuilds upstreams from source).

## Clean break — no compatibility shim

- **v3 locks rejected at load.**  `drift build` / `drift deploy` on
  0.30.0 will refuse a v3 lock with a `republish-required` diagnostic.
  Run `drift prepare` to regenerate as v4.
- **Pre-0.30 packages have no `.source-attestation` sidecar.**  In
  default-strict mode, consumption fails with "no valid source
  attestation on disk."  Republish the package with toolchain ≥ 0.30.0
  to get the sidecar.  No silent fallback to byte-only verification —
  that would defeat the trust boundary the v4 split exists to draw.
- **`drift prepare` will not write a v4 lock with empty source
  identity.**  Resolved non-co-artifact deps without a valid sidecar
  trigger a fail-fast `PrepareError` listing every offending package.
  Republish the offending upstream(s), then re-prepare.

## Unsigned dev opt-in (preserved)

Packages built and consumed under `--allow-unsigned-from` still work.
`author_key == "unsigned"` propagates: source identity fields are
allowed empty (signing infra governs both halves), and strict-mode
verification skips the artifact-signer + source-attestation checks.
**`sha256` is still enforced** — the unsigned escape hatch covers
signature/attestation only, not byte identity.  Source-rebuild mode
hard-rejects unsigned packages: there's no signed source identity to
anchor trust to.

## Migration steps (downstream teams)

Run these in order.

1. **Update to the 0.30.0 toolchain.**

2. **Republish every signed package you produce.**  A 0.30.0 build:
   - stamps `source_content_id` into the `.dmp` manifest,
   - emits the `<package>.source-attestation` sidecar signed with
     your existing `--sign-key-file`.

   No new key required; the same Ed25519 seed signs both the
   `.dmp` and the source attestation.  (The lock records
   `source_attestation_key` separately so the two roles can diverge
   later — long-lived org identity key signs source, ephemeral build
   key signs artifacts — but you don't need to make that split today.)

3. **Re-run `drift prepare`** in every project that consumes packages.
   v3 locks are rejected at load; the regenerated v4 lock will
   reference the new sidecar values for each dep.

4. **Build / deploy as usual.**  Default `drift build` and `drift
   deploy` are byte-strict on the artifact half AND source-strict on
   the source half — no flag changes needed.

5. **For source-from-commit certification (orchestrator workflows):**
   add `--source-rebuild` to the `drift build` / `drift deploy`
   invocation.  The verifier then tolerates `.dmp` byte drift on
   upstreams as long as their `.source-attestation` sidecars
   re-verify against the lock.  Per-package byte-drift is reported
   to stdout as run evidence.

## What you do NOT need to change

- **Authored manifests** (`drift/manifest.json`).  The v2 manifest
  format is unchanged; `package_deps` ranges are still the only
  authored constraint.  `source_content_id` is computed from the
  declared inputs by the toolchain — no manifest field to edit.
- **Trust store / signing key file format.**  Same Ed25519 seed
  file, same base64-text encoding (`lang/drift/crypto.b64_decode`,
  `validate=True`).

## `.dmp` manifest change (what IS required)

0.30.0 signed packages emit a **required** `source_content_id`
stamp into the `.dmp` manifest — not optional.  The stamp is the
only way the artifact itself declares the source identity it was
built from; a `.source-attestation` sidecar alone is **not**
enough and cannot retroactively upgrade an older package into
source-mode by adjacency (the Phase B.2 trust rule).  The
canonical walk:

1. `drift deploy` computes `source_content_id` from stable source
   inputs.
2. driftc stamps the value into the `.dmp` manifest via
   `--source-content-id`.
3. The `.source-attestation` sidecar is signed over the same
   value; the resolver cross-binds body ↔ stamp at index time.

Older packages without the stamp cannot participate in v4 /
source-attestation trust: they must be **republished** with
toolchain ≥ 0.30.0.  No sidecar-only path.

## Failure modes you may see

| Diagnostic                                                                   | Meaning                                                              | Fix                                                            |
|------------------------------------------------------------------------------|----------------------------------------------------------------------|----------------------------------------------------------------|
| `drift/lock.json uses schema v3 (0.29.x); v4 is required as of 0.30.0`       | Lock was written by an older toolchain                               | `drift prepare`                                                |
| `cannot write v4 lock — these resolved dependencies have no valid source attestation` | An upstream you depend on hasn't been republished on ≥ 0.30.0  | Republish the named package; re-prepare                        |
| `has no valid source attestation on disk (sidecar missing, unbound, or signature failed)` | The `.source-attestation` sidecar is gone, mismatched, or broken     | Reinstall the package; check stderr warnings for the cause     |
| `source-rebuild mode requires a signed source attestation as the trust root` | Used `--source-rebuild` against a package without a sidecar          | Republish; or drop the flag for byte-only mode                 |
| `is marked author_key: "unsigned", but source-rebuild mode requires…`        | Used `--source-rebuild` against an unsigned dev package              | Sign + republish; source-mode is incompatible with `unsigned`  |

## Where the trust root lives

The `source_attestation_key` recorded in the lock is the only thing
trusted in source-rebuild mode.  The rebuilt `.dmp`'s own `.sig`
signer is irrelevant — the rebuilder cannot sign as the package
owner, and source-mode pins this explicitly.  An orchestrator
substituting their own attestation key for the original author's
shows up as `source_attestation_key changed` and is rejected.
