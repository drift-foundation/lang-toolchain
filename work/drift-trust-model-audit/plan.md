# Drift trust model — implementation plan

Date: 2026-05-18
Status: design + implementation plan, pre-implementation.
Companion: `work/drift-trust-model-audit/audit.md` (current reality
walkthrough).
Driver: "Pause the local compat patch. Fix the trust model correctly."

## 0. Direction and policy (binding)

- Package repos are **untrusted transport**.  A repo URL or folder
  name carries zero trust.
- **Author key never enters orch.**  Orch holds a certifier key,
  not an author key.  Author identity stays with author.
- **Author trust and certifier trust are separate roles.**  A
  consumer's trust store names which kids it trusts for each role
  per namespace.  A kid is intrinsically one role or the other
  for a given namespace; no role-agnostic fallback.
- **Consumer acceptance is a composition.**  A trusted author
  claim for the namespace/source identity is ALWAYS required.
  Artifact acceptance is then EITHER (a) a trusted certifier /
  distributor claim binding the artifact bytes, OR (b) consumer
  self-verify by rebuilding from source matching the author's
  SCI.  Author-as-distributor is not a separate path: an author
  who wants to distribute directly signs a cert claim with their
  own key, and the consumer trusts that same kid in both the
  `authors` and `certifiers` role lists for the relevant
  namespace.
- **Pre-v1 acceptance is a hard product boundary, not a
  migration preference.**  Drift is pre-release.  There are no
  outside users holding pre-v1 trust files, packages, or sidecars
  whose smooth migration we owe anything to.  Accepting pre-v1
  formats today would create permanent format ambiguity for zero
  user benefit — it would be a self-inflicted technical debt that
  no real consumer needs.  Therefore:

  - Every format header carries a `version` field so future schema
    changes can dispatch unambiguously (this is the v2-and-beyond
    affordance).
  - Today the toolchain accepts EXACTLY `version: 1` for each
    format.  Anything else — including any pre-v1 in-repo
    fixture, a hand-edited `0`, or a legacy `.sig` /
    `.source-attestation` blob — is rejected loudly.
  - There is NO v0 loader.  NO untagged-trust fallback.  NO
    role-agnostic kid set.  NO legacy `.sig` /
    `.source-attestation` acceptance path.  NO compatibility shim
    for current in-repo artifacts during the bootstrap window.
  - Future-version compatibility is designed when that future
    version exists.  Carrying a `version` field today does NOT
    imply we will ever retroactively accept pre-v1 formats.

  In-repo fixtures and orch packages that break are regenerated
  into v1, not preserved.

The drift-web/net-tls failure is evidence of the current model
being incomplete.  It is NOT a regression to fix with a staging
exception.  It is unblocked by orch emitting proper certifier
claims (per this plan), not by widening `staged_trust.py`.

## 1. One canonical format set

Format identifiers and versions, all at v1, all fresh.  The
`version` field exists in every header so the verifier can later
dispatch on schema rev; today it accepts ONLY `version: 1` for
each.  Receiving anything else (including `0`) is a hard reject
with a clear "unsupported format version; expected v1" diagnostic.

| Format | Tag string | Schema version | Purpose |
| ------ | ---------- | -------------- | ------- |
| Trust store | `drift-trust` | 1 | Consumer-side policy: who is trusted for what role per namespace |
| Author claim sidecar | `drift-author-claim` | 1 | Author-signed: source identity + package release intent.  Never binds artifact bytes (O6). |
| Certifier claim sidecar | `drift-cert-claim` | 1 | Certifier-signed: artifact bytes + build identity + cert suite result + run evidence |
| Author profile | `drift-author-profile` | 1 | Author's global identity document; namespaces the author claims; signing kid |
| Provenance bundle | `drift-provenance-bundle` | 1 | Build-environment record; bound by `.cert-claim.body.evidence_sha256` |
| Run manifest (orch) | `drift-run-manifest` | 1 | Orch-side: pins per-package signer kids across a multi-manifest orch run.  Internal to orch; NOT trusted by consumers. |
| Package payload | `drift-pkg` / `.dmp` / `.zdmp` | unchanged | Compiled module bundle |

Everything previously named `.sig`, `.source-attestation`, current
`drift-trust` v0, etc., is **removed** outright.  No fallback
loaders.  No back-compat branches.

## 2. Trust store (`drift-trust` v1)

```json
{
  "format": "drift-trust",
  "version": 1,
  "keys": {
    "ed25519:<kid>": {
      "algo": "ed25519",
      "pubkey": "<base64-32B>",
      "label": "PushCoin author key"
    }
  },
  "namespaces": {
    "singular.*": {
      "authors":    ["ed25519:pushcoin_author"],
      "certifiers": ["ed25519:pushcoin_orch_certifier"]
    },
    "mariadb.rpc.*": {
      "authors":    ["ed25519:foundation_author"],
      "certifiers": ["ed25519:foundation_orch_certifier"]
    }
  },
  "revoked": []
}
```

Rules:

- `authors` and `certifiers` are **disjoint** policy sets.  A kid
  MAY appear in both, but only because they explicitly play both
  roles in the consumer's policy.  A single key playing both
  roles in the wild (e.g. author shortcut for local dev) is fine;
  the trust file states the policy explicitly per role.
- Namespace matching is unchanged: longest-prefix-wins.  Inside a
  matched entry, the consumer reads BOTH `authors` and
  `certifiers` lists (no longest-prefix-wins per role across
  different entries; the matched entry's role lists are
  authoritative).
- An entry without one of the role lists means **that role has
  no trust for that namespace**.  E.g. an `authors`-only entry
  means no certifier shortcut is allowed for that namespace;
  consumers must self-verify by rebuilding from source matching
  the author claim's SCI.  (Per O6, there is no direct-author
  artifact acceptance path; an author who wants to distribute
  directly must additionally be trusted in the `certifiers` list
  and sign a cert claim.)
- `revoked` is a flat list of kids; revocation overrides any
  namespace allow.

Verifier helper:

```python
TrustStore.allowed_authors_for_module(module_id) -> set[Kid]
TrustStore.allowed_certifiers_for_module(module_id) -> set[Kid]
```

