# Trust Model v1

This document is the durable contract for Drift's package trust model.
It supersedes the pre-v1 `.sig` / `.source-attestation` envelope shape
described in `docs/design/provenance-bundle.md`, which is retained only
as a historical reference.

The trust-v1 cutover is a **hard product boundary**: there is no
silent fall-back to a pre-v1 trust path, no untagged-trust shape, and
no role-agnostic kid lookup.  A package missing v1 sidecars cannot be
loaded by a v1-aware consumer (except via the explicit dev-time
`allow_unverified_roots` policy, which is itself gated against the
reserved namespaces).

---

## 0. TL;DR for the casual reader

You're about to compile your code against `net-tls 0.3.0`.  The
`.dmp` file on your disk could have come from anywhere — your
laptop's cache, a corporate mirror, a colleague's USB stick.  How
do you know it's actually `net-tls 0.3.0` as published, and not
something a hostile mirror handed you instead?

Drift's answer is two signed sidecars sitting next to every `.dmp`:

- **`<pkg>.author-claim`** — signed by the *author*, says
  "I, the maintainer of `net-tls`, released version 0.3.0 with
  this exact source identity and these declared deps."
- **`<pkg>.cert-claim.<kid>.json`** — signed by a *certifier*
  (typically a build/CI/release engineer, not the author), says
  "I built this exact `.dmp` from that exact source identity, ran
  this test suite against it, observed the resolved dep graph,
  and got these results."

Before your compiler links anything in, it cracks open both
sidecars, checks the signatures against your local
`drift/trust.json`, and refuses to proceed unless **both**:

1. an author kid you trust says this is `net-tls 0.3.0`, AND
2. a certifier kid you trust says these are the bytes for that
   release, with a passing cert suite and the resolved dep graph
   intact.

The two roles are kept on separate machines with separate keys.
A break-in on the build host gives the attacker the *certifier*
key — not the author key.  An author who's been social-engineered
into signing a malicious release still needs a certifier signature
the consumer trusts.  The split is the whole point.

The rest of this document spells out the file shapes, the exact
verification steps, what the model proves and doesn't prove, and
which attacks the test suite pins down.

---

## 1. Goals and non-goals

### Goals

- **Untrusted distribution channels.**  Package repositories and the
  bytes that move across them carry no authority.  A package only
  proves what it is after a v1-aware consumer verifies the sidecars
  attached to its `.dmp` against the consumer's trust store.

- **Author key isolation.**  An author's private key never enters the
  certifier / orch / deploy infrastructure.  The `tools/drift_author/`
  CLI is structurally separated from `tools/drift_deploy/`; the
  import-boundary check in
  `lang/tests/driver/test_import_boundaries.py` makes that boundary
  enforceable in code review.

- **Roles are distinct.**  The model has two roles — `authors` and
  `certifiers` — and a kid trusted in one role does not implicitly
  receive the other.  The trust store is role-tagged; the verifier
  consults each role independently.

- **Source identity vs artifact identity.**  Author claims
  authenticate *who released what source under what name* (package id,
  version, namespaces, source content id, declared deps); they do
  **not** bind compiled artifact bytes.  Cert claims authenticate the
  artifact bytes plus the toolchain, run, cert suite, and the full
  resolved dependency graph at certification time.

- **Cheap normal-path verification.**  A regular consumer does not
  walk the source tree and does not recompute the source content id
  (SCI).  It compares pre-stamped values, verifies signatures, and
  trusts the certifier shortcut.  SCI recomputation is reserved for
  the explicit self-verify / source-rebuild mode.

### Non-goals

- **Transport security.**  v1 does not specify how `.dmp` files reach
  the consumer's disk.  Mirrors, CDNs, and offline media are all
  acceptable; the verifier ignores provenance metadata that lacks a
  cryptographic anchor.

- **PKI / certificate chains.**  v1 trust is a flat allowlist of
  Ed25519 kids, keyed by namespace pattern with role tags.  There is
  no notion of "ed25519 cert authority"; trust is configured
  out-of-band (toolchain ships a core trust store; users layer a
  project trust store on top).

- **Confidentiality.**  Author and cert claims are public metadata.
  Nothing in v1 encrypts package contents.

---

## 2. Threat model and the three-principal split

### 2.1 Who's attacking what

The consumer is compiling source against a package they did not
build themselves.  The threat model assumes:

- **The distribution path is hostile.**  Anything between the
  publisher and the consumer's disk — package repositories,
  mirrors, CDNs, internal artifact stores, file transfer scripts,
  even the local filesystem cache — can substitute, mutate, drop,
  or reorder bytes.  v1 verification must produce the same
  accept/reject decision whether the package came from a trusted
  mirror or was dropped onto disk by a malicious local script.
- **Sidecars travel with the artifact.**  An attacker can rewrite
  any sidecar file as freely as they can rewrite the `.dmp`.  The
  only inputs the consumer treats as authoritative are the kids
  and pubkeys already in its own trust store and the build-time
  flags it received from the user.
- **Some signer keys are stolen.**  A compromised author kid lets
  the attacker publish new packages under that author's name; a
  compromised certifier kid lets the attacker emit new cert claims
  for whatever artifacts they choose.  v1 limits the blast radius
  of either compromise through role separation and per-namespace
  granting.
- **The toolchain itself is trusted.**  v1 does not defend against
  a malicious `driftc`; users are responsible for installing the
  toolchain through a channel they trust.  The toolchain ships
  with `core_trust_v1.json`; that file is part of the toolchain's
  trusted state.

### 2.2 What v1 verification is trying to prove

For every dep the consumer loads, the verifier wants to answer one
question with a cryptographic anchor:

> *Are these bytes, sitting at this `(package_id, version)`, what a
> trusted author released and what a trusted certifier ran their
> suite against, with both signing over identities the consumer
> recognises by name and role?*

Either half of that question failing — wrong package id, wrong
version, wrong source identity, wrong signer, wrong role,
wrong namespace, revoked kid, dep-graph drift — rejects the load.
There is no graceful degradation.

### 2.3 Three principals, three private keys

v1 has three principals with distinct private-key custody:

| Principal     | Holds private key for     | Produces                                            | Trust the consumer needs                                          |
| ------------- | ------------------------- | --------------------------------------------------- | ----------------------------------------------------------------- |
| **Author**    | author kid (Ed25519)      | `<pkg>.author-claim` (signs over source identity)   | author kid is granted the `authors` role for the module namespace |
| **Certifier** | certifier kid (Ed25519)   | `<pkg>.cert-claim.<kid>.json` (signs over artifact) | certifier kid is granted the `certifiers` role for the module namespace |
| **Consumer**  | none (verifier only)      | nothing on disk                                     | both of the above, in the consumer's `drift/trust.json`           |

