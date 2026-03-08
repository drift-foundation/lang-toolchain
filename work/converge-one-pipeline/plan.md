# Converge One Pipeline Plan (Review-First)

Status: Draft for architectural review only
Owner: Compiler team
Reviewer: Klaudia (primary)
Date: 2026-03-07

## Purpose
This document is a review-first plan to converge local compilation and external package-consumer compilation into one pipeline.

This phase is design only.
Do not implement code changes yet.

Primary objective of this draft:
- Klaudia reviews the architecture critically.
- Klaudia identifies weak assumptions, missing invariants, and risky sequencing.
- Klaudia fills in module/function-level detail.
- We agree on execution order and rollback boundaries before touching code.

## Why This Exists
Package support exists, but package-consumer parity has not been enforced systematically.

That produced repeated divergence between:
- local/source compilation path
- external package-consumer path

Observed failure classes across the K-series include:
- TypeId remap incompleteness and forward nominal drift
- duplicated transform application
- callable registry divergence
- generic vs monomorphized method resolution ambiguity
- wrapper/can_throw shape mismatch
- reachability and emission gaps
- entry-wrapper dependency drift
- package-only runtime ownership bugs

The practical result is that local compilation and package-consumer compilation are behaving like two partially separate compilers.

## Hard Requirements
These are non-negotiable and should shape the design review.

1. One compiler, two input origins.
Local modules and package-loaded modules may enter through different loaders, but they must converge before semantic-sensitive stages diverge.

2. No stdlib-specific semantic hacks.
Any fix in visibility, trait scope, method resolution, or codegen must apply to arbitrary external packages, not just `std.*`.

3. Regression-first remains mandatory.
Every new bug class uncovered during convergence must have a pinned regression before the root-cause fix lands.

4. Temporary fallbacks must be explicit.
If a temporary package-consumer fallback is required, it must be:
- package-generic
- documented in code and plan
- paired with a removal target
- guarded by negative regressions

## Non-Goals (for this planning phase)
- No refactor implementation
- No opportunistic cleanup commits
- No unrelated test churn
- No stdlib/app workarounds for compiler defects

## Current Architectural Smells
This section exists to orient the review around known failure modes.

### A. Duplicated Pipeline Logic
Known examples:
- package MIR deserialization/remap path separate from local MIR production
- separate callable/signature registration logic
- separate reachability assumptions
- separate codegen entry/wrapper assumptions

### B. Implicit Contracts
The system currently relies on contracts that are only partially encoded:
- "all package TypeIds are remapped"
- "all needed wrappers are emitted"
- "entry wrapper dependencies exist if called"
- "wrapper calls preserve can_throw shape"
- "trait scope/visibility for package modules is reconstructed correctly"

### C. Policy Drift Between Paths
Examples from recent fixes:
- package path needed different reachability edges
- package path re-applied transforms already applied locally
- signature visibility and wrapper handling diverged

## Target Architecture
Single logical pipeline with two input origins:
- `origin=local`
- `origin=package`

The convergence point should happen before stages where divergence is currently causing correctness issues.

### A. Unified Compilation Unit
Define one `CompilationUnit`-style model containing, at minimum:
- `mir_funcs`
- `ssa_funcs`
- `fn_infos`
- `type_table`
- `entry_id`
- `rename_map`
- wrapper dependency flags
- per-function origin metadata
- per-function transform-application metadata

Review question:
- Is this enough to express both local and package cases without hidden side channels?

### B. One Canonicalization/Remap Stage
Canonicalization/remap must run exactly once per function per relevant transform class.

Required invariants:
- no stale package TypeIds survive
- no missing-key package TypeIds survive
- no duplicate transform application
- canonicalization of forward nominals is structurally identical regardless of origin

Review question:
- Which transforms belong here versus later semantic/codegen normalization?

### C. One Registration/Resolution Policy
Callable registration and lookup must be shared across origins.

Required semantics:
- deterministic candidate ordering
- normalized receiver identity
- explicit wrapper exclusion policy
- exact concrete vs generic vs wrapper precedence defined in one place
- package-generic behavior only

Review question:
- What is the single source of truth for candidate precedence?

### D. One Reachability/Emission Engine
Reachability must be shared and explicit.

Minimum edge set must include:
- direct calls
- interface/function-ref edges
- destroy/drop-induced edges
- wrapper-induced targets
- entry-wrapper dependency edges or availability flags

Review question:
- Which edges are semantic reachability versus codegen-only emission reachability?

### E. One Codegen Entry Path
There should be one codegen handoff and one wrapper emission policy.

Required semantics:
- wrapper dependency emission keyed off lowered MIR availability
- no signature-only assumptions for emitted symbols
- no undefined internal call targets at IR level
- can_throw shape consistent for local and package-origin calls

Review question:
- Which wrapper dependencies should be explicit inputs versus inferred from MIR?