Replaces `allowed_kids_for_module`.  The old, role-agnostic helper
is removed.

### 2.1 Role-based, not organization-based (O2)

The model is role-based.  Foundation is just one actor that may
hold keys for both roles.  Stdlib (`std.*`, `lang.*`, `drift.*`)
gets the SAME treatment as any other namespace in `core_trust.json`:
role-tagged `authors` and `certifiers` entries.

No "author-only because it is Foundation" shortcut.  Every
consumed artifact has an author role AND a certifier/distributor
role.  For stdlib, Foundation may occupy both roles, but they
are still represented separately in policy and in claims:

```json
{
  "format": "drift-trust",
  "version": 1,
  "keys": {
    "ed25519:foundation_author":   { "algo": "ed25519", "pubkey": "...", "label": "Drift Foundation author" },
    "ed25519:foundation_certifier":{ "algo": "ed25519", "pubkey": "...", "label": "Drift Foundation cert/distrib" }
  },
  "namespaces": {
    "std.*":   { "authors": ["ed25519:foundation_author"], "certifiers": ["ed25519:foundation_certifier"] },
    "lang.*":  { "authors": ["ed25519:foundation_author"], "certifiers": ["ed25519:foundation_certifier"] },
    "drift.*": { "authors": ["ed25519:foundation_author"], "certifiers": ["ed25519:foundation_certifier"] }
  }
}
```

In a tighter pre-release reality where Foundation hasn't yet
separated its keys, the SAME kid can appear in both lists for
the same namespace — but it MUST be listed in both lists.  The
verifier composition then runs uniformly: one role check for
author claim, a separate role check for cert claim.  No
"Foundation special case" gates anywhere in the verifier code.

The bootstrap stdlib re-emit (slice 6) produces both an
`.author-claim` and a `.cert-claim` for every stdlib package,
even if the same key signs both.  This keeps the bootstrap
trust shape identical to every other consumer's shape.

## 3. Author claim (`drift-author-claim` v1)

The author claim binds the author's identity to a package
**release** — source identity, declared deps, target class.
**Author claims never bind artifact bytes** (O6, sign-off
2026-05-18).  Artifact hashes are always bound by certifier /
distributor claims.

### 3.1 Role invariant (O6)

> Author role answers "who authorized this source release?"
> Distributor / certifier role answers "who vouches for this
> concrete artifact?"  The same actor may hold both roles, but
> the claims stay separate.

