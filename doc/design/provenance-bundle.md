# Provenance Bundle

> **Historical document — pre-v1 trust model.**  The `.sig` /
> `.author-profile` envelope described here was replaced by the
> trust-v1 author-claim / cert-claim sidecar pair.  The current,
> authoritative trust contract is
> [`doc/design/trust-v1.md`](trust-v1.md); the v0 envelope shape
> below is retained only as a historical reference and is no
> longer emitted, verified, or accepted by the toolchain.

## Overview

The provenance bundle is a zstd-compressed JSON document emitted alongside every
Drift build artifact (package or app). It records the exact build environment,
compiler identity, resolved dependency graph, and (when available) dependency
provenance and signing keys. For signed packages, the provenance digest is bound
into the v2 signing envelope, making any post-build tampering detectable.

## Published Artifact Layout

### Packages

```
<dest>/<name>/<version>/
  <name>.zdmp                   # compressed package
  <name>.sig                    # signature sidecar (v2 envelope)
  <name>.author-profile         # publisher identity (top-level, NOT inside bundle)
  <name>.provenance.zst         # zstd-compressed provenance bundle
```

All four files form the authenticated artifact set. The `.sig` binds the
package bytes, author profile, and provenance into a single signed envelope.

### Apps

```
<app-dest>/<name>/<version>/
  <name>                        # compiled binary
  <name>.sig                    # signature sidecar (v2 envelope, when signing key available)
  <name>.provenance.zst         # zstd-compressed provenance bundle (authenticated via .sig)
```

Both packages and apps use the same v2 signing envelope and verification model.
Apps do not have an `.author-profile` file (author profiles are package-only).
When a signing key is provided, the `.sig` binds the app binary digest and
provenance bundle digest into a signed envelope.

## Bundle Format