## Boundary Contracts (Must Be Explicit)
Each contract should have:
- one code-level validator/assertion
- one pinned regression
- one owner location in the pipeline

### 1. Type Remap Completeness Contract
Guarantee:
- no pre-remap package TypeIds survive into host MIR/SSA-visible structures

Expected enforcement:
- post-remap validator
- package universe awareness
- local types included

### 2. Callable Registry Contract
Guarantee:
- no wrapper leakage
- deterministic candidate precedence
- no duplicate semantic candidates due to generic/monomorphized overlap

### 3. Reachability/Emission Completeness Contract
Guarantee:
- every callable internal symbol referenced in IR is either defined or declared as a legitimate runtime/external symbol

### 4. Wrapper/can_throw Contract
Guarantee:
- any boundary/wrapper-induced `can_throw=True` path produces the correct `FnResult` shape
- void-returning intrinsics do not silently capture results unless contractually allowed

### 5. Entry-Wrapper Dependency Contract
Guarantee:
- entry-wrapper helper calls are emitted only when the required dependency is actually lowered/available

Current temporary state to remove in the refactor:
- package-consumer path currently uses a bounded BFS heuristic for entry-wrapper implicit deps (`_K40_MAX_CLOSURE`) to avoid K18-class transitive explosions
- this is acceptable only as a temporary safeguard
- the one-pipeline refactor should replace this with an explicit dependency model so wrapper deps are represented structurally, not discovered via heuristic closure walking

### 6. Visibility/Trait Scope Contract
Guarantee:
- package modules reconstruct enough scope/visibility to preserve package-generic semantics
- no private API leakage
- no stdlib-only behavior

## Test Strategy for Convergence
Two-lane validation is mandatory for every phase:
1. Local lane
2. External package-consumer lane

### Blocking
- existing local smoke remains blocking
- `ext-e2e-smoke` remains blocking

### Reporting
- `ext-e2e-report` is the trend monitor
- failures must be grouped by:
  - compile-check
  - compile-codegen
  - link
  - runtime
  - infra/policy exclusions

### Metrics
Track both:
- raw pass rate
- adjusted pass rate excluding known infra/policy buckets

### Promotion Rule
When a high-risk failure class is fixed, add one representative case to smoke.

### Required Negative Coverage
For package-consumer-specific fixes, add negative regressions where applicable:
- private API still rejected
- missing trait scope still rejected in ordinary user code
- no stdlib-only special casing

## Phased Implementation Shape (For Review)
This sequence is proposed for critique, not treated as final.

### Phase 0: Review and Contract Mapping
Deliverables:
- revised architecture
- explicit contract-to-code-location map
- PR slicing plan

### Phase 1: Shared Skeleton and Parity Instrumentation
Goal:
- establish a unified `CompilationUnit` handoff shape
- add parity/debug comparison mode if needed

Expected output:
- no semantic behavior change intended

### Phase 2: Canonicalization/Remap Consolidation
Goal:
- unify TypeId remap/canonicalization path and transform-application tracking

Key risks:
- hidden package-only structures not covered by validators

### Phase 3: Registration/Resolution Consolidation
Goal:
- one callable registration path
- one precedence policy

Key risks:
- over-pruning valid concrete candidates
- reintroducing wrapper leakage

### Phase 4: Reachability/Emission Consolidation
Goal:
- shared edge extraction and symbol completeness policy

Key risks:
- under-reachability causing undefined symbols
- over-reachability dragging unsupported generic closure into codegen

### Phase 5: Codegen Entry/Wrapper Consolidation
Goal:
- one entry-wrapper dependency policy
- one `can_throw`/`FnResult` contract

Key risks:
- silent shape mismatches in intrinsics/wrappers
- preserving temporary heuristic dependency seeding longer than necessary

### Phase 6: Legacy Branch Removal
Goal:
- delete duplicated legacy branch logic only after previous phases are stable

Key risks:
- removing paths before parity has been demonstrated

## Klaudia's Review — 2026-03-07

### K42 as Structural Motivating Defect

K42 proves the plan is necessary, not aspirational. The concrete defect:

**Two type-check passes for the same source functions.**

1. **Pass 1** (`driftc.py:8296–8338`): iterates `normalized_hirs_by_id`, calls `type_checker.check_function()` using the top-level `callable_registry`, `global_impl_index`, `global_trait_impl_index`, `trait_scope_by_module`, `visible_modules_by_name`. Uses `signatures_by_id_all`. Respects `allow_unsafe` from CLI.

2. **Pass 2** (`compile_stubbed_funcs` at `driftc.py:9044`): rebuilds ALL of the above from scratch at `driftc.py:2766–3070`. Creates a new `CallableRegistry()`, new module_ids, new visibility maps, new trait worlds. Defaults `allow_unsafe=True`.