If an author wants to distribute a binary directly (the "simple
local / dev" path), they ALSO sign a `.cert-claim` over the
artifact with their own key.  The author's kid is then trusted
in BOTH role lists for that namespace in the consumer's trust
store.  There is no special "author-direct" verifier path; that
case is just the certifier-shortcut path with the same kid
playing both roles.

### 3.2 Body schema

```json
{
  "format": "drift-author-claim",
  "version": 1,
  "body": {
    "schema_version": 1,
    "package_id": "singular",
    "version": "0.3.0",
    "namespaces": ["singular", "singular.*"],
    "source_content_id": "sha256:<hex>",
    "required_deps": [
      { "name": "mariadb-rpc", "version_range": "^0.5.0" }
    ],
    "target_class": "release",
    "release_utc": "2026-05-18T12:00:00Z"
  },
  "signatures": [
    { "algo": "ed25519", "kid": "ed25519:author", "sig": "<base64>" }
  ]
}
```

Note absence of any `artifact_*` field.  By construction the
author claim is artifact-agnostic.

### 3.3 Signing rule

- The author signs `canonical_json_bytes(body)`.
- `signatures` may contain multiple entries (multi-author
  releases supported).
- At consumer verify time, AT LEAST ONE signature must verify
  against a kid in `trust.allowed_authors_for_module(M)` for
  every module M the package claims to own.  ("Any one within
  this claim's array" — see O5.)

### 3.4 Source-content-id

Hash of canonical-ordered: `(kind, module_namespace, entry_module,
sorted modules[(path, sha256)], sorted package_deps[(name,
range)], sorted native_deps, unsafe, sorted assets[(path,
sha256)], target_class)`.  Same definition as the current
`source_content_id` in `source_attestation.py` — kept verbatim
because it already excludes anything that legitimately varies
across rebuilds.

### 3.5 Distribution

`.author-claim` sidecar published by author alongside (or before)
the `.dmp`.  Single canonical name: `<pkg>.author-claim` (single
file per package release — author identity claims do not multiply
the way certifier claims do).

## 4. Certifier claim (`drift-cert-claim` v1)

The certifier claim binds the orch/certifier identity to a
specific BUILD of a package: the artifact bytes, the toolchain
used, the dep graph, the certification suite result, and the
evidence trail.

### 4.1 Core invariants (O3, O4)

> A certifier claim is only meaningful if changing any package in
> the resolved graph changes what the certifier signed.  Otherwise
> the certifier did not really certify the artifact the consumer
> is using.

> "Certified" is not a single bit.  The certifier claim must say
> what suite passed, and consumers / CI must be able to require
> that suite.  Otherwise a weak smoke-test signature and a full
> release-gate signature are indistinguishable to the verifier.

Concretely:

- **dep_graph covers the full resolved transitive closure** (O3),
  not just direct deps.  Every package in the consumer's resolved
  graph at load time must appear as a `dep_graph` entry of the
  cert claim.  If a transitive dep is missing or its hash
  differs, the verifier rejects.
- **`cert_suite.id` is verifier-addressable** (O4).  The
  consumer may pin which suite the cert was produced by via
  `drift verify --require-cert-suite <id>`.  v1 has no global
  registry of suite ids; ids are free-form namespaced strings
  (recommended convention: `<authority>/<suite-name>`, e.g.
  `drift.foundation/default` or `pushcoin/internal-stage`).

### 4.2 Body schema

```json
{
  "format": "drift-cert-claim",
  "version": 1,
  "body": {
    "schema_version": 1,
    "package_id": "singular",
    "version": "0.3.0",
    "artifact_sha256": "sha256:<hex>",
    "source_content_id": "sha256:<hex>",
    "target": "drift-linux-x86_64",
    "toolchain": {
      "driftc_version": "0.31.108",
      "drift_rt_abi": 14,
      "driftc_commit": "<sha>"
    },
    "dep_graph": [
      {
        "package_id": "mariadb-rpc",
        "version": "0.5.0",
        "artifact_sha256": "sha256:<hex>",
        "source_content_id": "sha256:<hex>",
        "author_kid": "ed25519:foundation_author",
        "cert_kid": "ed25519:foundation_orch_certifier",
        "dep_kind": "direct"
      },
      {
        "package_id": "mariadb-wire",
        "version": "0.4.2",
        "artifact_sha256": "sha256:<hex>",
        "source_content_id": "sha256:<hex>",
        "author_kid": "ed25519:foundation_author",
        "cert_kid": "ed25519:foundation_orch_certifier",
        "dep_kind": "transitive"
      }
    ],
    "cert_suite": {
      "id": "drift.foundation/default",
      "version": "1.0.0",
      "result": "pass",
      "result_evidence_sha256": "sha256:<hex>"
    },
    "run_id": "<uuid>",
    "run_started_utc": "2026-05-18T12:00:00Z",
    "evidence_sha256": "sha256:<hex>"
  },
  "signatures": [
    { "algo": "ed25519", "kid": "ed25519:certifier", "sig": "<base64>" }
  ]
}
```

### 4.3 Signing rule

- Certifier signs `canonical_json_bytes(body)`.
- One signature per certifier kid; `signatures` may carry
  multiple entries only for the multi-key (key rotation,
  multi-region orch) case under a single certifier identity.
  Multiple INDEPENDENT certifiers (e.g. Foundation orch +
  customer orch) emit SEPARATE sidecar files — see §4.6.

### 4.4 Binding requirements

- `artifact_sha256` MUST match the on-disk `.dmp` decompressed
  bytes.
- `source_content_id` MUST match the corresponding
  `.author-claim`'s `body.source_content_id` for the same
  package_id + version (consumer enforces during verify;
  comparison only, never recompute — see guardrail G1 in §5.5).
- `dep_graph` MUST be the full resolved transitive closure
  observed at build/cert time.  Every package the consumer's
  driftc resolves at load time MUST have a matching entry
  (by `package_id`, `version`, `artifact_sha256`).  Missing or
  mismatched entries fail verification.
- `dep_kind` is `"direct"` or `"transitive"` — informational
  for diagnostics; the verifier does not treat them
  differently for trust decisions.
- Each dep's `author_kid` and `cert_kid` must be the kids that
  signed THAT dep's author claim and cert claim respectively
  (or `null` if a dep has no cert claim, e.g. when the consumer
  is self-verifying that dep).  The certifier commits to the
  exact upstream identity set.

### 4.5 Cert suite (O4)

- `cert_suite.id` is a free-form namespaced identifier
  (recommended `<authority>/<suite>`).
- `cert_suite.result` is `"pass"` | `"fail"`.  A `.cert-claim`
  with `result: "fail"` is well-formed but rejected by default;
  consumer can opt in to viewing fail-claims for audit (a future
  flag, out of scope for v1).
- Verifier supports `--require-cert-suite <id>` to enforce a
  specific suite for CI / release-gate lanes.  Paired with
  `--require-certifier <kid>` to lock down both axes.

### 4.6 Provenance binding

`evidence_sha256` is the hash of the run-evidence bundle (current
`.provenance.zst` payload + cert suite logs).  Bundle remains
unsigned-but-bound.  Any consumer wanting to audit the cert
result inspects the evidence bundle out-of-band; the verifier
only checks the hash binding.

### 4.7 Distribution (O1)

`.cert-claim` is a per-certifier sidecar.  When multiple
certifiers attest the same package release, multiple sidecars
coexist alongside the `.dmp`.

Canonical filename (decided per O1):

```
<pkg>.cert-claim.<kid>.json
```

`<kid>` is the FULL certifier kid (e.g.
`ed25519:Ql7U5KNrMOohuQ4hxxLGSeghTCm36vaAidEDnG5VOEw=`).  Full
kid avoids the collision risk of a short prefix.  The colon and
equals signs in the kid are safe across all target filesystems
the toolchain supports; if a host FS rejects them, the loader
URL-encodes them and the trust check still operates on the
canonical kid form embedded in the file body.

Rationale: per-file (not per-directory) keeps the filesystem
shape flat and grep-friendly; full kid (not short) eliminates
collision risk entirely.

## 5. Consumer verifier — composition policy

Per O6, there is **no author-direct artifact acceptance path**.
Author claims bind source/release intent only.  Artifact bytes
are bound exclusively by certifier claims.  Two acceptance paths
remain (down from three in the earlier draft):

1.  Certifier-shortcut: trusted author claim + trusted certifier
    claim, bound by `source_content_id`.
2.  Self-verify: trusted author claim + consumer rebuilds from
    local source matching the author's SCI.

If an author wants to "directly distribute" a binary, they take
on the certifier/distributor role TOO and sign a cert claim with
their own key.  The consumer trusts the same kid in both role
lists.  Mechanically, this is just the certifier-shortcut path
with one kid in both roles — no special-case code in the verifier.

### 5.1 Verifier pseudocode

When `driftc` loads a foreign package for module M:

```
verify(package P, module M, trust T,
       *, require_certifier=None, require_cert_suite=None,
       self_verify=False, self_verify_sci=None) =

  # --- AUTHOR CLAIM — always required ----------------------------
  AC = read_author_claim(P)            # one canonical file per release
  require AC.body.package_id, version are consistent with P
  require M is covered by AC.body.namespaces
  require AC.body.source_content_id == sci_stamped_in_dmp_manifest(P)
        # G1: COMPARE the stamps; do NOT recompute SCI from binary.
        # The .dmp's manifest carries source_content_id (stamped at
        # build time by the certifier).  The author signed that
        # SCI in AC.body.  We assert the two stamps agree.  We
        # never derive SCI from the .dmp's bytes — that would be a
        # phantom proof.
  require ∃ sig ∈ AC.signatures with
            sig.kid ∈ T.allowed_authors_for_module(M)
            AND ed25519_verify(sig, kid, canonical(AC.body)) passes
            AND kid not in T.revoked
        # O5: "any one" within the array suffices

  artifact_sha = sha256(P.dmp_decompressed)

  # --- ACCEPTANCE PATH (exactly one) ------------------------------

  IF self_verify:
      # Self-verify mode is the ONLY path that recomputes SCI
      # from source.
      require self_verify_sci is not None
      require self_verify_sci == AC.body.source_content_id
      ACCEPT  # self-verify

  ELSE:
      # Certifier-shortcut path.
      candidate_ccs = read_cert_claims(P)   # one or more sidecar files
      FOR each CC in candidate_ccs:
          require CC.body.package_id, version match P
          require CC.body.artifact_sha256 == artifact_sha
          require CC.body.source_content_id == AC.body.source_content_id
          require ∃ sig ∈ CC.signatures with
                    sig.kid ∈ T.allowed_certifiers_for_module(M)
                    AND ed25519_verify(sig, kid, canonical(CC.body)) passes
                    AND kid not in T.revoked
          require dep_graph_covers_actual_resolved_closure(CC.body.dep_graph, P)
                # O3: every dep the consumer is loading must have a
                # matching dep_graph entry (package_id, version,
                # artifact_sha256, source_content_id all match)
          IF require_certifier is not None:
              require sig.kid == require_certifier
          IF require_cert_suite is not None:
              require CC.body.cert_suite.id == require_cert_suite
          require CC.body.cert_suite.result == "pass"
          # First matching CC wins.
          ACCEPT  # certifier-shortcut

      REJECT with explanation
```

### 5.2 SCI is compared, never recomputed in normal mode (G1)

> Normal consumer verification CANNOT recompute `source_content_id`
> from a binary `.dmp` because source is not present.  The normal
> path COMPARES three stamped SCIs:
>
>   1. the SCI in the `.dmp` package manifest (stamped by the
>      certifier at build),
>   2. the SCI in the trusted `.author-claim` body (signed by the
>      author),
>   3. the SCI in the trusted `.cert-claim` body (signed by the
>      certifier).
>
> All three must be equal.  The verifier MUST NOT recompute SCI
> from the `.dmp` bytes or imply that SCI was independently
> derived.  Only self-verify mode recomputes SCI from local
> source.

Diagnostic must say which mode it ran in.  Conflating the two
would let the verifier appear to prove source identity when it
only compared signed metadata.

### 5.3 dep_graph closure check (O3)

When driftc loads a package P with cert claim CC, it has already
resolved P's dep closure (via lock or fresh resolution).  The
verifier walks every dep currently being loaded and asserts:

- The dep's `package_id` and `version` appear as an entry in
  `CC.body.dep_graph`.
- The dep's `artifact_sha256` (computed from the dep's `.dmp`)
  matches the dep_graph entry's `artifact_sha256`.
- The dep's `source_content_id` (recovered from the dep's
  package manifest, NOT recomputed) matches the dep_graph
  entry's `source_content_id`.

