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

## Questions Klaudia Should Answer
Please answer concretely, with module/function references where possible.

1. Where does this draft still leave duplicated logic?
2. Which contract is currently weakest and should be implemented first?
3. Which phase ordering is wrong or too risky?
4. Which phases should be split into smaller PRs?
5. What invariants are missing?
6. Which known K-class regressions are not covered by this plan?
7. Where is there risk of stdlib-specific behavior instead of package-generic behavior?
8. Which temporary fallbacks should be accepted, and what is the concrete removal trigger for each?
9. Which parity checks should exist only temporarily during migration versus permanently?
10. What should replace the current bounded-BFS entry-wrapper dependency heuristic so K40-class fixes become structural rather than heuristic?

## Deliverable Requested From Klaudia
Klaudia should return an enhanced version of this plan with:
- corrected architecture text
- explicit module/function touchpoints
- revised phase ordering if needed
- risk table per phase
- rollback strategy per phase
- acceptance criteria per phase
- test matrix per phase (local + package lanes)
- list of temporary fallbacks, if any, with removal plan
- explicit proposal for replacing bounded-BFS wrapper dependency seeding with a structural model

## Go/No-Go Gate Before Coding
No code work starts until all are true:
1. Plan reviewed and revised by Klaudia
2. Open architectural questions resolved
3. Phase order agreed
4. Contract checks mapped to concrete locations
5. Smoke/report expectations agreed
6. Package-generic behavior rule explicitly confirmed

## Success Criteria (For Eventual Implementation)
- substantial reduction in local/package divergence points
- no new regressions in pinned K-series tests
- improved package-consumer pass trend with stable smoke
- downstream is no longer the primary bug discovery path
- package-consumer fixes are package-generic, not stdlib-specific