These two passes see different state:
- **Callable registry**: Pass 1 uses the top-level registry built at ~line 7100–7200. Pass 2 builds a fresh one at line 2766–3069 from `signatures_by_id_all`. Registration ordering and filtering differ → `lock()` resolution succeeds in Pass 1 but fails in Pass 2.
- **Trait world**: Pass 1 builds trait worlds at ~line 8233–8260. Pass 2 rebuilds at line 2798–2870. External trait defs and impl metas are processed differently.
- **Unsafe policy**: Pass 1 uses `allow_unsafe` from CLI args. Pass 2 defaults to `True`.
- **Visibility**: Pass 1 builds module visibility at ~line 8196–8231. Pass 2 builds at line 2768–2786 from `module_deps`.

K42 is not one bug — it's the structural consequence of duplicate pipeline construction. Every state divergence between Pass 1 and Pass 2 is a potential K42 instance.

### Answers to Plan Questions

**1. Where does this draft still leave duplicated logic?**

The biggest gap: the plan doesn't call out that the duplicate type-check IS the problem. The plan discusses "unified CompilationUnit" but doesn't address that `compile_stubbed_funcs` re-does type-checking. The fix is not just unifying MIR/SSA structures — it's eliminating the second type-check entirely by passing typed results from Pass 1 into MIR lowering.

Concrete locations of duplicated logic:
| Logic | Pass 1 location | Pass 2 location |
|-------|----------------|-----------------|
| CallableRegistry construction | `driftc.py:7100–7200` | `driftc.py:2766–3069` |
| GlobalImplIndex construction | `driftc.py:8233–8240` | `driftc.py:2880–2920` |
| GlobalTraitImplIndex construction | `driftc.py:8248–8260` | `driftc.py:2920–2960` |
| trait_scope_by_module | `driftc.py:8261–8269` | `driftc.py:3000–3020` |
| visible_modules_by_name | `driftc.py:8196–8231` | `driftc.py:2768–2786` |
| TypeChecker instantiation | `driftc.py:8295` (implicit) | `driftc.py:2765` |
| Unsafe policy | CLI-driven | Hardcoded `True` |

**2. Which contract is currently weakest?**