The split matters because the three principals have different
threat profiles:

- The **author's** machine has to handle source code, but never
  needs to be online during a release: an author can sign offline.
  Author key compromise compromises *what packages can be released
  under that author's name*.
- The **certifier's** machine has to run a build pipeline,
  download external artifacts, execute test suites, and often sits
  inside CI/CD with broader system reach.  Certifier key
  compromise compromises *which artifacts get attested* — but the
  attacker still needs an author signature on the *source* claim
  to publish under the author's name.
- The **consumer** verifies; it has no signing key.

Compromising one role does not silently buy the other.  The
asymmetry matters:

- A **stolen author kid** lets the attacker forge author claims —
  meaning they can mint new releases at any `(package_id,
  version, SCI)` triple in the namespaces the kid is trusted for.
  Each one still has to be paired with a cert claim from a kid
  the consumer trusts as **certifier**, but if the same
  organization controls both roles (a common production pattern),
  the author key is the gating one.
- A **stolen certifier kid** lets the attacker attest arbitrary
  `.dmp` bytes against any existing author claim's source
  identity, in the namespaces the kid is trusted for.  Normal
  consumers do not recompute SCI; they accept the cert claim's
  attestation that "these bytes were built from that source."
  This is the explicit cost of the certifier shortcut and is
  examined in Scenario D below.  Mitigations are trust scoping,
  revocation, and the self-verify escape hatch — not elimination.

Neither compromise silently buys the *other* role, because the
verifier consults the trust store independently per role.  This
is what "author key never enters the certifier infrastructure"
buys: a certifier-host compromise (the more likely incident,
given certifier hosts run external code) does not escalate into
the ability to mint new releases.

### 2.4 How the split is enforced structurally

The role split is enforced in three load-bearing places:

1. **Module layout.**  The author-side CLI lives in
   `tools/drift_author/`; the certifier/deploy code lives in
   `tools/drift_deploy/`.  Author seeds are loaded only by
   `tools.drift_author.key_loader`.  An automated check in
   [`lang/tests/driver/test_import_boundaries.py`](../../lang/tests/driver/test_import_boundaries.py)
   fails the build if any deploy / certifier module imports
   `tools.drift_author.key_loader` (or any sibling under
   `tools.drift_author`).  The boundary is part of the test
   matrix, not a convention.
2. **Trust store shape.**  Every namespace entry has separate
   `authors` and `certifiers` kid lists.  Granting one role does
   not grant the other.  The v1 loader rejects pre-v1 flat-list
   namespace entries on read.
3. **Verifier role-routing.**  The author-claim step queries
   `trust.allowed_authors_for_module(<module_id>)` and the
   cert-claim step queries `trust.allowed_certifiers_for_module
   (<module_id>)`.  A kid present in only one list satisfies only
   the matching step; the test matrix pins this with
   `test_attack6_*` (see §6).

### 2.5 A worked example: Alice publishes, Bob's CI consumes

Concrete trace of one release passing through all three principals.

**Alice (author)** maintains `acme.crypto` and is about to release
version `0.4.0`.  On her laptop she runs:

```text
drift-author publish                   \
    --sidecar-dir build/               \
    --package-id acme.crypto           \
    --version 0.4.0                    \
    --namespace acme.crypto.*          \
    --source-content-id sha256:71f3…   \
    --required-dep std=^0              \
    --target-class library             \
    --release-utc 2026-05-19T00:00:00Z \
    --key-file ~/.config/drift/keys/alice.seed
```

Her private key never leaves her laptop.  The resulting
`acme.crypto.author-claim` says (in essence): *"Alice's kid
attests that source content `71f3…` is `acme.crypto 0.4.0`, owns
the namespace `acme.crypto.*`, and requires `std=^0`."*

**Carlos (certifier)** runs the release pipeline on a CI host.
He receives Alice's `.dmp` + author-claim, builds the artifact,
runs the `drift-deploy/release-2026` cert suite against it, and
emits a cert claim:

```text
drift-deploy cert emit                       \
    --pkg build/acme.crypto.dmp              \
    --author-claim build/acme.crypto.author-claim \
    --target drift-dev                       \
    --cert-suite drift-deploy/release-2026   \
    --key-file /var/lib/drift-deploy/keys/release-2026.seed
```

The pipeline produces `acme.crypto.cert-claim.<carlos-kid>.json`.
Carlos's CI host only ever holds Carlos's *certifier* private key;
it never sees Alice's author seed.

**Bob (consumer)** has these entries in his `drift/trust.json`:

```json
{
  "namespaces": {
    "acme.crypto.*": {
      "authors":    ["ed25519:alice…"],
      "certifiers": ["ed25519:carlos…"]
    }
  }
}
```

Bob's CI runs `driftc … --dep acme.crypto@0.4.0 …`.  Before any
bytecode from `acme.crypto` gets linked, the compiler walks
through the runtime-enforcement checks in §4.  Both sidecars
verify, both kids are in the right role for `acme.crypto.*`,
neither is revoked, the cert claim's dep graph matches Bob's
resolve, the artifact sha256 matches the file on disk — the load
succeeds.  Total cryptographic cost: two Ed25519 verifies and a
handful of equality checks.

### 2.6 Attack scenarios — what each defense actually catches

Below are five plausible incidents and how v1 verification ends
each one.  Every entry maps to a load-bearing test in
`lang/tests/packages/test_v1_adversarial.py`; see §6 for the full
matrix.

#### Scenario A: Hostile mirror swaps the .dmp

An attacker on the network between Bob's CI and his package
mirror substitutes `acme.crypto.dmp` with a backdoored version
they built.  They do not have access to Alice's or Carlos's
private keys.

The author-claim and cert-claim on the mirror are still Alice's
and Carlos's originals — the attacker can swap them too, but they
cannot re-sign them.  Bob's compiler:

- Computes sha256 of the on-disk `.dmp` → it does NOT equal the
  `artifact_sha256` in Carlos's cert-claim.
- Rejects the load with `"artifact_sha256 mismatch"`.

Caught by `test_attack1_repo_substitution_rejected`.

#### Scenario B: Replay an old vulnerable release

The attacker grabs the *cert-claim* from `acme.crypto 0.3.9`
(which had a known-fixed bug) and ships it alongside the
*manifest* of `0.4.0`.  Their bet: the consumer trusts both kids,
so won't notice.

Bob's compiler:

- Reads `package_id=acme.crypto, version=0.4.0` from the manifest.
- Reads `package_id=acme.crypto, version=0.3.9` from the cert
  claim body.
- Rejects with `"cert claim version mismatch"`.