Missing entries or mismatches reject loudly.  This is what makes
the cert claim meaningful: changing any package in the resolved
graph changes what the certifier signed.

### 5.4 Failure modes (must be distinguishable in the diagnostic)

- Missing `.author-claim`.
- `.author-claim` not signed by any trusted author kid for M.
- `source_content_id` mismatch between `.author-claim` and
  the stamped SCI in the `.dmp` manifest.
- No `.cert-claim` found and not in self-verify mode.
- `.cert-claim` present but `source_content_id` mismatches
  `.author-claim`.
- `.cert-claim` present but signed by an untrusted certifier
  kid for M.
- `.cert-claim` present but `artifact_sha256` mismatches the
  on-disk `.dmp`.
- `.cert-claim` `dep_graph` missing an entry for a dep the
  consumer is loading.
- `.cert-claim` `dep_graph` entry mismatches the dep's actual
  artifact hash or stamped SCI.
- `.cert-claim` `cert_suite.result` is not `"pass"`.
- `--require-certifier <kid>` mismatch.
- `--require-cert-suite <id>` mismatch.
- Self-verify SCI mismatch with author claim.
- Revoked kid (author or certifier).

### 5.5 Guardrail summary (referenced from G1, G2, G3)

> **G1.** Normal verification compares stamped SCIs; only
> self-verify mode recomputes from source.  Diagnostic must say
> which mode ran.
>
> **G2.** No dual-format window on main.  Slices 1–6 land as one
> coordinated PR series; the merge IS the cutover.  Never accept
> both pre-v1 and v1 at the same time.
>
> **G3.** Artifact hashes are accepted only from certifier /
> distributor claims.  Author claims never bind artifacts.  If an
> author distributes directly, they emit a cert claim with their
> own kid trusted in both roles.

### 5.6 What's removed

- `signature_v0.verify_package_signatures` — replaced by
  author+cert verifier.
- `.sig` sidecar — no longer emitted, no longer parsed.
- The "direct-author" acceptance path — collapsed into the
  certifier-shortcut path via dual-role kids.
- `trust_v0.allowed_kids_for_module` (role-agnostic) — replaced by role-tagged helpers.
- `.source-attestation` — replaced by `.author-claim`.

### 5.3 What stays

- The `.dmp` / `.zdmp` package payload format (unchanged).
- Provenance bundle payload shape (unchanged content; binding mechanism changes to `cert_claim.evidence_sha256`).
- `core_trust.json` for reserved namespaces (`std.*`, `lang.*`, `drift.*`).  Migrates to `drift-trust` v1 shape.

## 6. Author profile (`drift-author-profile` v1)

The author's GLOBAL identity document.  Per O8 (sign-off
2026-05-18):

> Identity is long-lived; release authorization is per-version.
> Keep them in separate artifacts.

The author profile MUST NOT embed per-release claims.  Each
package/version gets its own standalone `.author-claim`; the
profile is identity only.

```json
{
  "format": "drift-author-profile",
  "version": 1,
  "body": {
    "org": "PushCoin",
    "namespaces": ["singular", "singular.*"],
    "author_kid": "ed25519:author"
  },
  "signatures": [
    { "algo": "ed25519", "kid": "ed25519:author", "sig": "<base64>" }
  ]
}
```