**Callable Registry Contract (#2)**. It's the most state-dependent and the direct cause of K42. Registry construction at line 2766–3069 iterates `signatures_by_id` and registers methods/functions. The iteration order, wrapper filtering, and generic vs monomorphized precedence all differ from the top-level registry. This contract should be implemented first because:
- It directly blocks 4 tests (lock auto-borrow)
- It's the most likely source of future K42 instances
- It demonstrates whether the unification approach works

**3. Which phase ordering is wrong or too risky?**

Phase 3 (Registration/Resolution) should come BEFORE Phase 2 (Canonicalization/Remap). Reason: the callable registry divergence (K42) is the active blocker at 96.4%. TypeId remap (Phase 2) was the blocker at 77% but is now largely resolved. The current risk ordering should follow the current failure surface.

Revised order:
1. Phase 0: Review and contract mapping (this document)
2. Phase 1: Eliminate the second type-check (the core K42 fix)
3. Phase 2: Registration/Resolution consolidation (callable registry)
4. Phase 3: Canonicalization/Remap consolidation
5. Phase 4: Reachability/Emission consolidation
6. Phase 5: Codegen Entry/Wrapper consolidation
7. Phase 6: Legacy branch removal

**4. Which phases should be split?**

Phase 1 ("Eliminate the second type-check") is the highest-risk change and should be split:
- **1a**: Pass the `typed_fns` dict from Pass 1 into `compile_stubbed_funcs`. Add a `skip_typecheck` parameter. When `skip_typecheck=True` AND `typed_fns` is provided, `compile_stubbed_funcs` uses the pre-typed results instead of re-running type-check. This is a pure bypass — no deletion of old code yet.
- **1b**: Pass the `callable_registry`, `global_impl_index`, `trait_scope_by_module`, etc. from Pass 1 into `compile_stubbed_funcs` as optional overrides. When provided, skip reconstruction.
- **1c**: Remove the old reconstruction code in `compile_stubbed_funcs` once 1a+1b are stable.

**5. What invariants are missing?**

- **No-double-typecheck invariant**: Each source function must be type-checked exactly once. Currently violated.
- **Unsafe propagation invariant**: `allow_unsafe` must flow from CLI through all compilation stages. Currently violated by `compile_stubbed_funcs` default.
- **Copy status consistency**: `copy_status()` for a given TypeId must return the same result across all compilation stages. Currently violated (MIR array copy invariant failures).
- **Result type inference consistency**: Generic variant type inference (e.g., `Result<T, Int>`) must resolve identically across passes. Currently violated.

**6. Which K-class regressions are not covered?**

- **K42 itself** is not explicitly a contract in the plan. Add: "No source function may be type-checked by more than one TypeChecker instance."
- The plan doesn't address the `result_generic_ok_copy_struct_string_match_return_no_leak` failure — generic variant type inference in the package path. This might be a K37-adjacent issue.

**7. Where is stdlib-specific behavior risk?**

- `_K40_MAX_CLOSURE = 64` in bounded BFS — this constant was tuned for the std stdlib's preamble. A user package with a larger preamble would be silently pruned.
- K25 temporary fallback (all traits/all modules for external modules) — currently package-generic but too broad. Plan mentions this correctly.
- The `unsafe_trusted_modules` fix (trust all source modules when `--allow-unsafe`) is package-generic. Correct.

**8. Temporary fallbacks to accept:**

| Fallback | Removal trigger | Removal target |
|----------|----------------|----------------|
| K25 all-traits-scope | DMIR v1 serializes trait_scope | Phase 3 or later |
| K40 bounded BFS (64) | Structural entry-wrapper dependency model | Phase 5 |
| K42 allow_unsafe=True default | Phase 1b (pass CLI unsafe from top-level) | Phase 1b |

**9. Temporary vs permanent parity checks:**

- **Permanent**: TypeId remap completeness validator, function reachability/emission completeness check, FnResult shape validator
- **Temporary**: Debug comparison mode (if Phase 1a adds typed_fns bypass, temporarily run BOTH paths and assert same diagnostics — remove after Phase 1c)

**10. Replacing bounded-BFS wrapper dependency seeding:**

The structural model: entry wrapper implicit deps should be declared as metadata on the entry wrapper itself, not discovered via MIR closure walking.

Concrete proposal:
- `ENTRY_WRAPPER_IMPLICIT_DEPS` already declares the deps. Instead of BFS-walking their transitive closure, emit them as `declare` stubs in the LLVM module and let the linker resolve them from the package's pre-compiled object (when we have object-level package linking). For now, the bounded BFS is acceptable because the preamble is small and static.

### Revised Execution Order

| Phase | Scope | Risk | Rollback | Acceptance |
|-------|-------|------|----------|------------|
| 0 | This review | None | N/A | Agreement on plan |
| 1a | Add `skip_typecheck` + `typed_fns` parameter to `compile_stubbed_funcs`; pass from top-level | Medium | Revert parameter; both paths still work | All existing tests pass; K42 unsafe test (`array_byte_alloc_uninit_requires_unsafe`) now passes |
| 1b | Pass `callable_registry` + trait indexes as overrides to `compile_stubbed_funcs` | High | Revert overrides | K42 lock tests pass (+4); ext-e2e-smoke stable |
| 1c | Remove old reconstruction code from `compile_stubbed_funcs` | Low (after 1a+1b) | Revert deletion | Same test results as 1b |
| 2 | Registration/Resolution: single callable_registry, wrapper exclusion, precedence policy | Medium | Revert registration changes | No wrapper leakage; deterministic candidate ordering |
| 3 | Canonicalization/Remap: TypeId remap validator, forward nominal consistency | Low | Revert validator | Post-remap assertion passes for all package types |
| 4 | Reachability/Emission: shared edge extraction | Medium | Revert edge changes | No undefined symbols at link time |
| 5 | Codegen Entry/Wrapper: structural dep model, can_throw contract | Medium | Revert to bounded BFS | Entry wrapper tests stable |
| 6 | Legacy branch removal | Low (after all above) | Revert deletions | All tests green on both lanes |

### Test Matrix Per Phase

Each phase must pass both lanes:
- **Local lane**: `just test-e2e` + `just mir-codegen` + `just lang-codegen-test`
- **Package lane**: `just ext-e2e-smoke` + full `pkg_consumer_runner.py` report

Phase 1a acceptance: K42 unsafe test passes → total 539/558 (96.6%)
Phase 1b acceptance: K42 lock tests pass → total 543/558 (97.3%)
Phase 1c acceptance: same as 1b, reduced code

## Go/No-Go Gate Before Coding
No code work starts until all are true:
1. Plan reviewed and revised by Klaudia — **DONE (this section)**
2. Open architectural questions resolved — **DONE**
3. Phase order agreed — **Proposed above; needs owner confirmation**
4. Contract checks mapped to concrete locations — **Mapped in Answer #1**
5. Smoke/report expectations agreed — **Proposed in test matrix**
6. Package-generic behavior rule explicitly confirmed — **Confirmed: all fixes must be package-generic**

## Success Criteria (For Eventual Implementation)
- substantial reduction in local/package divergence points
- no new regressions in pinned K-series tests
- improved package-consumer pass trend with stable smoke
- downstream is no longer the primary bug discovery path
- package-consumer fixes are package-generic, not stdlib-specific
- **no source function type-checked more than once** (K42 invariant)