Caught by `test_attack2_cert_claim_replay_rejected_on_version_drop`.

#### Scenario C: Swap a transitive dependency

Alice and Carlos both signed the release honestly.  But a
malicious package-root operator replaces a *transitive* dep
(e.g. `std.tls` that `acme.crypto` pulls in) with a different
version.

Carlos's cert-claim records the *full* resolved dep graph at
certification time, including each transitive dep's
`(package_id, version, artifact_sha256, source_content_id,
author_kid, cert_kid)`.  Bob's resolver builds the closure at
compile time:

- The walker hits `std.tls` and sees its on-disk identity does
  not match the entry in Carlos's `dep_graph`.
- `check_dep_graph_covers` fails closed.

Caught by `test_attack4_transitive_dep_swap_rejected`.

#### Scenario D: A certifier-host break-in

The attacker compromises Carlos's CI host and steals the
certifier seed.  They can now emit valid cert claims under
Carlos's trusted kid for any `.dmp` bytes they choose.

**This is serious.**  Certifier compromise is the threat v1
treats as the most likely incident (CI hosts run external code
and live longer than author workstations) and is mitigated, not
eliminated.  Be honest about what the role split costs the
attacker and what it doesn't.

What a stolen certifier kid DOES let the attacker do:

- Build a backdoored `acme.crypto.dmp` themselves.
- Stamp Alice's legitimate `source_content_id` into the
  backdoored manifest (SCI is a value the builder chooses; it is
  only authoritative when verified against the signed bodies
  that quote it).
- Reuse Alice's existing `acme.crypto.author-claim` unchanged.
- Sign a fresh `acme.crypto.cert-claim.<carlos-kid>.json` whose
  body records `artifact_sha256 = <backdoored bytes>` and
  `source_content_id = <Alice's SCI>`.

A normal consumer compiling against this disk layout would
**accept** the load: the author claim verifies under Alice's kid,
the cert claim verifies under Carlos's kid, the manifest's SCI
matches both claim bodies, and the cert claim's `artifact_sha256`
matches the actual `.dmp` bytes on disk.  Normal consumers do not
recompute SCI; they trust the cert claim's attestation.  That is
the explicit cost of the certifier shortcut.

What a stolen certifier kid does NOT let the attacker do:

- **Forge releases under a new identity.**  The attacker cannot
  publish `acme.crypto 0.5.0` (a new version) or push a release
  with a new SCI without Alice's author key.  An author claim
  bound to a new `(package_id, version, source_content_id)`
  triple has to be signed by a kid trusted as **author** for the
  namespace — Carlos's kid is not (role separation).  The author
  key stays on Alice's workstation.
- **Reach beyond the trusted namespaces.**  Carlos's kid is
  granted `certifiers` for `acme.crypto.*` only.  A backdoored
  `acme.payments.dmp` signed by Carlos's kid does not verify
  because Carlos is not in the `certifiers` list for
  `acme.payments.*` (longest-prefix scoping — Scenario E covers
  the same property on the author side).
- **Survive revocation.**  Once Carlos's kid is known
  compromised, adding it to `revoked` in `drift/trust.json`
  filters it out of every namespace's `certifiers` list before
  signature checks; the attacker's cert claims stop verifying on
  the next consumer build.  The diagnostic names the revoked kid
  explicitly so users can correlate it with the `drift trust
  revoke` call.

Defense-in-depth a consumer policy can layer on:

- **Trust scoping** (the load-bearing default).  Grant certifier
  trust narrowly: each namespace's `certifiers` list should
  contain only the kids that genuinely need to attest that
  namespace.  A compromised certifier kid trusted only for
  `acme.*` cannot taint `net.*`.
- **`--require-cert-suite <id>`**.  Pins which cert suite must
  have run (e.g. `drift-deploy/release-2026`).  *Note:* a
  compromised certifier can still write any string into
  `cert_suite.id` and any digest into
  `result_evidence_sha256`, so this flag is primarily a
  defense against an *honest* certifier emitting a release that
  skipped the required suite — not a defense against a fully
  compromised kid.  Treat it as policy enforcement, not
  cryptographic protection.
- **Self-verify / source-rebuild mode** (§5).  A consumer that
  cannot tolerate certifier-shortcut trust can rebuild from
  source and let `compose_verify` recompute SCI locally, which
  bypasses the cert claim entirely.  Carlos's compromised kid
  cannot fake matching SCI because SCI is a function of the
  source bytes, not a value Carlos picks.  This is the strongest
  mitigation but the most expensive — it gives up the certifier
  shortcut.

The structural conclusion: certifier compromise is contained, not
neutralized.  The role split prevents escalation to new author
releases and bounds the attacker to the namespaces the kid was
trusted for; revocation cuts the kid off; suite-id pinning
constrains honest publishers; self-verify is the escape hatch
when you genuinely need to verify bytes-on-disk without trusting
any certifier.

#### Scenario E: Confused-deputy across namespaces

Bob trusts Alice's kid for `acme.crypto.*` (authors role) but not
for `acme.payments.*`.  An attacker tries to publish
`acme.payments 0.1.0` signed by Alice's kid.

Bob's compiler:

- Walks `acme.payments` modules.
- For each module, calls
  `trust.allowed_authors_for_module("acme.payments.foo")`.
- Longest-prefix match against the trust store returns the
  `acme.payments.*` entry, which does NOT contain Alice's kid.
- The author-claim cryptographically verifies (it's a real
  signature), but no signer kid is in the namespace's `authors`
  list → rejection.

Caught by
`test_attack7_prefix_grant_does_not_authorize_unrelated_signer`.

---

### 2.7 What this model deliberately does NOT promise

- **It does not promise byte-for-byte reproducibility.**  A
  certifier signs *the bytes they observed*.  If an independent
  rebuild produces different bytes, that rebuild needs a new cert
  claim — not equality with the original.
- **It does not promise the source is good.**  v1 says "this
  source identity is what the author attested and this artifact
  is what the certifier ran their suite against."  Whether the
  source is malicious, whether the suite is meaningful, and
  whether the result was honestly reported is out of scope for
  cryptographic verification; it lives in `cert_suite.id` and
  policy flags like `--require-cert-suite`.
- **It does not promise the toolchain that built the artifact is
  the same one the consumer compiles with.**  The cert claim
  records `toolchain.driftc_version` for evidence; it is not a
  gate.  Cross-version compatibility is the responsibility of the
  artifact ABI / language spec, not the trust model.

---

## 3. Artifacts and on-disk shape

A published v1 package consists of exactly three files in the
package's directory under a package root:

```
<pkg_root>/<package_id>/<version>/
    <package_id>.dmp
    <package_id>.author-claim
    <package_id>.cert-claim.<certifier-kid>.json
```