Self-signed.  Distributed via the author's site.  Consumer uses it
to bootstrap their trust store (`drift trust apply` adds
`profile.body.author_kid` to `trust.namespaces[ns].authors` for
each declared namespace).  Does NOT add any certifier kid — that
is a separate, explicit consumer decision (`drift trust
trust-certifier`).

## 7. Orch flow

### 7.1 Per-package author publication (offline, author key required)

```
drift author-publish \
  --author-key-file ~/.config/drift/keys/pushcoin.author.seed \
  --manifest singular/drift/manifest.json \
  --target release \
  --out singular-0.3.0.author-claim
```

Emits `.author-claim` ONLY.  Does NOT build the package.  Does
NOT need any compiled artifact (per O6, author claims never bind
artifact bytes).  If the author also wants to distribute the
binary directly, they run `drift cert-publish` with their own
key in a subsequent step — same kid in two roles.

Author key never leaves author's workstation / signing module.

### 7.2 Orch build + certify (orch key required, NO author key)

```
drift cert-publish \
  --cert-key-file /etc/drift/orch.cert.seed \
  --manifest singular/drift/manifest.json \
  --package-root /var/orch/staged \
  --run-id <uuid> \
  --cert-suite drift-foundation-cert-suite:1.0.0 \
  --out singular-0.3.0
```

Orch:
1. Resolves deps using its baseline trust + same-run manifest.
2. Builds the package; computes `artifact_sha256`,
   `source_content_id`.
3. Runs the cert suite; records `result_evidence_sha256` and
   `evidence_sha256` (bundle).
4. Emits `.dmp`, `.cert-claim`, `.provenance.zst`.
5. NO `.author-claim` emitted — author must publish that
   separately, asynchronously.

The author's `.author-claim` and the orch's `.cert-claim` are
combined at the package repository (or by the consumer).  They
are independent publications.

### 7.3 Cross-package orch runs (drift-web shape)

Orch builds multiple packages in one run.  Each emits its own
`.cert-claim` signed by the orch's certifier key.  Dep resolution
for in-flight siblings:

- Orch maintains an internal run-staging area.  When package A
  (e.g. `web.rest`) depends on package B (e.g. `net-tls`) BUILT
  IN THE SAME RUN, A's build resolves B from the staging area.
- The dep is verified via B's freshly-emitted `.cert-claim`.  The
  orch's certifier key is in the orch's own working trust store
  (which it controls).
- The orch run manifest pins (`run_id`, `package_id → cert_kid`)
  for every package produced this run.  This is orch-internal
  state, not a consumer-trusted artifact.

Result: drift-web's `web.rest` build no longer needs a staged
trust overlay that adds drift-web's deploy signer to `net.tls.*`.
It needs the orch's working trust to already authorize the orch's
own certifier kid (which it does, by construction — the orch
controls its own trust store).  Consumers downstream see two
sets of sidecars (`net-tls.author-claim` from Foundation,
`net-tls.cert-claim` from orch); their own trust store decides
which to honor.

### 7.4 What `build_staged_trust` becomes

After this redesign, `build_staged_trust` ceases to exist in its
current shape.  Its replacement is:

- Orch builds with its OWN persistent trust store
  (`/etc/drift/orch.trust.json`) that authorizes the orch's
  certifier kid for all namespaces the orch is allowed to
  certify.  No per-deploy staging trust overlay is needed.
- For in-flight siblings of the same run, the orch ensures the
  cert claims are emitted before consumers attempt verification.
- The current `_classify_deps_for_trust_overlay` /
  `co_deployed_namespaces` / `external_dependency_namespaces`
  taxonomy is unnecessary in the new model — there is only
  "orch is certifying it" (orch trust applies) or "orch is
  consuming an already-certified dep" (consumer's trust applies
  uniformly).

## 8. CLI surface

| Command | Role | Key required | Inputs | Outputs |
| ------- | ---- | ------------ | ------ | ------- |
| `drift author-publish` | author | author seed | manifest | `.author-claim` |
| `drift cert-publish` | certifier / orch | certifier seed | manifest, package-root | `.dmp`, `.cert-claim`, `.provenance.zst` |
| `drift verify` | consumer | none | `.dmp` + `.author-claim` + `.cert-claim`(s) + trust | accept/reject + diagnostics |
| `drift trust apply` | consumer admin | none | `.author-profile` | merged trust entry (authors only) |
| `drift trust trust-certifier` | consumer admin | none | `<ns>` + `<kid-or-keyfile>` | merged trust entry (certifier role) |
| `drift build` | author / orch | none for build itself | manifest, package-root | `.dmp` only (no claims) |

### 8.1 `drift verify` policy flags (O4, O7)

- `--require-certifier <kid>` — REJECT if the certifier
  signature on the matching `.cert-claim` is not by this exact
  kid.  Useful when a release-gate lane needs to prove it used
  a specific certification path.
- `--require-cert-suite <id>` — REJECT if
  `.cert-claim.body.cert_suite.id` does not exactly equal this
  string.  Distinguishes weak smoke-test signatures from full
  release-gate signatures.

Combined, the typical CI invocation looks like:

```
drift verify pkg.dmp \
  --require-certifier ed25519:foundation-ci \
  --require-cert-suite drift.foundation/default
```

Both flags are optional in v1; consumer trust store alone is
sufficient for the basic composition policy.  The flags add
per-invocation tightening.

- `--self-verify` — opt into the self-verify path; requires
  source available locally (consumer rebuilds and the verifier
  compares the recomputed SCI against the author claim).

### 8.2 What's removed

- `drift sign` (replaced by `drift author-publish` / `drift cert-publish`).
- `drift deploy` (replaced by `drift cert-publish`; the orchestration script can sequence multiple `cert-publish` calls).
- Anything emitting `.sig` or `.source-attestation`.

`drift verify` is now the canonical CLI for testing the trust chain locally.

## 9. Breakage inventory

What stops verifying / working immediately:

1. **All existing `.sig` sidecars** in the wild (and in-repo).
   No fallback loader.  Old `.dmp`s without an `.author-claim` +
   trusted author kid fail to load.
2. **All existing `.source-attestation` sidecars.**  Format
   replaced by `.author-claim`; no migration path in the code.
3. **All existing `drift-trust` v0 stores.**  Schema version
   `0` rejected; consumers must migrate to v1.
