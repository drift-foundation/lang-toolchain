# Module/Packages Phase 5 polish — work tracker

Goal: make Drift’s package workflow *reproducible, deterministic, and debuggable* without adding network registries yet.

This tracker covers **Phase 5** only (tooling polish on top of existing `drift publish/fetch/vendor` and package verification).

---

## Pinned invariants (do not relax)

### Lockfile authority
- If `drift.lock.json` exists, **no resolution logic runs**.
- Any missing/mismatched/unverifiable lock entry is a **hard error** (never a warning, never “try another source”).
- `drift fetch` **never** mutates `drift.lock.json`.
- `drift.lock.json` is written only by:
  - `drift vendor` (and later `drift update` if/when added).

### Determinism
- No filesystem iteration order affects resolution.
- Resolution is fully specified by stable sort keys (below).

### Trust boundary
- Identity mismatches and hash mismatches are **always fatal**.
- No `--ignore`, no `--force`, no fallback behavior for integrity/trust errors.

---

## Glossary (pinned terms)
- **source**: a configured origin, identified by `source_id` (MVP: directory-backed).
- **index**: metadata listing packages inside a source (`index.json`).
- **artifact**: the fetched `pkg.dmp` bytes (and optional `pkg.dmp.sig`).

---

## Phase 5.1 — Lockfile becomes authoritative

### Schema v0 (pinned fields)
- `format`, `version`
- `packages: { package_id -> entry }`
- Each entry must include:
  - `package_id`, `version`, `target`
  - `observed_identity: { package_id, version, target }` (echo from the verified artifact)
  - `sha256` (hex of exact `pkg.dmp` bytes)
  - `source_id` (must correspond to a configured source; no `"unknown"`)
  - `sig_kids[]` (optional informational)
  - `path` (optional informational)

### Validation (must be centralized)
1) **Schema**: correct shapes/types.
2) **Semantic**:
   - `package_id/version/target` == `observed_identity.*`
   - `sha256` well-formed
   - `source_id` present and not `"unknown"`
3) **Policy**:
   - unknown fields rejected unless lock version explicitly allows them

### Tool behavior (pinned)
- `drift fetch` with lock:
  - fetches *exactly* the locked identity+sha256 from the locked `source_id`
  - verifies artifact bytes hash equals lock sha256
  - verifies signatures/trust as configured

### Status
- [x] Implement lock schema/validator updates (`observed_identity`, mandatory `source_id`)
- [x] `drift vendor` writes authoritative lock from vendored bytes (sha256 over bytes)
- [x] `drift fetch` honors lock strictly (no resolution, no lock mutation)
- [x] Tests: lock-authoritative + legacy-lock rejection

---

## Phase 5.2 — Deterministic multi-source selection (unlocked mode)

### Inputs
- `drift-sources.json` v0 contains ordered/priority-tagged dir sources:
  - `id` (stable ASCII identifier)
  - `priority` (required integer, lower wins)
  - `path` (directory)

### Deterministic selection rule (pinned)
When no lock exists, for a requested `(package_id, version, target)`:
1) gather all candidates across all sources
2) sort by:
   - `(priority, source_id, normalized_package_path)`
3) select the first candidate

Normalization:
- `normalized_package_path` uses `/`, no `.` or `..`.
- do not rely on OS directory order at any point.

### Additional pinned rule (supply-chain clarity)
- If the same identity can be satisfied by more than one source (even if bytes/sha match), this is an **error** unless the lock exists.

### Status
- [x] Add/validate `drift-sources.json` schema with explicit `priority`
- [x] Implement deterministic selection by stable tuple key
- [x] Reject ambiguous identity available in multiple sources when unlocked
- [x] Tests: deterministic selection + ambiguity rejection (unlocked)

---

## Phase 5.3 — Index/identity mismatch errors (sharp + structured)

### Internal error context (pinned)
When failing on index/identity/hash errors, build a structured context:
- `error_code`
- `claimed_identity`
- `observed_identity`
- `sha256` (bytes)
- `source_id`
- `index_path`
- `artifact_path`
- `signer_kids?` (optional)

### Error severity (pinned)
- `IDENTITY_MISMATCH` and `SHA_MISMATCH` are **always fatal**.

### Status
- [x] Improve mismatch error messages to include claimed vs observed + source/index context
- [x] Add stable error codes/substrings for CI assertions
- [x] Tests: malformed index, sha mismatch, identity mismatch (prints both)

---

## Phase 5.4 — `drift fetch --json` (machine output)

### Output contract (pinned)
- Include `mode: "lock" | "unlocked"`
- Include per-package:
  - selected `source_id`
  - identity fields
  - sha256
  - verification summary (hash/signature/trust)
  - cache paths written
- No human text mixed into JSON output.

### Status
- [x] Implement `--json` output for `drift fetch`
- [x] Tests: required keys present; mode is correct

---

## Phase 5.5 — `drift doctor` (sanity checks)

### Classification (pinned)
Doctor checks classify issues as:
- **fatal** (build impossible)
- **degraded** (build possible but unsafe)
- **info**

MVP can exit on first fatal; later we can add `--all`.

### Minimum checks (MVP)
- sources file schema + readability
- index readability and required fields
- trust store parseability + pubkey length checks
- lock parseability + internal consistency
- cache/vendor optional consistency checks (flag-gated)

### Status
- [x] Implement `drift doctor` with fatal/degraded/info classification
- [x] Tests: bad sources, bad trust store, lock/cache mismatch

---

## Guardrail for execution order

- Do not start Phase 5.3+ until Phase 5.1 and 5.2 tests pass without flags or fallbacks.