Multiple cert-claim sidecars MAY coexist (one per certifier) — the
filename template lives in
[`lang/driftc/packages/sidecar_naming.py`](../../lang/driftc/packages/sidecar_naming.py).
Author-claim sidecars are per-package: O8 (multi-author releases)
appends additional signatures to the *same* `<pkg>.author-claim`
file rather than emitting parallel sidecars.

### 3.1 `.dmp` (compiled package)

The compiled package bytes.  v1 requires the manifest inside the
`.dmp` to carry `source_content_id` (`"sha256:<hex>"`).  An emit
without that stamp produces a package no v1 consumer can accept —
the diagnostic is "package manifest missing source_content_id".

Drift built-in tooling stamps SCI automatically:

- `drift deploy` computes SCI from declared source/asset bytes.
- `drift build` (library mode) does the same with a graceful
  fallback to `None` when the source tree is not fully resolvable
  (test mocks, partial trees), which lets non-verifying builds keep
  working but produces a `.dmp` that the v1 verifier will reject
  at consume time.

### 3.2 `<pkg>.author-claim`

JSON body + ≥1 Ed25519 signature.  Shape lives in
[`author_claim_v1.py`](../../lang/driftc/packages/author_claim_v1.py).
Body fields:

| field                 | meaning                                                          |
| --------------------- | ---------------------------------------------------------------- |
| `schema_version`      | always `1`                                                       |
| `package_id`          | e.g. `"net.tls"`                                                 |
| `version`             | e.g. `"0.3.0"`                                                   |
| `namespaces`          | list of module-id glob patterns the package claims to own        |
| `source_content_id`   | `"sha256:<hex>"` of canonical source/asset bytes                 |
| `required_deps`       | list of `{"name", "version_range"}`                              |
| `target_class`        | e.g. `"library"` / `"app"`                                       |
| `release_utc`         | ISO-8601 release timestamp                                       |

Signatures are stored as a JSON array; each entry has `algo`, `kid`,
and `sig`.  Pubkey bytes are NOT carried inline — they live in the
trust store, indirected by kid.  Unknown top-level keys and unknown
body keys are rejected at load (`_reject_unknown_keys`); the format
is strictly closed.

### 3.3 `<pkg>.cert-claim.<kid>.json`

JSON body + one or more Ed25519 signatures, by certifier kids.
Shape lives in
[`cert_claim_v1.py`](../../lang/driftc/packages/cert_claim_v1.py).

The signatures are carried as a JSON array (same shape as the
author claim).  Multiple signatures on a single cert-claim
sidecar are reserved for key-rotation and multi-region
attestation under a *single* logical certifier identity --
multiple INDEPENDENT certifiers attesting the same package
release each emit a SEPARATE sidecar file (the per-O1
convention: one file per certifier kid, named with that kid in
the filename).  The verifier accepts the claim when at least one
signature in the array verifies under a trusted certifier kid
(the same "any-one signature suffices" rule the author claim
uses).

Body fields:

| field                       | meaning                                                                  |
| --------------------------- | ------------------------------------------------------------------------ |
| `schema_version`            | always `1`                                                               |
| `package_id`                | must match the package's manifest                                        |
| `version`                   | must match the package's manifest                                        |
| `artifact_sha256`           | `"sha256:<hex>"` of the `.dmp` bytes the cert claim covers               |
| `source_content_id`         | must equal the author claim's `source_content_id`                        |
| `target`                    | e.g. `"drift-dev"` — **signed audit metadata, not enforced.** See note below. |
| `toolchain`                 | `{driftc_version, drift_rt_abi, driftc_commit}`                          |
| `dep_graph`                 | full resolved closure (see §3.5)                                         |
| `cert_suite`                | `{id, version, result, result_evidence_sha256}`                          |
| `run_id` / `run_started_utc`| certifier run provenance                                                 |
| `evidence_sha256`           | `sha256:<hex>` of the on-disk `<pkg>.provenance.zst` bytes (see §3.6)    |

Each sidecar file's name embeds one certifier kid (per O1); that
kid is the *primary* signer the sidecar represents.  Additional
signatures inside the same file are extra signers under that
primary identity (key-rotation / multi-region operation).
Independent certifiers always emit separate sidecar files.

**Note on `body.target`.**  The cert claim's `target` field
records the build target the certifier compiled the artifact for
(e.g. `"linux-x86_64"`, `"darwin-aarch64"`, `"drift-dev"`).  v1's
consumer-side `compose_verify` does **not** compare this field
against the consumer's own build target — `target` is signed
audit metadata, useful to inspectors that want to confirm "this
artifact was certified for the target I think it was."  The
underlying cross-target safety guarantee comes from
`artifact_sha256` instead: a `.dmp` built for one target has
different bytes than the same source built for another, so the
artifact-bytes binding already prevents cross-target swap.  If a
future policy needs "certified target must equal consumer
target," it should grow an `expected_target=...` parameter on
`compose_verify` and gate explicitly; today the field is
informational.

### 3.4 Trust store (`drift/trust.json`)

Role-tagged JSON.  Shape lives in
[`trust_v1.py`](../../lang/driftc/packages/trust_v1.py).

```json
{
  "format": "drift-trust",
  "version": 1,
  "keys": {
    "<kid>": {"algo": "ed25519", "pubkey": "<base64-32-bytes>"}
  },
  "namespaces": {
    "<glob>": {
      "authors":    ["<kid>", ...],
      "certifiers": ["<kid>", ...]
    }
  },
  "revoked": ["<kid>", ...]
}
```

Key shape changes versus the pre-v1 form: namespaces are now JSON
objects with explicit `authors` / `certifiers` lists, NOT flat lists
of kids.  `revoked` is a flat list of kids (not an object).  The v1
loader rejects the pre-v1 shapes with a clear "v0 flat list shape is
NOT accepted" diagnostic, deliberately refusing silent upgrades.

Namespaces are matched **longest-prefix wins** against the module
id.  A grant for `acme.crypto.*` outranks a grant for `acme.*` for a
module under `acme.crypto`; the broader grant does not leak into the
narrower one and vice versa.

Trust stores compose: the toolchain ships a `core_trust_v1.json`
that authorizes the reserved namespaces (`std.*`, `lang.*`,
`drift.*`); the project's `drift/trust.json` is unioned on top.  The
core trust store can never be overridden by project trust — reserved
namespaces always resolve to the core kid set.

### 3.5 Source Content ID (SCI) and dep graph