4. **All existing `.author-profile` files** (currently v0).
   Migrate to v1 schema.
5. **`drift deploy` CLI surface** — removed.  Orch scripts must
   move to `drift cert-publish`.
6. **`drift sign` CLI surface** — removed.
7. **In-repo stdlib packages** (currently signed `.sig`) — must
   be re-emitted with `.author-claim` + `.cert-claim` signed by
   the Drift Foundation's identity keys.  This is a bootstrap
   gate; see §10.6.
8. **All test fixtures** that emit/verify v0 trust files,
   `.sig`, `.source-attestation` — regenerated, not migrated.
9. **`tools/drift_deploy/staged_trust.py`** — deleted.  The
   build-trust overlay machinery is unnecessary in the new model.
10. **`_classify_deps_for_trust_overlay`** — deleted (along with
    its tests).  Replaced by orch-internal trust (the orch's
    working trust authorizes its own certifier kid; no overlay
    needed).
11. **`DRIFT_CERT_MODE` semantics simplify.** `stage`/`certify`
    were proxies for source-rebuild mode; the new model has
    explicit author vs cert paths.  Mode flag can be removed or
    repurposed.

## 10. Implementation slices (atomic, ordered)

Each slice is a separate **commit** with its own audit + tests,
and each commit on its own produces a green test suite — but
slices 1–6 land on a coordinated branch and merge to main as
ONE cutover (G2).  Main never sees a dual-format window.

Concretely:

- **Slices 1–6 share a feature branch** (e.g. `trust-v1`).
  Each slice is a commit on that branch with its own audit
  + green tests.  The branch is reviewed slice-by-slice but
  merged in a single fast-forward or squash that constitutes
  the cutover.
- **Slices 7–8 land separately on main** as ordinary commits
  AFTER slices 1–6 have merged.  These delete the
  staged-trust machinery and rename CLI surfaces; both happen
  in a world where the new format is already canonical.
- **Slice 9 happens in the drift-web repo**, not this one.

Reviewability is preserved (slice-by-slice on the branch);
main-correctness is preserved (no transition window where
two formats coexist).  Do not bundle slices into one commit
on the branch — the slice boundary is what makes the change
reviewable.  Do not land any of slices 1–6 individually on
main — that would create the dual-format window G2 forbids.

### Slice 1 — Trust store v1 (compiler-side)

- New `lang/driftc/packages/trust_v1.py` (rename of v0 file, full
  rewrite).
- Role-tagged `TrustStore` dataclass.
- `allowed_authors_for_module` / `allowed_certifiers_for_module`.
- Delete `trust_v0.py` + `allowed_kids_for_module`.
- Update `core_trust.json` to v1 shape with role-tagged
  `authors` AND `certifiers` entries for the reserved
  namespaces (`std.*`, `lang.*`, `drift.*`).  Per O2 there is
  no Foundation special case: stdlib has the same two-role
  shape as any other namespace.  In the bootstrap window
  Foundation MAY reuse the same kid in both lists, but both
  lists must be present and both claims (`.author-claim` +
  `.cert-claim`) must accompany every stdlib package.  This
  keeps the verifier composition uniform — stdlib goes through
  the same author+certifier check as user packages.
- Tests: pure-Python TrustStore behavior.

### Slice 2 — Author-claim emit + verify

- New `lang/driftc/packages/author_claim_v1.py`: canonicalization
  + sign + verify body.
- New `lang/drift/author_publish.py` (or extend `cli.py`):
  `drift author-publish` command.
- Delete `lang/drift/sign.py` and the entire `.sig` emit path.
- Delete `tools/drift_deploy/source_attestation.py`.
- Tests: round-trip an `.author-claim`; verify against trust
  store; failure modes (untrusted kid, sci mismatch, namespace
  mismatch).

### Slice 3 — Cert-claim emit + verify

- New `lang/driftc/packages/cert_claim_v1.py`.
- New `tools/drift_deploy/cert_publish.py` (or rename
  `drift_deploy.py` → `cert_publish.py`): `drift cert-publish`
  command.
- The orch flow.  Cert claim produced; no author key required.
- Tests: round-trip; verify against trust store; failure modes
  (artifact_sha mismatch, sci mismatch, untrusted certifier
  kid, dep graph mismatch).

### Slice 4 — Verifier composition

- Rewrite `lang/driftc/packages/provider_v0.py` (rename to
  `provider_v1.py`) `load_package_v0_with_policy` →
  `load_package_v1_with_policy`.
- Implement the composition policy from §5.
- Delete `signature_v0.py::verify_package_signatures`.
- Tests: both acceptance paths (certifier-shortcut + same-kid-in-
  both-roles variant; self-verify); each failure path with
  distinguishable diagnostics including the G1 mode-of-operation
  label.

### Slice 5 — Provenance binding via cert claim

- `provenance.zst` payload unchanged; binding moves from
  `.sig.body.provenance_sha256` to
  `cert_claim.body.evidence_sha256`.
- Update `tools/drift_deploy/provenance.py` emit; update
  verifier to re-hash and check against `evidence_sha256`.

### Slice 6 — Stdlib + in-repo fixture regeneration

- Re-emit every in-repo signed package with the new model:
  `.author-claim` from Foundation author key (bootstrap), no
  `.cert-claim` initially (stdlib is published not certified;
  Foundation may emit one later via its own orch).
- Regenerate all test fixtures touching `.sig`,
  `.source-attestation`, v0 trust stores.
- Bootstrap key handling: the Drift Foundation's author key seed
  for stdlib lives at a documented path (e.g.
  `dev-bootstrap/foundation.author.seed`) used ONLY during
  pre-release; clearly marked as a bootstrap fixture, NOT a
  production key.

### Slice 7 — Remove deploy-tool staged-trust machinery

