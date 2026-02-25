# Plan: Compiler/Runtime ABI version stamping (link-time guard)

Date: 2026-02-25
Owner: compiler team
Status: completed (2026-02-25)

## 1. Problem statement
`driftc`, `libdrift_rt.a`, and stdlib artifacts can drift in version. When compiler-generated ABI expectations and runtime-provided ABI differ, failures currently show up as runtime crashes or opaque behavior instead of deterministic build-time errors.

Goal: fail at link-time when compiler/runtime ABI versions are incompatible.

## 2. Proposed mechanism (primary)
Use a link-time ABI symbol contract.

1. Runtime exports exactly one ABI marker symbol for the currently supported ABI:
   - `__drift_rt_abi_version_<N>`
2. Compiler emits a required reference to the same exact symbol `<N>` in generated output.
3. Link step succeeds only when both sides agree on `<N>`.
4. On mismatch, linker fails with undefined symbol (fast, deterministic, no runtime crash).

This follows established toolchain patterns used by language runtimes and C libraries.

## 3. Design decisions

### 3.1 ABI version source of truth
Define a single shared ABI version constant in repo tooling/build configuration (integer).
- Example: `DRIFT_RT_ABI_VERSION = 12`
- This value must drive both:
  - runtime symbol emitted into `libdrift_rt.a`
  - compiler symbol reference emitted by codegen

No semver here: monotonic integer bump on ABI break.

### 3.2 Runtime symbol shape
Export a strong symbol in runtime C code:
- `void __drift_rt_abi_version_<N>(void) {}`

Notes:
- Must be present in all runtime variants (debug/asan/alloc_track/default, etc.).
- Must not be `static`.
- Keep this symbol always available even under optimization/LTO.

### 3.3 Compiler reference shape
Emit an explicit use of the ABI marker so it cannot be dropped as dead code.
Preferred approach:
- add a call to `__drift_rt_abi_version_<N>()` from generated entry wrapper path.

Alternative acceptable approach:
- emit an extern declaration + used global referencing symbol, but direct call is simpler and more linker-stable.

### 3.4 Failure UX
Base behavior: linker undefined symbol is acceptable and already actionable.
Optional improvement:
- detect this symbol mismatch in driver link error parsing and append hint:
  - `driftc targets runtime ABI vX; linked runtime provides different ABI. Rebuild runtime/std artifacts.`

## 4. ABI bump policy
Bump ABI integer when changing any compiler/runtime boundary contract, including:
- runtime-exported helper signatures consumed by codegen
- data layouts crossing boundary (struct/variant/frame payload ABI)
- calling convention/return packing used between generated code and runtime
- ownership/drop contract changes that alter boundary behavior

Do not bump for pure internal refactors with no boundary change.

## 5. Implementation plan (phased)

### Phase A: Runtime marker
- Add ABI marker symbol source file in runtime C sources.
- Wire version constant into runtime build (all variants).
- Verify final archives export `__drift_rt_abi_version_<N>`.

### Phase B: Compiler marker reference
- Add codegen emission of marker reference/call in generated module entry path.
- Ensure this is emitted for all normal compile/link flows (tests and CLI paths).

### Phase C: Driver diagnostics (optional but recommended)
- Parse linker failure output for `__drift_rt_abi_version_` unresolved symbols.
- Emit concise compatibility hint without hiding linker stderr.

### Phase D: Documentation
- Add short section to ABI/runtime docs describing stamping and bump rules.
- Add note to contributor docs: ABI-breaking runtime/codegen changes require bump + tests.

## 6. Regression and validation matrix

### 6.1 Positive tests
1. Matching versions:
- compile+link+run succeeds.
2. All runtime variants:
- debug/default/asan/alloc_track archives each export ABI marker symbol.
3. Codegen presence:
- generated IR/object includes reference/call to ABI marker.

### 6.2 Negative tests (mandatory)
1. Mismatch fails at link-time:
- fixture where compiler expects `vN+1`, runtime provides `vN` (or vice versa)
- assert link failure and unresolved `__drift_rt_abi_version_<expected>` symbol.
2. Diagnostic hint test (if Phase C implemented):
- assert hint text is appended for this mismatch class.

### 6.3 Non-regression checks
- Existing compile/link e2e suites continue green with matching ABI.
- No runtime behavior changes aside from mismatch detection.

## 7. Boundary Contract Guardrails mapping
This is a boundary contract hardening change. Required alignment:
1. Positive e2e proving normal boundary works with matching ABI.
2. Negative regression proving mismatch fails clearly at link boundary.
3. Update stale comments/docs about runtime compatibility guarantees.
4. Keep version check centralized (single symbol mechanism), no scattered ad-hoc checks.

## 8. Risks and mitigations
- Risk: symbol gets optimized away.
  - Mitigation: explicit runtime function + explicit call from generated entry path.
- Risk: only some runtime variants get marker.
  - Mitigation: archive symbol validation test across all produced runtime libs.
- Risk: version constant drifts between runtime and compiler build scripts.
  - Mitigation: single source-of-truth constant consumed by both build paths.

## 9. Definition of done
Feature is complete when all are true:
1. Runtime exports ABI marker in all runtime library variants.
2. Compiler emits required marker reference.
3. Link mismatch regression fails deterministically with clear symbol evidence.
4. Matching versions pass full relevant compile/link tests.
5. Docs/history updated with policy and bump guidance.

## 12. Implementation outcome (completed)
- Phase A complete:
  - runtime marker symbol exported from `lang/language_runtime/abi_version_stamp.c`
  - runtime build injects `-DDRIFT_RT_ABI_VERSION=<N>` from `lang/driftc/driftc_versions.py`
  - all runtime archive variants include the marker.
- Phase B complete:
  - compiler emits ABI marker reference in production entrypoint-linked code paths.
  - implementation uses module ctor registration for stamped modules.
- Phase C complete:
  - driver appends ABI mismatch hint when linker stderr contains `__drift_rt_abi_version_`.
- Phase D complete:
  - ABI stamping and bump guidance documented in `docs/design/drift-lang-abi.md` and agent policy (`AGENTS.md`).
- §11 complete:
  - `driftc --version` / `-V` outputs compiler version, ABI version, git SHA (when available), license, and foundation.

### Final contract note
- Stamp emission is intentionally tied to code paths that are expected to link against runtime (entrypoint-enforced or argv-wrapper paths).
- Helper-only IR paths used by bare-clang unit tests remain unstamped to avoid introducing runtime-link dependency into low-level tests.

## 10. Nice-to-have follow-up
- Extend mechanism to also stamp stdlib package ABI compatibility if/when stdlib binary ABI gets independently versioned.
- Add tooling command to print current compiler/runtime ABI versions for support workflows.

## 11. Additional item: `driftc --version` metadata output
Add compiler version reporting as part of this effort so operators can quickly verify compatibility context.

Required output fields:
- compiler version string
- `DRIFT_RT_ABI_VERSION`
- build git SHA (when available)
- license (`GPL-3.0`)
- supervising body (`The Drift Language Foundation`)

Implementation notes:
- keep the output stable and script-friendly (one-line or deterministic multi-line format),
- source ABI value from the same single authoritative constant used for stamping,
- do not use git SHA as compatibility gate; it is diagnostic-only.

Validation:
1. `driftc --version` works in normal builds and prints all required fields.
2. ABI value in `--version` matches emitted runtime/codegen stamp version.
3. Optional test: regex-based smoke test for required tokens in output.