SCI is a `sha256:<hex>` stamp over a canonical encoding of the
package's declared source/asset bytes plus build-relevant metadata
(target class, declared deps, unsafe flag).  It lives in
[`source_content_id.py`](../../lang/driftc/packages/source_content_id.py).
The same SCI value appears in three places that v1 verify checks for
equality:

1. The `.dmp` manifest (`source_content_id`).
2. The author claim body.
3. The cert claim body.

Mismatch on any pair is a hard rejection — this is what blocks
author-claim replay against a different release (§6).

The cert claim's `dep_graph` records the full resolved closure
(direct + transitive) at certification time, with each entry pinning
`(package_id, version, artifact_sha256, source_content_id, author_kid,
cert_kid, dep_kind)`.  The closure walker in
[`closure_walk.py`](../../lang/driftc/packages/closure_walk.py)
fails closed on any missing dep identity rather than emitting a
sentinel — see §6 (attack 4: transitive dep swap).

**What `check_dep_graph_covers` actually pins.**  At consumer
verify time, the cover check compares each consumer-resolved
dep against the parent cert claim's `dep_graph` entry on
**`(package_id, version, artifact_sha256, source_content_id)`**
only.  The entry's `author_kid`, `cert_kid`, and `dep_kind`
fields are **signed audit metadata**: they record what the
certifier observed for that dep at certification time, but they
are not load-bearing in the parent's trust gate.  Per-dep trust
is enforced when the consumer loads each dep individually
(every dep flows through its own `compose_verify`), so the
recorded kids cannot be used to bypass per-dep verification.
If a future requirement is "the parent's view of the dep's kids
must exactly match the consumer's local view," `ResolvedDep`
needs to grow kid fields and the cover check needs to compare
them; today neither happens.

**SCI canonicalization.**  Deliberate semantics:

- **Bytes follow the symlink, path stays logical.**  SCI hashes
  the resolved target bytes (`hash_file` reads through symlinks)
  but records the **logical project-relative path** (`rel`),
  not the resolved real path.  Two source trees that produce the
  same `rel -> bytes` mapping yield the same SCI even if one
  uses regular files and the other uses in-tree symlinks to
  alias content.
- **Symlinks that escape `source_root` are rejected.**
  `_resolve_source_path` raises `ValueError` for any
  module/asset whose resolved real path falls outside the
  declared source root.  This closes the attack where bytes
  controlled by something outside the project tree change SCI
  through a project-internal symlink target.
- **In-tree symlinks are permitted.**  A symlink whose resolved
  target still lives under `source_root` is treated as an alias
  for in-tree content; the project owns the bytes, the symlink
  is just another name for them.

The behavior is pinned by three tests in
[`test_source_content_id.py`](../../lang/tests/packages/test_source_content_id.py):
`test_sci_rejects_module_symlink_outside_source_root`,
`test_sci_accepts_symlink_inside_source_root`, and
`test_sci_symlink_alias_matches_direct_file_with_same_bytes`.

### 3.6 Evidence bundle and the cert-claim binding

Every deploy run that emits a cert claim also produces a
`<pkg>.provenance.zst` evidence bundle next to the `.dmp` and
sidecars.  The bundle is:

- a zstd-compressed JSON envelope recording the certifier's
  observation of the build (compiler info, declared dep table,
  source identity, deployment toolchain versions, and per-dep
  provenance summaries copied from sibling `.provenance.zst`
  files);
- **evidence, not authority** — consumer package load does not
  parse or trust it;
- **unsigned** on disk.

The cert claim binds the bundle into its signed body via
`evidence_sha256`, which equals `sha256:<hex>` of the on-disk
`.provenance.zst` *as-shipped* (the compressed bytes the consumer
receives).  Two consequences:

- **Inspector path**: anyone reading the provenance bundle for
  audit can recompute its sha256 and compare against
  `cert_claim.body.evidence_sha256`; if they match, the bundle's
  bytes are the same ones the certifier signed under.  If a
  mirror swapped the unsigned bundle, the comparison fails and
  the inspector knows the evidence is not what the certifier
  attested.
- **Fail-closed at emit**: the deploy step REFUSES to emit a
  cert claim if the provenance bundle is missing.  There is no
  empty / sentinel digest the cert claim would accept in place
  of a real bundle.  A cert claim asserts the certifier ran a
  suite WITH evidence; a missing bundle means there is no
  evidence to bind.  The reference enforcement is in
  [`drift_deploy.py::_emit_cert_claim_for_artifact`](../../tools/drift_deploy/drift_deploy.py)
  and is pinned by
  `test_c3_invariants.test_missing_provenance_bundle_fails_closed`.

The bundle stays unsigned because the cert claim already carries
the cryptographic anchor; a separate signature would just
duplicate the same property at the cost of a second sidecar
file.  Verification on the consumer side never reads the bundle.

#### Suite-evidence digest vs. provenance-bundle digest

`cert_suite.result_evidence_sha256` is a **separate** field from
`body.evidence_sha256`.  The two carry independent digests:

| Field                              | Digest of                                                        |
| ---------------------------------- | ---------------------------------------------------------------- |
| `body.evidence_sha256`             | the on-disk `.provenance.zst` (the run-level evidence bundle)    |
| `body.cert_suite.result_evidence_sha256` | the suite's OWN evidence artifact (test logs, coverage report, vendor cert PDF, ...) |

The provenance-bundle digest is enforced unconditionally and
fail-closed (the run produced a provenance bundle; we bind its
bytes).  The suite-evidence digest is provided by the operator
via `DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256`.  v1 deliberately
refuses to default this field to a synthetic constant — a signed
cert claim that records "the suite ran with evidence" must point
at a real evidence digest the operator supplied.

For suites that legitimately produce no artifact (rare; some
manual-review or attestation-only suites), the operator opts in
explicitly:

```text
DRIFT_DEPLOY_CERT_SUITE_EVIDENCE_SHA256=sha256:<empty-bytes-hash>
DRIFT_DEPLOY_CERT_SUITE_NO_EVIDENCE=1
```