The `.provenance.zst` file is a zstd-compressed JSON document (the "bundle
manifest") with this schema:

```json
{
  "format": "drift-provenance-bundle",
  "version": 0,
  "provenance": { ... main provenance document ... },
  "dep_provenance": {
    "web-jwt": { ... dep's provenance document ... }
  },
  "dep_keys": {
    "ed25519:abc123...": {
      "algo": "ed25519",
      "kid": "ed25519:abc123...",
      "pubkey": "<base64_32bytes>"
    }
  }
}
```

### Compression Settings

- Algorithm: zstd
- Level: 3
- Threads: 0 (single-threaded, deterministic output)
- Content size written into frame header

These settings match the `.zdmp` compression pipeline for consistency.

### Determinism

The uncompressed content is serialized with `json.dumps(sort_keys=True,
separators=(",",":"))` -- compact, no whitespace, keys sorted. Combined with
single-threaded zstd compression, this guarantees that identical inputs produce
identical compressed bytes.

## Main Provenance Schema

The provenance document inside the bundle uses schema version 4:

```json
{
  "schema_version": 4,
  "artifact_name": "web-rest",
  "artifact_version": "0.2.5",
  "artifact_kind": "package",
  "artifact_sha256": "sha256:<hex>",
  "source_content_id": "sha256:<hex>",
  "target": "drift-dev",
  "compiler_version": "0.27.92",
  "compiler_commit": "abc1234",
  "abi": 6,
  "build_utc": "2026-03-20T12:00:00Z",
  "resolved_deps": {
    "web-jwt": {
      "version": "0.2.5",
      "integrity": "sha256:<hex>"
    }
  },
  "source": {
    "vcs_type": "git",
    "branch": "main",
    "commit": "a1b2c3d4e5f6..."
  }
}
```

### Fields

| Field | Type | Description |
|---|---|---|
| `schema_version` | int | Always `4` for the current schema (v4 added the required `source_content_id` leg). |
| `artifact_name` | string | Package or app name. |
| `artifact_version` | string | Semver version string. |
| `artifact_kind` | string | `"package"` or `"app"` (canonical — never `library`). |
| `artifact_sha256` | string | `"sha256:<hex>"` -- digest of the primary artifact bytes (uncompressed `.dmp` for packages, compiled binary for apps). |
| `source_content_id` | string | `"sha256:<hex>"` -- **v4 required.** The provenance leg of the three-way SCI equality (author == cert == provenance); cross-checked against both signed claims at verify time. |
| `target` | string | Target triple used for the build. |
| `compiler_version` | string | Drift compiler version. |
| `compiler_commit` | string | Git commit of the compiler (or `"unknown"`). |
| `abi` | int | Compiler ABI level. |
| `build_utc` | string | ISO 8601 UTC timestamp of the build. |
| `resolved_deps` | object | Map of dependency package IDs to `{version, integrity}`. |
| `source` | object (optional) | Source identity: `{vcs_type, branch, commit}`. Present when VCS is detected at build time. This is the exact source identity for audit — the lock file pins compatibility range, provenance pins exact source. |

## Bundle Contents

### `dep_provenance`

A map from dependency package name to the dependency's provenance document. This
is collected at build time from the staged package root. If a dependency does not
have a provenance file (e.g., legacy packages), it is omitted from the map.

### `dep_keys`

A map from key ID (kid) to public key metadata for dependency signers. This
enables offline verification of the dependency chain without needing access to
a trust store. Keys are extracted from dependency `.sig` sidecars at build time.

## Why Author Profile Stays Top-Level

The author profile is NOT embedded inside the provenance bundle because:

1. It is a **publisher identity** document, not build provenance.
2. It needs to be directly readable by tooling that inspects published packages
   without decompressing the bundle.
3. It has its own lifecycle (created once, reused across packages).
4. The `.sig` envelope already binds it via `author_profile_sha256`.

## Authentication Model

### Package provenance: authenticated

For packages, the provenance bundle is authenticated through the v2 signing
envelope. The signing pipeline:

1. Build the package (`.dmp` bytes).
2. Build the main provenance document (includes `artifact_sha256` of the `.dmp`).
3. Collect dependency provenance and public keys from staged package root.
4. Build the bundle JSON, compress with zstd.
5. Write to `<artifact>.provenance.zst`.
6. Compute `sha256(compressed_bytes)` -- the digest covers the on-disk form.
7. Build the v2 envelope:
   ```
   drift-sig-envelope-v2
   package-sha256:<hex>
   author-profile-sha256:<hex>
   provenance-sha256:<hex>
   ```
8. Sign the envelope with Ed25519.
9. Write `.sig` sidecar containing `provenance_sha256` field.

At verification time:

1. Load the `.sig` sidecar.
2. Verify `package_sha256` matches the actual package bytes.
3. If envelope version >= 2 and `provenance_sha256` is present:
   - Load the actual `<artifact>.provenance.zst` from disk.
   - Compute sha256 of the compressed bytes.
   - Compare against the signed `provenance_sha256` from `.sig`.
   - Reject on mismatch ("provenance sidecar integrity check failed").
4. Reconstruct the signed envelope and verify the Ed25519 signature.
5. Check trust policy for module namespaces.

### App provenance: authenticated

Apps receive the same signing treatment as packages when a signing key is
available. The signing pipeline:

1. Build the app binary.
2. Build the main provenance document (includes `artifact_sha256` of the binary).
3. Collect dependency provenance and public keys from staged package root.
4. Build the bundle JSON, compress with zstd.
5. Write to `<artifact>.provenance.zst`.
6. Compute `sha256(compressed_bytes)`.
7. Build the v2 envelope:
   ```
   drift-sig-envelope-v2
   package-sha256:<app_binary_hex>
   provenance-sha256:<provenance_bundle_hex>
   ```
   (No `author-profile-sha256` line -- author profiles are package-only.)
8. Sign the envelope with Ed25519.
9. Write `.sig` sidecar containing `provenance_sha256` field.

If no signing key is available, the provenance bundle is still emitted for
auditing purposes but is not cryptographically authenticated.

## Verification Contract

The verifier treats signed packages as an artifact set: `.zdmp` + `.sig` +
`.provenance.zst` + `.author-profile`. Tampering with any individual file
invalidates the signature. The verification order is:

1. Package sha256 check (`.sig` vs actual bytes).
2. Provenance integrity check (`.sig` provenance digest vs actual `.provenance.zst` on disk).
3. Author profile integrity check (`.sig` author-profile digest vs actual `.author-profile` on disk).
4. Envelope signature verification (Ed25519).
5. Namespace trust policy enforcement.

If the signed envelope says provenance is present, the `.provenance.zst` file
must exist on disk and its sha256 must match the signed digest.

If the signed envelope says an author profile is present, the `.author-profile`
file must exist on disk and its sha256 must match the signed digest.

Missing or modified files are rejected with a clear diagnostic.

Backward compatibility is preserved: v0 envelopes (raw bytes) and v1 envelopes
(package + profile) continue to verify without provenance checks. The verifier
falls back from `.provenance.zst` to `.provenance.json` when the new format is
not found.

## Offline Verification Objective

The provenance bundle embeds dependency public keys (`dep_keys`) to support
future offline verification of the dependency chain. A verifier with the bundle
and the dependency `.zdmp` + `.sig` files can verify signatures without network
access or a centralized trust store. This is not yet enforced but the data is
available for tooling to consume.

## Orchestration / Certification Use Case

The provenance bundle enables external tools to:

- Audit the exact compiler version and ABI used to build an artifact.
- Verify the complete resolved dependency graph at build time.
- Confirm the artifact digest matches what was built.
- Inspect dependency provenance for transitive supply chain auditing.
- Extract dependency signer keys for offline verification.
- Reproduce or validate builds against known-good environments.
- Generate SBOMs (Software Bill of Materials) from resolved dependency data.