- Delete `tools/drift_deploy/staged_trust.py` outright.
- Delete `_classify_deps_for_trust_overlay`.
- Delete the `--baseline-trust` plumbing for the per-deploy
  overlay (orch's working trust replaces it).
- Tests: delete the corresponding test classes
  (`TestStagedTrust`, `TestModuleNamespace::test_external_dep_*`,
  `TestClassifyDepsForTrustOverlay`).

### Slice 8 — CLI rename + doc rewrite

- `drift deploy` → `drift cert-publish`.
- `drift sign` removed.
- New `drift author-publish`.
- `drift trust apply` writes role-tagged entries.
- New `drift trust trust-certifier <ns> <kid-or-keyfile>` for
  adding certifier kids.
- Rewrite `docs/toolchain-build-workflow.md`,
  `docs/trust-and-signing.md` (if exists; create if not) to
  the one canonical model.

### Slice 9 — drift-web orch script unblock

- After slices 1–8 land, the drift-web orch (and any other
  multi-package orch) updates its scripts to use
  `drift cert-publish` with the orch's own working trust store
  authorizing the orch's certifier kid.
- net-tls is built first; its `.cert-claim` is emitted into the
  orch's staging.
- web.rest / web.jwt / web.client are built next; their builds
  consume net-tls's cert claim through the orch's trust.  No
  per-build trust overlay is generated.
- Foundation publishes the matching `.author-claim` for net-tls
  through a separate, offline process.

This slice produces no compiler code; it's the migration of
drift-web (and any other consumer of the old machinery) onto the
new flow.

## 11. Design decisions (signed off 2026-05-18)

| # | Decision | Invariant |
| - | -------- | --------- |
| O1 | Per-certifier sidecar files.  Canonical name: `<pkg>.cert-claim.<kid>.json`.  Full kid; no short-prefix collision risk. | One file per (package release, certifier kid). |
| O2 | `core_trust.json` for `std.*` / `lang.*` / `drift.*` carries role-tagged `authors` and `certifiers` entries.  Foundation is just one actor; same shape as every other namespace.  No "author-only because it is Foundation" shortcut. | The model is role-based, not organization-based.  Verification always composes a trusted author claim AND a trusted certifier claim (unless self-verify). |
| O3 | `cert_claim.body.dep_graph` covers the full resolved transitive closure.  Direct-only is not acceptable. | A certifier claim is only meaningful if changing any package in the resolved graph changes what the certifier signed. |
| O4 | `cert_suite.id` is verifier-policy-addressable in v1.  `drift verify --require-cert-suite <id>` enforces it.  No global registry; ids are free-form namespaced strings.  Per-namespace required-suite policy in the trust store is a v2 feature. | "Certified" is not a single bit.  The certifier claim must say what suite passed; consumers / CI must be able to require that suite. |
| O5 | "Any one valid trusted signature for the role/namespace" applies WITHIN each claim's signature array.  The composition still requires BOTH an author claim AND a certifier claim (unless self-verify or direct-author-as-cert).  Quorum policy is v2. | Composition is per role; intra-claim sig array is permissive. |
| O6 | `author_claim.body.artifact_sha256` REMOVED.  Author claims bind source/release intent only.  Artifact hashes are bound exclusively by certifier/distributor claims.  If the author wants to distribute directly, they sign a cert claim with their own key. | Author role = "who authorized this source release?"  Distributor/certifier role = "who vouches for this concrete artifact?"  Same actor may hold both roles; the claims stay separate. |
| O7 | `drift verify --require-certifier <kid>` and `--require-cert-suite <id>` are v1 flags.  Combined invocation pins both axes for CI / release-gate lanes. | Verification path narrowing is per-invocation, not global state. |
| O8 | Author claims are standalone per-release files (`.author-claim`).  `.author-profile` remains global identity only (org, namespace ownership, author kid).  Profile MUST NOT embed release claims. | Identity is long-lived; release authorization is per-version.  Keep them in separate artifacts. |

### 11.1 Guardrails (pre-implementation)

Three hard constraints derived from the sign-off discussion:

**G1.** Normal consumer verification cannot recompute
`source_content_id` from a binary `.dmp` (source is not present).
The normal path COMPARES three stamped SCIs: author claim's,
package-stamped (in the `.dmp` manifest), and certifier claim's.
Only self-verify mode recomputes SCI from local source.  The
verifier diagnostic must clearly state which mode it ran in.
Otherwise the verifier could imply it independently proved
source identity when it only compared signed metadata.

**G2.** No dual-format window on main.  If slices are separate
commits, they either build unused v1 pieces first and cut over
atomically, or the coordinated PR series lands as one cutover.
Do not add v0/v1 compatibility loaders as an implementation
convenience.

**G3.** Artifact hashes are only accepted from certifier /
distributor claims.  Author claims never bind artifacts.  If
the author distributes directly, they sign a certifier claim
too, possibly with the same key trusted in both roles.

All three are referenced from §5 (verifier) and §10 (slices).

## 12. Test plan

Each slice ships its own tests.  Cross-cutting acceptance test
matrix for the composition policy (verifier rewrite, slice 4):

| # | Setup | Expected |
| - | ----- | -------- |
| T1 | `.author-claim` from trusted author + `.cert-claim` from trusted certifier; sci matches across both stamps and the `.dmp` manifest; artifact hash matches; full transitive dep_graph covers the actual closure; `cert_suite.result == "pass"` | ACCEPT (certifier-shortcut) |
| T2 | Direct-author-as-distributor: same kid in `T.allowed_authors_for_module(M)` AND `T.allowed_certifiers_for_module(M)`; that kid signs BOTH the `.author-claim` AND the `.cert-claim`; bindings consistent | ACCEPT (the kid plays both roles) |
| T3 | `.author-claim` sci != `.cert-claim` sci | REJECT (sci mismatch between claims) |
| T4 | `.author-claim` sci != stamped sci in `.dmp` manifest | REJECT (sci mismatch with package stamp) |
| T5 | `.author-claim` only; no `.cert-claim`; not self-verify mode | REJECT (no acceptance path) |
| T6 | `.author-claim` from UNTRUSTED author kid | REJECT (untrusted author) |
| T7 | `.author-claim` from trusted author for namespace A, package claims module in namespace B | REJECT (namespace mismatch) |
| T8 | `.cert-claim`'s `artifact_sha256` mismatches the on-disk `.dmp` | REJECT (artifact mismatch) |
| T9 | `.cert-claim` from UNTRUSTED certifier kid | REJECT (untrusted certifier) |
| T10 | Author kid revoked | REJECT (revoked) |
| T11 | Certifier kid revoked | REJECT (revoked) |
| T12 | Multiple `.cert-claim` sidecars present; one signed by trusted certifier, one by untrusted; trusted one fully valid | ACCEPT (any trusted matching CC suffices) |
| T13 | Self-verify mode: consumer rebuilds, recomputed sci matches `.author-claim.body.source_content_id`; no `.cert-claim` consulted | ACCEPT (self-verify) |
| T14 | Self-verify mode: consumer rebuilds, recomputed sci differs from `.author-claim` | REJECT (rebuilt source differs from author's release) |
| T15 | `--require-certifier ed25519:foo` set; matching `.cert-claim` is signed by `ed25519:bar` (still trusted) | REJECT (required-certifier mismatch) |
| T16 | `--require-cert-suite drift.foundation/default` set; matching `.cert-claim.body.cert_suite.id == "pushcoin/internal-stage"` | REJECT (required-suite mismatch) |
| T17 | dep_graph closure: cert claim's `dep_graph` omits one transitive dep the consumer is actually loading | REJECT (dep_graph missing entry) |
| T18 | dep_graph closure: cert claim's `dep_graph` lists the right dep_pkg/version but `artifact_sha256` mismatches the dep's `.dmp` | REJECT (dep_graph artifact mismatch) |
| T19 | dep_graph closure: cert claim's `dep_graph` has the right entry; consumer loads the exact same dep set | ACCEPT |
| T20 | `cert_suite.result == "fail"` | REJECT (failing cert claim) |
| T21 | drift-web shape regression: orch builds `net-tls` and `web.rest` in one orch run; `net-tls` has author-claim from Foundation + cert-claim from orch; `web.rest`'s cert-claim references net-tls in dep_graph with matching artifact_sha and sci; both verify under consumer trust that authorizes Foundation as author of `net.tls.*` and orch as certifier of both namespaces | ACCEPT (drift-web orch flow works without any staged-trust overlay) |
| T22 | PushCoin shape regression: pushcoin author claim for `singular.*` only; consumer trusts `pushcoin_author` for `singular.*` AND `foundation_author` / `foundation_certifier` for `mariadb.*`; loading singular pulls in `mariadb.rpc.managed`; both packages have author+cert claims; all verify independently | ACCEPT |
| T23 | PushCoin negative: `pushcoin_author` presented as author for `mariadb.rpc.managed` (i.e. an attempt to author-spoof) | REJECT (pushcoin not in `T.allowed_authors_for_module("mariadb.rpc.managed")`) |
| T24 | PushCoin negative variant: `pushcoin_certifier` presented as certifier for `mariadb.rpc.managed` | REJECT (pushcoin not in `T.allowed_certifiers_for_module("mariadb.rpc.managed")`) |
| T25 | G1 guardrail: verifier diagnostic for an ACCEPT case in normal mode explicitly states "compared stamped SCIs" (not "verified source identity") | PASS (diagnostic shape pin) |
| T26 | G1 guardrail: verifier diagnostic for an ACCEPT case in self-verify mode explicitly states "recomputed and matched source identity" | PASS (diagnostic shape pin) |
| T27 | Format version: `.author-claim` header version is 0 (or any non-1) | REJECT with "unsupported format version; expected v1" |
| T28 | Format version: `.cert-claim` header version is 0 | REJECT with "unsupported format version; expected v1" |
| T29 | Format version: trust store `version: 0` | REJECT at load (no v0 loader exists) |

Plus per-slice unit tests as called out above.

## 13. Migration timeline

Internal-only, pre-release.  No external coordination required
beyond the drift-foundation toolchain team and the consuming
projects (drift-web, mariadb-rpc, singular).

No temporary dual-format loaders.  No bootstrap shim that accepts
both old and new sidecars during a transition window.  The
stdlib regeneration and the verifier rewrite ship together so
"main is green" with the new format the moment the series merges.

Cadence:

1.  **Coordinated PR series, slices 1–6, single mergeable unit.**
    Every commit's tests pass on its own; the SERIES is the
    cutover.  Stdlib `.author-claim` fixtures regenerate in
    slice 6 alongside the verifier rewrite in slice 4 — they
    land together.  Before the series merges, the toolchain
    runs the old format only; after, the new format only.
2.  **Cleanup PR, slices 7–8.**  Delete the deploy-tool staged-
    trust machinery and rename CLI commands.  Doc rewrite.
3.  **Drift-web orch migration, slice 9.**  Happens in the
    drift-web repo after slices 1–8 are tagged in the toolchain
    release.

At no point does the toolchain accept BOTH formats simultaneously.
The merge of slice 1–6 IS the cutover.

## 14. Out of scope (explicitly)

- Cert-suite-id registration / governance (`O4`).
- Trust quorum policy (`O5`).
- Hardware-backed signing key support (HSM integration).
- Cross-publisher cert claim composition (e.g. customer's own
  cert + foundation cert both required for a package).  Future
  via O4.
- DNS-anchored author identity (e.g. `pushcoin.com` proving the
  author kid).  Future; orthogonal.
- Source-rebuild semantics changes; only the trust binding around
  source-rebuild changes (sci now comes from `.author-claim`
  instead of `.source-attestation`).

## 15. Recommendation

Adopt this plan.  Settle O1–O8 first (single review pass).  Then
implement slices 1–6 as one coordinated PR series.  Slices 7–8
follow as cleanup.  Slice 9 happens in drift-web after.

Estimated total compiler+deploy-tool LOC change: ~3500–5000
lines added/replaced (a substantial rewrite of the trust path),
balanced against a comparable amount deleted (`signature_v0.py`,
`trust_v0.py`, `sign.py`, `staged_trust.py`,
`source_attestation.py`).  Net diff is probably small in
absolute terms; the change is structural.

Test impact: ~1000–2000 lines of new tests; ~500–1500 lines of
deleted tests (the staged-trust suite goes away entirely).

The drift-web/net-tls case becomes a normal cert-publish flow.
The PushCoin/singular case becomes a normal author-publish from
PushCoin + cert-publish by whichever certifier PushCoin (or
their orch) uses.  Neither needs special-case code paths in the
deploy tool.

This is the proper fix.  No more staging exceptions.