Both env vars must be set together.  When the opt-in is active,
the deploy logs a clearly-labeled warning ("cert suite ... is
being signed with the empty-evidence sentinel") to stderr so the
choice is visible in the build log.  Setting the env to the
empty hash *without* the opt-in is treated as a misconfiguration
and the deploy refuses.  The policy line: "suite chose no suite
evidence," never "default no evidence."

The two fields are independent: the provenance-bundle binding is
on every cert claim and cannot be turned off; the suite-evidence
sentinel only governs `result_evidence_sha256`.

---

## 4. Runtime enforcement: how the consumer verifies

This is the heart of v1: the actual checks `driftc` runs before
any bytes from a dep get linked into the consumer's build.  The
caller is not asked to opt in — every `--dep <pkg>@<ver>` flows
through this gate.  Failure of any step aborts the compile with a
specific diagnostic naming what failed (kid, namespace,
mismatched field).

The reference implementation is
[`verify_v1.compose_verify`](../../lang/driftc/packages/verify_v1.py);
it is invoked per-module by
[`provider_v1.load_package_v1_with_policy`](../../lang/driftc/packages/provider_v1.py)
during normal compilation, and by `drift-deploy` / `drift prepare`
in source-rebuild mode (§5).  The compiler does **not** maintain a
separate "we already verified this" cache that bypasses re-checks:
every consumer of every module hits the same gate.

In casual terms — what runs before any code from a dep is touched:

> *Open the `.dmp`.  Read who it claims to be.  Find its two
> sidecars next to it.  Confirm that an author kid the trust store
> grants `authors` for this namespace signed a claim binding this
> source identity to this `(package_id, version)`, and that a
> certifier kid the trust store grants `certifiers` for this
> namespace signed a claim binding the actual artifact bytes back
> to the same source identity.  Compare resolved deps to what the
> certifier saw.  If any step fails, abort the compile.*

A regular consumer compiling against a v1 package executes this
flow once per `--dep <pkg>@<ver>`.

### 4.1 Load package identity

Read `package_id` and `version` from the `.dmp` manifest plus the SCI
stamp.  An unstamped manifest is an immediate failure.

### 4.2 Verify the author claim

For each module the consumer needs from the package:

1. Discover `<pkg>.author-claim` next to the `.dmp`.
2. Load the JSON; reject unknown top-level / body keys.
3. Check `body.package_id == manifest.package_id` and
   `body.version == manifest.version`.
4. Check that *some* `body.namespaces[i]` covers the module's
   `module_id` (longest-prefix glob match).
5. Check `body.source_content_id == manifest.source_content_id`.
6. Cryptographically verify *some* signature in `claim.signatures`
   against a trust-store pubkey.
7. Of the kids that verified, check at least one is granted the
   **`authors`** role for the module's namespace (after applying the
   `revoked` set).

Failure of any step rejects the load.  The diagnostic surfaces the
specific step (covered modules, signer kids, etc.) so the user can
correlate it to the trust store and the published claim.  A
revoked-kid rejection is explicit: the diagnostic names the revoked
kid by string when revocation is the cause of a rejection.

### 4.3 Verify a cert claim

The consumer enumerates `<pkg>.cert-claim.<kid>.json` sidecars next
to the `.dmp` and tries each in turn until one verifies, unless
`--require-certifier <kid>` is set (then exactly that sidecar must
verify).

For a single cert claim:

1. Reject unknown keys.
2. Check `body.package_id` / `body.version` against the manifest.
3. Check `body.source_content_id == author_claim.body.source_content_id`.
4. Check `body.artifact_sha256` matches the actual `.dmp` bytes.
5. Check `body.cert_suite.result == "pass"` — **unconditional**;
   any other value (including `"fail"`, `"skip"`, or omitted)
   rejects the claim regardless of `--require-cert-suite`.
6. If `--require-cert-suite <id>` is set, check
   `body.cert_suite.id == <id>` (this flag pins only the suite
   id; the `"pass"` requirement is enforced at step 5
   independently).
7. Verify *some* signature in `claim.signatures` against a
   trust-store pubkey.
8. Of the kids that verified, check at least one is granted the
   **`certifiers`** role for every module the consumer needs (and
   is not revoked).
9. Verify the `dep_graph` covers every entry in the consumer's
   resolved closure (`check_dep_graph_covers`).

### 4.4 Where the `--require-*` flags apply

These are O4 / O7 sign-off flags exposed on the consumer-side
`compose_verify` call:

- **`--require-certifier <kid>`** — only the sidecar signed by
  exactly `<kid>` may satisfy the cert-claim step.  Used when a
  policy demands a specific certifier (e.g. internal red-team) even
  though other certifier kids are trusted for the namespace.

- **`--require-cert-suite <id>`** — the chosen cert claim's
  `cert_suite.id` must equal `<id>`.  Used when a policy demands
  a specific test/audit suite was run before consumption (e.g.
  `"sca-2026"` for an SCA scan, `"mariadb-cert/full"` for a
  vendor cert).  Note: the `"pass"` requirement on
  `cert_suite.result` is enforced unconditionally (step 5
  above), not by this flag — `--require-cert-suite` only pins
  *which* suite the consumer demands proof of, not whether the
  result-must-pass invariant applies.

Both flags are incompatible with `self_verify=True` (see §5):
self-verify bypasses the certifier shortcut, so pinning a specific
certifier or suite makes no sense in that mode.

---

## 5. Self-verify / source-rebuild mode

Self-verify is the "I just built this from source myself" mode used
by source-rebuild certifiers and by a publisher's CI before they
emit their own cert claim.  It is *opt-in* via the
`self_verify=True, self_verify_sci=<sha256:...>` arguments to
`compose_verify`.

### 5.1 What changes

- The consumer (or certifier) walks the source tree and computes
  SCI for itself.  The recomputed value is passed as
  `self_verify_sci`.
- `compose_verify` checks `self_verify_sci ==
  author_claim.body.source_content_id`.  Mismatch is a hard
  rejection.
- The cert-claim step is *skipped*.  The trust anchor in this mode
  is the locally-recomputed SCI, not a third-party certifier's
  attestation.
- `--require-certifier` / `--require-cert-suite` are rejected as
  incompatible (see §4.4).

### 5.2 What it proves

- The bytes you just compiled produce the same SCI the author
  claim was signed over.
- A signer kid you trust as **author** for the module signed that
  claim and is not revoked.

In effect: "the source on this disk corresponds to the release this
author kid attested."  This is the foundation of source-rebuild
certification — the certifier runs self-verify on freshly-rebuilt
sources, then emits a new cert claim binding the rebuilt artifact
bytes back to the author's source identity.

### 5.3 What it does NOT prove

- It does NOT prove anything about the artifact bytes.  The cert
  claim is skipped; `artifact_sha256` is not consulted.
- It does NOT prove the rebuild used the same toolchain or
  produced the same artifact as the author's original release.
  A self-verify pass is consistent with the rebuilder producing
  a totally different `.dmp` byte sequence from the author's
  original — bit-for-bit reproducibility is not in scope.
- It does NOT run the cert claim's `dep_graph` cover check.
  Self-verify's `compose_verify` returns OK on author-SCI match
  alone; `check_dep_graph_covers` is in the cert-claim path,
  which self-verify skips by design.  This is coherent only
  because self-verify trusts the caller's local resolve — every
  dep the caller loads still flows through its own per-dep
  `compose_verify`, so a malicious resolved closure cannot
  smuggle in an untrusted dep.  Self-verify is incompatible with
  `--require-certifier` / `--require-cert-suite` for the same
  reason: those flags pin a certifier-shortcut decision the
  self-verify path doesn't make.
- It does NOT prove the resolved dep graph is the same one any
  *other* certifier attested.  The local resolve at rebuild time
  is the dep graph in this mode.

### 5.4 Why this is safe

Self-verify is consumed by the entity doing the rebuild
(orch / certifier / publisher CI).  That entity already trusts its
own filesystem and its own resolve; what it needs is a
cryptographic anchor saying "this source identity is the one the
trusted author kid attested under this package_id@version."
Self-verify provides exactly that and nothing more.

---

## 6. Attack coverage

The v1 model defends against the attacks below.  Each entry names
the load-bearing test in
[`lang/tests/packages/test_v1_adversarial.py`](../../lang/tests/packages/test_v1_adversarial.py)
that pins the property to the code.

| #  | Attack                              | Test                                                                     | Defense                                                                 |
| -- | ----------------------------------- | ------------------------------------------------------------------------ | ----------------------------------------------------------------------- |
| 1  | Repo substitution                   | `test_attack1_repo_substitution_rejected`                                | author + cert claim bind `(package_id, version)` to the manifest        |
| 2a | Cert-claim replay (version drop)    | `test_attack2_cert_claim_replay_rejected_on_version_drop`                | cert body `version` checked against manifest                            |
| 2b | Cert-claim replay (package drop)    | `test_attack2_cert_claim_replay_rejected_on_package_drop`                | cert body `package_id` checked against manifest                         |
| 3  | Author-claim replay (old SCI)       | `test_attack3_author_claim_replay_old_sci_rejected`                      | three-way SCI equality (manifest = author = cert)                       |
| 4  | Transitive dep swap                 | `test_attack4_transitive_dep_swap_rejected`                              | `dep_graph` records full closure with per-dep `(sha256, SCI, kids)`; `check_dep_graph_covers` fails closed on any swap |
| 5  | Weak-suite substitution             | `test_attack5_weak_suite_substitution_rejected`                          | `--require-cert-suite` pins the suite id; `cert_suite.result == "pass"` is enforced unconditionally |
| 6a | Wrong-role key (cert kid → author)  | `test_attack6_cert_kid_signs_author_claim_rejected`                      | role-tagged trust: cert-only kid is not in `authors` for any namespace  |
| 6b | Wrong-role key (author kid → cert)  | `test_attack6_author_kid_signs_cert_claim_rejected`                      | symmetric: author-only kid is not in `certifiers`                       |
| 7a | Namespace shadowing (longest-prefix)| `test_attack7_longest_prefix_wins_more_specific_grant`                   | trust store applies longest-prefix-wins; broader grant does not satisfy narrower namespace |
| 7b | Cross-namespace authority leak      | `test_attack7_prefix_grant_does_not_authorize_unrelated_signer`          | broader grant does not authorize a signer of an unrelated narrower namespace |
| 8a | Unsigned metadata injection (body)  | `test_attack8_extra_field_in_author_body_rejected_at_load`               | strict unknown-key rejection inside the body                            |
| 8b | Unsigned metadata injection (top)   | `test_attack8_extra_top_level_field_rejected_at_load`                    | strict unknown-key rejection at the top level                           |
| 9a | Multi-signer confusion (alien sig)  | `test_attack9_alien_signature_does_not_authorize`                        | only signatures by *trusted* kids count toward verification             |
| 9b | Multi-signer confusion (only alien) | `test_attack9_only_alien_signature_rejected`                             | rejection when no trusted kid signed                                    |
| 9c | Multi-signer confusion (wrong body) | `test_attack9_trusted_sig_over_different_body_rejected`                  | signature is computed over canonicalized body; tampering invalidates it |
| 10a| Self-verify false claim             | `test_attack10_self_verify_sci_mismatch_rejected`                        | self-verify checks `self_verify_sci == author_claim.body.source_content_id` |
| 10b| Self-verify happy path              | `test_attack10_self_verify_matching_sci_accepts`                         | positive control                                                        |

The single-file test module is the canonical security source of
truth.  When the trust contract evolves, the matrix above is the
checklist that needs to remain green; any pinned property dropped
without a written rationale and a corresponding test edit is a
regression.

---

## 7. Operational guidance

### 7.1 Author workflow

Author-side signing lives entirely in `tools/drift_author/` and is
intentionally walled off from the certifier/deploy pipeline.

```text
drift-author publish               \
    --sidecar-dir <pkg-dir>        \
    --package-id <id>              \
    --version <ver>                \
    --namespace <glob>             \   # repeatable
    --source-content-id sha256:... \
    --required-dep <name>=<range>  \   # repeatable
    --target-class library         \
    --release-utc 2026-05-19T00:00:00Z \
    --key-file <author.seed>
```

For multi-author releases (O8), append signatures with `cosign`:

```text
drift-author cosign            \
    --sidecar-dir <pkg-dir>    \
    --package-id <id>          \
    --key-file <co-author.seed>
```

Authors load their private seeds via `tools.drift_author.key_loader`,
which the import-boundary check forbids any deploy / certifier
module from importing.  The structural separation is part of the
contract, not a convention; do not erode it.

### 7.2 Certifier workflow

Cert-claim emission lives in
[`tools/drift_deploy/cert_emit.py`](../../tools/drift_deploy/cert_emit.py)
and is invoked by `drift-deploy` after the artifact is built.  The
certifier loads:

- the already-emitted `<pkg>.author-claim` (to read its
  `source_content_id` and validate cross-binding);
- the artifact bytes (to compute `artifact_sha256`);
- the resolved dep closure from the build's lockfile +
  just-emitted co-artifact sidecars;

and emits `<pkg>.cert-claim.<kid>.json` over the certifier kid.
The certifier kid is the only private key needed on the deploy host;
the author's private key is never required there.

A certifier may legitimately re-sign the same `.dmp` after re-running
a suite (different `cert_suite.id` or run_id), producing additional
side-by-side sidecars; this is the intended multi-cert pattern.

### 7.3 Trust granting

Trust is granted per namespace, per role.  The CLI surface is the
`drift trust` family — `add`, `revoke`, `list`, plus the
`drift init` / `drift trust <profile>` profile pipeline for the
common "trust an author's published kid" workflow:

```text
drift trust add                            \
    --trust-store drift/trust.json         \
    --namespace acme.crypto.*              \
    --pubkey-b64 <base64-32>               \
    --kid ed25519:<kid>                    \
    --role author          # or 'certifier' or 'both'
```

`--role both` is the dev-time shortcut; production trust stores
SHOULD pass `author` or `certifier` explicitly so the role
separation is visible in the file.

Revocation flips a kid into the `revoked` list.  The verifier
filters revoked kids out of the per-namespace allowlist *before*
checking signatures, so a previously-trusted signer's kid no longer
satisfies any module.  The author-claim diagnostic names the
revoked kid by string when revocation is the cause of a rejection,
so users can correlate the failure to the `drift trust revoke` call
that produced it.

### 7.4 Foundation is not special

The Drift Foundation appears in `core_trust_v1.json` in **both**
the `authors` and `certifiers` role for the reserved namespaces
(`std.*`, `lang.*`, `drift.*`) — but those two roles MUST
correspond to **independently controlled keys with independent
custody paths**.  The Foundation may play both roles
organizationally; the system still models two roles, two keys,
and two signing pipelines.

The role-tagging is what carries the contract: Foundation's
*author kid* satisfies the `authors` step because it appears in
the `authors` list of `core_trust_v1.json`, and Foundation's
*certifier kid* satisfies the `certifiers` step because it
appears in the `certifiers` list.  These are not the same kid
and not the same key material.  Dropping one of those kids from
its list takes that role away from Foundation exactly like any
other signer.

Production trust stores that want fully independent certifiers
for the stdlib MAY ship a project trust store that adds a
non-Foundation certifier kid to the `certifiers` list for the
reserved namespaces — the rest of the verification flow is
unchanged.

### 7.5 Toolchain bootstrap (stdlib build)

The toolchain ships the stdlib as a package and must produce v1
sidecars + a `core_trust_v1.json` for it.  This raises a custody
question: the deploy host that builds the toolchain dist runs
**certifier-role code only**; it must never hold the Foundation
author private key.

The hard rule: **the deploy step does not generate, store, or
read any author private key.**  Stdlib author-claim production
is a separate, prior step performed by the Foundation under
author-key custody.  Acceptable shapes for that step:

- a pre-signed `std.author-claim` checked into a Foundation-
  controlled release repository and fetched by the deploy
  pipeline as an input artifact;
- an offline `drift-author publish` run on a Foundation
  signing workstation (the seed never leaves that machine)
  producing the artifact, which is then handed to the deploy
  pipeline through normal file-transfer channels;
- a separate Foundation author-signing service / API that
  emits author claims after policy checks and exposes them as
  read-only artifacts for deploy.

What is **not** acceptable:

- `tools/deploy/steps/stdlib.py` generating an author seed;
- the deploy host writing any author seed to disk, even
  ephemerally;
- certifier code invoking author-claim signing with a seed it
  created;
- documenting any of the above as a "bootstrap exception."

Mechanically: `tools/deploy/steps/stdlib.build_and_install_stdlib`
takes the stdlib author claim path and the matching author
pubkey as **required inputs**.  It validates the claim
(package_id, version, SCI, namespaces, signer kid match the
build), mints a fresh **certifier** keypair, emits the cert
claim, writes `core_trust_v1.json` with the Foundation author
kid in the `authors` list and the deploy-minted certifier kid in
the `certifiers` list, and the run ends.  The certifier seed is
in-process only; the author seed never appears in the deploy
process at all.

The role-separation check in
`lang/tests/packages/test_author_key_boundary.py` enforces the
import boundary statically: nothing under `tools/deploy/` or
`tools/drift_deploy/` may import `tools.drift_author.*`, and
nothing in the orch tree may name an author-seed file path.  The
contract is part of the test matrix, not a convention.

---

## 8. Module map

The v1 contract is implemented across these files.  Keep this list
in sync if you rename or split a module.

| Concern                          | File                                                                                            |
| -------------------------------- | ----------------------------------------------------------------------------------------------- |
| Author claim format + verify     | [`lang/driftc/packages/author_claim_v1.py`](../../lang/driftc/packages/author_claim_v1.py)      |
| Cert claim format + verify       | [`lang/driftc/packages/cert_claim_v1.py`](../../lang/driftc/packages/cert_claim_v1.py)          |
| Composed verify (per-module)     | [`lang/driftc/packages/verify_v1.py`](../../lang/driftc/packages/verify_v1.py)                  |
| Trust store loader               | [`lang/driftc/packages/trust_v1.py`](../../lang/driftc/packages/trust_v1.py)                    |
| Resolved-closure walker          | [`lang/driftc/packages/closure_walk.py`](../../lang/driftc/packages/closure_walk.py)            |
| Sidecar filename rules           | [`lang/driftc/packages/sidecar_naming.py`](../../lang/driftc/packages/sidecar_naming.py)        |
| SCI computation                  | [`lang/driftc/packages/source_content_id.py`](../../lang/driftc/packages/source_content_id.py)  |
| Consumer-side load               | [`lang/driftc/packages/provider_v1.py`](../../lang/driftc/packages/provider_v1.py)              |
| Format-level shape validators    | [`lang/driftc/packages/package_validate.py`](../../lang/driftc/packages/package_validate.py)    |
| `drift-author publish` / cosign  | [`tools/drift_author/`](../../tools/drift_author/)                                              |
| `drift-deploy` cert-claim emit   | [`tools/drift_deploy/cert_emit.py`](../../tools/drift_deploy/cert_emit.py)                      |
| `drift trust` CLI                | [`lang/drift/trust.py`](../../lang/drift/trust.py) + [`lang/drift/cli.py`](../../lang/drift/cli.py) |
| Adversarial test suite           | [`lang/tests/packages/test_v1_adversarial.py`](../../lang/tests/packages/test_v1_adversarial.py)|
| Import-boundary check            | [`lang/tests/driver/test_import_boundaries.py`](../../lang/tests/driver/test_import_boundaries.py) |

---

## 9. Migration note

The pre-v1 `.sig` envelope / `.source-attestation` sidecar shape is
gone.  The trust-v1 cutover deletes the v0 modules
(`signature_v0.py`, `trust_v0.py`, `provider_v0.py`) and the v0 CLI
surfaces (`drift sign`, `drift publish`, `drift package
inspect-signers`).  Tooling that previously emitted v0 envelopes
must move to `drift-author publish` + `drift-deploy` cert-claim
emission; consumers that previously consumed `.sig` sidecars get
v1 author + cert claims for the same packages.

`drift/lock.json` v4 keeps its on-disk field names (`author_key`,
`source_attestation_key`) for backward compatibility, but the
semantics underneath are v1: `author_key` carries the cert-claim
signer kid and `source_attestation_key` carries the author-claim
signer kid.  A future lockfile-v5 rename will flip the spelling to
match the v1 vocabulary; until then, treat the field names as
historical labels and look at the cert/author distinction.
