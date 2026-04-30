# String Ownership → `ConstArc<T>` — Plan

**Status:** planning / pre-branch. Not an implementation request. This
document is the alignment artifact between user and assistant on what
the architectural target is, what the phases are, and how they gate
each other.

**Working directory:** `work/string-ownership/` (ephemeral per the
"work/ is ephemeral" repo discipline — nothing in source/stdlib/tests
imports from this tree). When the track lands, the audit + matrix +
ConstArc spec move to `docs/` as durable references; everything else
under `work/` gets cleaned at branch close.

**Branch (when started):** suggested name
`feature/const-arc-string-migration`. Not opened yet.

---

## Architectural target

```drift
pub struct String {
    data: ConstArc<StringBytes>
}

pub struct JsonHandle {
    data: ConstArc<JsonNode>
}

pub struct FrozenGraph {
    data: ConstArc<GraphData>
}
```

**One reusable immutable shared owner.** String uses the same ownership
model as user code. Compiler intrinsics construct normal values, not
values with special lifetime laws. Copy / drop / move behavior flows
through normal struct/field ownership. `string_arc.py` becomes
migration scaffolding, not the architecture.

---

## TL;DR

Drift's String ownership / refcount behavior is encoded as scattered
TypeId-special-cases across the compiler — the recurring symptom that
every new MIR op or ownership pass has to remember "and add the String
handler." The standing repo memory
`feedback_no_string_special_case_in_generic_lowering.md` already names
this smell.

**The internal-helper consolidation is symptom treatment, not cause
treatment.** Cause treatment: stop making String a primitive. Express
it as a struct with a `ConstArc<StringBytes>` field, and the
special-casing dissolves into normal struct/field ownership.

The track is structured **audit-first / primitive-second / migrate-third**:

1. **Phase 1** — audit + matrix. Pure documentation. Output is the
   regression-net map and the scope-defining findings (SSO? literals?
   FFI? ABI bump?) for phase 3.
2. **Phase 2** — design and ship `ConstArc<T>` as a library/runtime
   primitive, adopt it on JsonHandle to validate the shape **before**
   String depends on it.
3. **Phase 2.5 (optional)** — interim helper for scattered-gate
   containment. Skipped if phase 2 moves fast. **Explicit sunset
   clause: retires when phase 3 lands.**
4. **Phase 3** — migrate String to `struct String { data:
   ConstArc<StringBytes> }`. ABI bump, spec update, package
   re-emission, literal lowering, FFI, SSO decision. The big phase.
5. **Phase 4** — retire `string_arc.py`.

The internal helper is **explicitly not the destination.** It can
exist only as bounded scaffolding. The destination is `ConstArc<T>`.

---

## User-visible payoff

This is the language-design-level argument. Once `ConstArc<T>` lands
and adopters migrate, the `share` keyword stops conflating two
distinct operations:

- **"I'm choosing aliasing because mutations through aliases will be
  observable."** This stays on `share x` / `Share` trait, warning-bearing,
  for `Arc<Mutex<T>>`-style mutable-shared types and explicit
  closure-capture sharing of mutable owners (`captures(share app)`).
- **"I'm taking another cheap stake of immutable data."** This becomes
  implicit copy for `ConstArc<T>` adopters. `val b = a;` works.

UX deltas:

| Scenario | Today | Post-migration |
|---|---|---|
| `var s: String; val b = s;` | works (implicit copy via primitive) | works (implicit copy via ConstArc) — **no source change** |
| `var h: JsonHandle; val b = h;` | **moves**; user must call `h.clone()` | **works** — implicit O(1) refcount inc |
| Frozen map / interned symbol / frozen graph (future types) | doesn't exist, or requires `.clone()` ceremony | `val b = a;` works on day one |
| `var app: Arc<AppState>; val b = share app;` | required (`Share` is warning-bearing) | **unchanged** — `share` keeps its semantic for genuinely-aliasing-matters types |

**The user-visible win, summarized.** Today writing
`val b = share a;` is the only way to take another stake of an
`Arc`-backed immutable thing. Post-migration, immutable shared values
use `val b = a;` directly, and `share` is reserved for the cases that
actually need the warning. The language becomes more honest about what
each operation means.

This is the cleanest single argument for `ConstArc<T>` as the target.
Internal-helper-only tracks cannot deliver this UX win; only the
representation change (String becomes a normal struct, JsonHandle
migrates from `Arc` to `ConstArc`) does.

### Surrounding-syntax implications

ConstArc's existence reshapes the meaning of adjacent `share`-family
syntax. These corollaries are placeholders here; the durable text
goes into `docs/const_arc_spec.md` (phase 2 deliverable).

**1. The `share` keyword's semantic gets *sharper*, not weaker.**
Today users write `share x` mechanically whenever the type is
`Arc<T>`-backed, regardless of whether the underlying value is
genuinely mutable-shared (e.g., `Arc<Mutex<AppState>>`) or just
immutable (e.g., `Arc<JsonNode>` inside JsonHandle). The keyword
covers both cases identically. Post-migration:

- ConstArc-backed types: `val b = a;` (implicit copy). `share x`
  doesn't apply — they're not on the `Share` trait.
- Genuinely-aliasing-matters types: `share x` is *required* (Drift's
  `Share` keeps its warning-bearing contract). The warning regains
  its informational value because users who reflexively wrote
  `share` on every Arc will write it less often, and the times they
  do will be the times they should.

The language becomes more honest: the keyword now names the
operation it's actually for.

**2. `captures(share x)` closure syntax keeps working unchanged for
the cases that need it.** That syntax targets explicit sharing of
mutable owners across closure boundaries — exactly the
genuinely-aliasing-matters case (web-rest's `captures(share app)` for
the middleware `AppState` shared across handler closures, etc.).
After ConstArc lands:

- `captures(share x)` continues to work for `Arc<Mutex<T>>`-style
  mutable owners. **No change.**
- ConstArc-backed types in closure captures use plain
  `captures(copy x)` instead. The closure environment gets its own
  ConstArc stake via implicit refcount inc; aliasing is unobservable
  by definition, so no `share` ceremony is needed.

Same UX delta as for assignment: explicit `share` for mutable
owners, implicit copy for ConstArc.

**3. `share x` is permitted on `ConstArc<T>` as a stylistic synonym
for implicit copy. No deprecation, no warning, no migration break.**
Existing user code that mechanically wrote `share a` on Arc-backed
types keeps working unchanged after migration; the type system handles
the dispatch:

- `share x` where `T: Share` → `Share::share()` call, warning-bearing
  per `shareable.drift`'s contract.
- `share x` where `T` is `ConstArc<U>`-bearing (e.g., `String`,
  migrated `JsonHandle`) → equivalent to implicit copy, no warning.

The warning's informational value is preserved without requiring
syntactic distinction: the trait the type satisfies determines the
warning, not the keyword. Readers hover-type to know which case
they're in. Users who prefer making "this is a refcount inc" visible
at the call site can keep writing `share x` for ConstArc; users who
prefer terseness write `val b = a;`. Both lower identically.

This parallels the existing `copy x` expression form — `copy x`
works for any `Copy` type. Permitting `share x` for any
take-another-stake-bearing type (`Share` *or* `ConstArc`-bearing) is
the principled extension. Phase 3 ships zero deprecation runbook for
`share`-keyword usage.

---

## Decision summary (2026-04-30)

| Question | Decision |
|---|---|
| Architectural target? | **`ConstArc<T>` as a reusable language/library primitive.** Both String and user-defined immutable shared values use it the same way. |
| Public `ConstShare` trait? | **No, not as a separate destination.** `ConstArc<T>` covers the surface ConstShare would have covered. Trait-vs-type discussion can re-open *only* if a future user wants to write polymorphic code over multiple immutable-shared types. |
| Internal String ownership helper as durable architecture? | **No.** Helper is scaffolding only, with explicit sunset when phase 3 lands. |
| Migrate String onto `ConstArc<StringBytes>`? | **Yes** — that's the point of the track. Phase 3. |
| Adopt `ConstArc<T>` on a user-side type first? | **Yes** — `JsonHandle` migrates from `Arc<JsonNode>` to `ConstArc<JsonNode>` in phase 2 to validate the primitive's shape *before* String depends on it. |
| Phasing? | **Audit-first → primitive-second → migrate-third.** Acceptance gates between phases. |
| Bundling with hotfixes? | **No.** Own branch, own spec doc. |

---

## What `ConstArc<T>` is (design seed — phase 2 finalizes)

**Surface contract:**

- Immutable shared owner of `T`.
- Implicit, cheap copy: `var b = a;` produces another owner via
  refcount inc, no warning, no `.clone()` ceremony. (This is the
  Copy-like surface that makes ConstArc *not* `Share` —
  `std.core.shareable.Share`'s `share()` is warning-bearing because
  aliasing observability matters; `ConstArc` aliasing is by definition
  unobservable.)
- Drop releases the refcount; backing storage freed at zero.
- Read-only access to `T` — no `&mut T` accessor exists. Mutability
  through a `ConstArc<T>` field is a compile error.
- Thread-safe by construction: refcount is atomic; `T` is immutable
  so cross-thread access is sound *if* `T`'s reads are sound.
- No identity comparison through `ConstArc<T>` — aliasing
  unobservability means two `ConstArc<T>` values that point to the
  same backing must NOT be distinguishable from two pointing to
  separate backings with equal contents. (Same `==` answer, no
  reflection / address-of leakage.)

**T constraints (open — phase 2 settles):**

Candidates for what `T: ConstArc-able` requires:
- **Drop ok** — for refcount-zero release. (Standard.)
- **Frozen / Immutable** — `T` must have no public `&mut`-taking
  methods, OR a `Frozen` marker trait. Without this, `T` could be
  mutated through `Arc<Mutex<T>>`-style indirection inside, breaking
  the "aliasing unobservable" invariant.
- **Sendable / Sync** — for cross-thread soundness. May fall out of
  Frozen + thread-safe-refcount automatically.

The exact set is design surface for phase 2; phase 1 audit informs by
telling us which of these constraints String currently satisfies and
which it would have to be re-shaped to satisfy.

**How `ConstArc<T>` differs from `Arc<T>`:**

| | `Arc<T>` (today) | `ConstArc<T>` (target) |
|---|---|---|
| Trait family | `Share` (warning-bearing) | Copy-like (warning-free) |
| Mutation through alias | Permitted via `Arc<Mutex<T>>` etc. — aliasing is *expected* to matter | **Forbidden** by construction — aliasing is unobservable |
| User surface | Explicit `.clone()` | Implicit copy |
| `T` constraints | None beyond `Drop` | `Frozen` / `Immutable` (TBD) |
| Existing examples | `concurrent.Arc<JsonNode>` (today's JsonHandle), web-rest middleware app sharing | None yet — defined in this track |

**Implementation strategy (design hypothesis — phase 2 validates):**

Cheapest path is `ConstArc<T>` as a thin wrapper around the existing
`Arc<T>` runtime layout, with the additional invariants enforced at
the trait/type level:

- Same refcount layout as `Arc<T>` — bit-compatible at runtime.
- New `Frozen` (or equivalent) marker trait that `T` must implement.
- Compiler refuses `&mut T` accessors on `ConstArc<T>`.
- `Copy` is *implicit* for `ConstArc<T>` (or whatever Drift's
  equivalent of "auto-clone for ref-count types" lowers to today —
  this is exactly the question phase 2 answers).
- Static-backing variant for literals (see "literals" below).

This hypothesis avoids duplicating Arc's machinery. It might not
survive contact with reality — phase 2's first deliverable is to
either confirm or refute it.

---

## Two ConstShare lowering models — does the architectural target collapse them?

The earlier JsonHandle analysis identified two distinct lowering
models for ConstShare-shaped types:

- **MIR-primitive** (String today): refcount lives at MIR/codegen level.
- **User-composed-via-Arc** (JsonHandle today): refcount lives at
  library level via `Arc<T>::clone`.

**Yes — the architectural target collapses these into one.** Post-phase-3,
both String and JsonHandle are composed-via-ConstArc:

- String: `struct String { data: ConstArc<StringBytes> }`
- JsonHandle: `struct JsonHandle { data: ConstArc<JsonNode> }`

There is no longer a "MIR-primitive" lowering model in user-visible
types. `string_arc.py`'s scattered TypeId-gates dissolve because
String stops being a primitive at MIR level. The compiler emits
normal struct field ownership; ConstArc's `clone`/`drop` lowering
covers the refcount mechanics generically.

This is the clearest single argument for `ConstArc<T>` as the target.
The internal-helper-only path *cannot* collapse the two models —
it just centralizes the MIR-primitive special-casing. Only the
representation change (String becomes a struct) does.

---

## What "a String gate" means (for the phase-1 audit)

A naive `grep '== string_ty'` misses real shape. The audit's
inventory item is:

> Any code path that branches on String-ness at HIR or MIR level, OR
> that emits a String-specific runtime helper call (StringRetain,
> StringRelease, StringCopy, etc.).

Concretely, an inventory entry covers:

1. Direct TypeId equality: `ty == string_ty`, `ty == ensure_string()`.
2. Kind / shape checks: `td.kind is TypeKind.STRING_*` (if any).
3. Whole-pass specialization: `string_arc.py` walks every function
   and bakes String semantics throughout. The whole pass counts as
   one inventory entry, broken down by sub-region.
4. Runtime-helper call sites: anywhere a `StringRetain` /
   `StringRelease` / `StringCopy` op is emitted, or a corresponding
   LLVM call is lowered.
5. Ownership-query branches: `_query_copy`, `has_drop`,
   `_compute_drop_policy`, `verdict_at` consultation paths that
   branch on String-ness internally.
6. Storage / boundary integrations: package-boundary serialization,
   FFI shape, generic-instantiation paths that special-case String.

Each entry's eventual fate post-phase-3:

- (a) **Dissolves entirely** because String becomes a normal struct
  and the gate's question is answered by generic struct/field
  ownership.
- (b) **Migrates to ConstArc lowering** — the gate's question is
  about refcounted storage, which ConstArc handles generically.
- (c) **Stays as a String-specific concern** — e.g., literal
  encoding, FFI byte-buffer interop. These are real String concerns
  not refcount concerns; they don't go away.

Phase-1 inventory tags each entry with its expected fate. That's the
load-bearing measurement for "what does the migration actually do?"

---

## Phase plan

### Phase 0 — alignment (this document)

**Status:** in progress / under review.

**Deliverable:** this plan, reviewed by user. No compiler change, no
new files outside `work/`.

**Exit gate:** user approves the architectural target (`ConstArc<T>`),
the phasing, and the sunset clause on the optional helper.

### Phase 1 — audit + matrix + scope-defining findings (~3–4 days, zero compiler change)

**Deliverable:** documentation artifacts that scope phase 3.

1. **`docs/string_ownership_inventory.md`** — every gate per the
   six-pattern definition above. Schema per entry:

   ```
   ### <short title>
   - **File:line:** <path>:<lineno>
   - **Kind:** [direct-typeid | kind-shape | whole-pass | runtime-helper |
     ownership-query | boundary]
   - **Question answered:** [copy_means_retain | move_transfers_stake |
     drop_releases | read_from_storage_returns_stake |
     overwrite_needs_release | other-with-explanation]
   - **Current behavior:** one-line description.
   - **Expected post-phase-3 fate:** [(a) dissolves | (b) migrates to
     generic ConstArc lowering | (c) stays String-specific].
   - **Migration risk:** [low | medium | high] + one-line reason.
   - **Test coverage:** test paths that exercise this gate (or "GAP").
   ```

   Total entry count is itself a metric. Distribution across (a) /
   (b) / (c) tells us the shape of phase 3.

2. **`docs/string_ownership_matrix.md`** — String × storage-shape ×
   ownership-op coverage matrix. Columns: Copy, Move, Clone, Drop,
   Overwrite. Rows: bare local, RawBuffer<String> slot, Array<String>
   elem, TypeBox payload, struct field, variant payload,
   Optional<String>, Result<String,_> ok, callback param, closure
   capture, package-boundary in/out. Cells are `(behavior, test)`;
   "GAP" if no coverage.

3. **`docs/string_ownership_inventory.md` Appendix A —
   candidate-adopter analysis.** Worked example for `JsonHandle`
   (covered separately below); stub rows for "interned symbols",
   "frozen graph / DAG", "frozen map / set", "immutable bytes blob"
   describing their likely shape and whether they'd benefit from
   ConstArc directly.

4. **`docs/string_ownership_inventory.md` Appendix B —
   scope-defining findings for phase 3.** Explicit answers to:

   - **Literals.** How do `"hello"`-style String literals survive
     migration? Static-backing variant of `ConstArc<T>` (i.e.,
     ConstArc carries a static/heap discriminator)? Stay primitive
     and convert at boundary? Decision shapes ConstArc's runtime
     layout.
   - **SSO (small-string optimization).** Is inline storage for short
     strings in scope or out? If in scope, ConstArc<StringBytes> has
     to either support inline-vs-heap, or StringBytes encodes it,
     or we eat the perf regression. Phase 1 records the current
     measurement (does Drift's String have SSO today?) and the
     decision.
   - **FFI.** Current `extern "C"` String layout. Post-migration
     plan: keep current shape via runtime-side compatibility, or
     bump FFI surface (downstream impact)?
   - **Package format / ABI.** String currently encodes as a
     primitive in `.dmp`. Post-migration: struct-with-ConstArc-field.
     Phase 1 names the ABI bump as a phase 3 deliverable; phase 1
     does not ABI-bump.

5. **Memcheck carriers** for any matrix cell currently lacking
   coverage. New tests under `lang/tests/memcheck/` only. No source
   changes outside the test tree.

**Exit gate:**

- Inventory document covers every gate matching the six-pattern
  definition. Spot-check via `grep` (`ensure_string()`, `string_ty`,
  `StringRetain`, `StringRelease`, `string_arc`) reveals every hit
  has an entry or is explicitly under a whole-pass entry.
- Matrix has zero `GAP` cells.
- Appendix B answers all four scope-defining questions with
  decisions, not "TBD."
- All new memcheck carriers green on `pytest -n16 lang/tests/memcheck/`.
- `git diff main` for the phase-1 commit touches only `docs/` and
  `lang/tests/memcheck/`.

### Phase 2 — `ConstArc<T>` design + JsonHandle adoption (~5–7 days)

**Goal:** ship `ConstArc<T>` as a real, used primitive **before**
String migrates onto it. Validate the trait shape against an actual
adopter (JsonHandle) so that String's needs in phase 3 don't
retroactively reshape the primitive.

**Deliverable:**

1. **`docs/const_arc_spec.md`** — the durable spec. Surface contract,
   T constraints, runtime layout, refcount semantics,
   literal/static-backing handling, FFI plan, package format
   representation. References phase-1 Appendix B for scope-defining
   inputs.

2. **`stdlib/std/concurrent/const_arc.drift`** (or equivalent
   placement) — `ConstArc<T>` as a usable library type, with whatever
   compiler support is needed to enforce the surface invariants
   (Frozen marker, refusal of `&mut T`, implicit copy, etc.).

3. **JsonHandle migration** from `Arc<JsonNode>` to
   `ConstArc<JsonNode>`. This is the validation — the migration must
   land cleanly, all existing JsonHandle tests must pass, memcheck
   must be green, and `.dmp` round-trip for `JsonHandle`-bearing
   packages must work.

4. **New memcheck carriers** for `ConstArc<T>` over a synthetic test
   type with a destructor (the regression net for the phase-3
   migration).

5. **No String change.** String stays primitive in phase 2.

**Exit gate (load-bearing):**

- `ConstArc<T>` spec doc reviewed and merged.
- JsonHandle migrated; all JsonHandle tests + memcheck green; `.dmp`
  determinism check for a JsonHandle-bearing package passes.
- Compiler-enforced invariants tested: `&mut T` accessor on
  `ConstArc<T>` rejected with a clean diagnostic; non-Frozen `T`
  rejected at instantiation.
- ConstArc's design hypothesis (thin wrapper over Arc layout) either
  confirmed or replaced with a concrete alternative.
- ABI bump only if `ConstArc<T>` adds a runtime helper signature
  (likely yes — separate retain/release entry points or a typeclass
  vtable hook). Verified via `lang/tests/driver/test_abi_version_stamp.py`.

### Phase 2.5 (optional) — interim helper

**This phase is skipped by default.** It runs only if phase 1
findings or phase 2 progress suggest interim consolidation
materially de-risks phase 3.

**If it runs, what it is:** an internal helper module
(`lang/driftc/string_ownership.py` or wherever fits) with predicates
that consolidate scattered String gates under one roof. **Implementation
contract:**

> The helper exists only as scaffolding for phase 3. Its
> implementation lifetime is bounded — the file is deleted when
> phase 3 lands. **Do not** add features or generalize the helper
> beyond what phase 3 migration directly consumes.

**Sunset clause (mandatory):** the file's module docstring names
phase 3 as the trigger for deletion. If phase 3 is delayed beyond
some agreed budget (3 months from phase 2.5 start, say), the helper
is reviewed for retirement regardless — "permanent internal
abstraction" is not an acceptable outcome.

**Exit gate (if phase runs):** scattered-gate count reduced; full
test gate + memcheck + `.dmp` determinism green; sunset clause
visible in module docstring.

### Phase 3 — String representation migration (~2–4 weeks, the big phase)

**This is the destination.** ABI bump. Spec update. Package
re-emission. Literal lowering. FFI. SSO decision (per phase-1
Appendix B).

**Deliverable:**

1. **`stdlib/std/string/string.drift`** (or equivalent) — String
   redefined as `pub struct String { data: ConstArc<StringBytes> }`.
   `StringBytes` defined as the byte-content type (likely a
   primitive byte buffer, design TBD in phase 1 Appendix B).
2. **Compiler intrinsics narrowed.** Only construction / literal
   lowering / runtime interop remain. Copy / drop / move flow
   through normal struct field ownership.
3. **`string_arc.py` shrinks dramatically.** What remains is generic
   ConstArc-aware code that should logically live elsewhere —
   `const_arc_lowering.py` or in the generic ownership layer.
   Phase 4 cleans up the residual.
4. **ABI bump.** Compiler/runtime boundary changes:
   String layout, runtime helper signatures.
   `DRIFT_RT_ABI_VERSION` bumped per `AGENTS.md` § "Boundary Contract
   Guardrails."
5. **Package re-emission story.** Existing `.dmp` files don't survive
   this change. Path: orch certify-lane re-stage of all stdlib
   packages; downstream consumers re-emit on next deploy. Phase 3
   delivers a migration runbook for downstream.
6. **Literal lowering.** Per phase-1 Appendix B's decision —
   either `ConstArc::from_static(...)` or boundary-converted from
   primitive.
7. **FFI compatibility.** Per phase-1 Appendix B — either
   runtime-side compatibility or downstream FFI consumer migration.
8. **SSO decision implemented.** Per phase-1 Appendix B.
9. **Spec doc update** — language reference reflects
   `String = ConstArc<StringBytes>`.
10. **Phase 2.5 helper retired** (if it ran).

**Exit gate:**

- Full driver/e2e/memcheck gate green.
- `.dmp` byte-determinism for newly-emitted packages.
- Source-rebuild trust gate (orch certify lane) passes.
- ABI version stamp regression updated.
- All inventory entries from phase 1 marked as one of (a) dissolved /
  (b) migrated / (c) stays — distribution matches phase-1 expected
  fates within reasonable tolerance.
- Downstream package re-emission runbook reviewed.

### Phase 4 — retire `string_arc.py` (~1–2 days, cleanup)

**Goal:** delete or rename `string_arc.py`.

If phase 3 went clean, what's left is generic ConstArc-aware code
(retain/release lowering, drop-flag interaction, etc.). Move to
`const_arc_lowering.py` (or appropriate generic location). Delete
`string_arc.py`.

**Exit gate:** `string_arc.py` no longer exists; tests still green;
no diagnostic mentions String-specific ownership pass.

---

## Acceptance criteria — full-track close-out

The track wraps when **all** of these are true:

1. `ConstArc<T>` exists as a documented, tested, used (JsonHandle +
   String) primitive.
2. `String` is defined as `struct String { data: ConstArc<StringBytes> }`
   in source.
3. `string_arc.py` is deleted or shrunk to a generic
   `const_arc_lowering.py` containing zero String-specific gates.
4. Compiler intrinsics for String are narrowed to construction /
   literal / runtime interop only.
5. `docs/string_ownership_inventory.md` and
   `docs/string_ownership_matrix.md` exist as durable references and
   their entries' "expected fate" tags are validated by phase 3's
   actual outcome.
6. Memcheck carriers cover every matrix cell.
7. ABI version bumped (phase 3) with mismatch regression updated.
8. Package re-emission runbook executed for stdlib; downstream
   migration documented.
9. `docs/const_arc_spec.md` is the durable spec.

---

## Stop-and-escalate triggers (during the track)

Halt and consult before proceeding if any of these fire:

- **`.dmp` determinism breaks before phase 3.** Stop. The change is
  not internal-only — it touched serialization shape. Either revert
  or rescope.
- **`ConstArc<T>` design hypothesis (Arc-wrapper) refutes during
  phase 2.** Stop. Phase 2 needs to settle the concrete alternative
  before JsonHandle adoption can land.
- **JsonHandle adoption (phase 2) reveals a missing trait API
  surface.** Stop. Add to spec; don't paper over with String-only
  ergonomics.
- **Phase 3 migration touches a phase-1 inventory entry tagged "(c)
  stays String-specific" but the migration would dissolve it.**
  That's a finding inversion — stop and reconcile the inventory's
  "expected fate" with reality.
- **Memcheck regression in any cell.** Stop. The matrix's job is to
  catch this. Fix the matrix first, then the migration.

---

## Out of scope (will not do in this track)

- A separate public `ConstShare` trait. `ConstArc<T>` covers the
  surface; trait-vs-type re-opens only if a future user wants
  polymorphic code over multiple immutable-shared types.
- Touching `Arc<T>`'s existing surface or semantics. `ConstArc<T>`
  is additive; `Arc<T>` keeps its `Share`-based warning-bearing
  contract for the cases that need it.
- Frozen-collection types (frozen map/set/graph) as part of *this*
  track. They become candidates *after* `ConstArc<T>` ships; their
  migrations are separate tracks.
- `Mutex<T>` / interior mutability redesign. Out of scope.
- Borrow-checker walker consolidation (separate refactor-trigger
  entry).

---

## Candidate-adopter analysis: JsonHandle (worked example)

This is the seed for Appendix A of the phase-1 inventory document.
Captured here because JsonHandle is the validation adopter for
phase 2.

**What is JsonHandle today?**
(`stdlib/std/json/json.drift:1458-1525`)

| Dimension | Today |
|---|---|
| Runtime-backed handle? | No — pure user-level type |
| Arena pointer? | No |
| Refcounted? | Yes — via `concurrent.Arc<JsonNode>` field |
| Borrowed view? | No — owns an Arc stake |
| Mutable cursor? | No — no mutating methods on `JsonHandle` |
| Thread-safe? | Yes — `concurrent.Arc` is thread-safe |
| User-facing surface | Explicit `.clone()`; `share(node)` constructor; borrowing accessors |

Definition: `pub struct JsonHandle { data: concurrent.Arc<JsonNode> }`.
Compiler treats it as an ordinary struct-with-Arc-field. **No
JsonHandle-specific gates exist anywhere in the compiler.**

**What semantics does JsonHandle want?**

| Dimension | Desired |
|---|---|
| Immutable parsed JSON tree? | Yes |
| Cheap copy via retain? | Yes — O(1) refcount inc |
| Aliases observationally identical to independent copies? | Yes |
| Safe cross-thread? | Yes |
| Drop releases backing storage? | Yes |

JsonHandle wants exactly ConstArc semantics. **Three for three.**

**Migration path (phase 2):**

```diff
-pub struct JsonHandle {
-    data: concurrent.Arc<JsonNode>
-}
+pub struct JsonHandle {
+    data: ConstArc<JsonNode>
+}
```

Plus updating `clone()` and `share()` constructors, plus possibly
narrowing accessors that today use Arc-specific methods.

**What does this migration prove?**

- ConstArc's surface contract is real and usable for at least one
  user-facing immutable-shared type.
- The `Frozen` / `Immutable` constraint on `T` (whatever phase 2
  settles) is satisfiable by `JsonNode`.
- ConstArc's package boundary representation works.
- Memcheck carriers catch reasonable regression shapes.

**What does this migration NOT prove?**

- Literals. JsonHandle has no literals; phase 3 has the harder
  literal problem.
- SSO. JsonHandle has no inline-vs-heap decision; phase 3 owns it.
- FFI. JsonHandle isn't FFI-exposed.

So phase 2 validates the *general* shape; phase 3 still has its own
real surface to traverse. Phase 2 is necessary but not sufficient.

**Stub rows for phase 1 to flesh out:**

- **Interned symbols (hypothetical):** strong adopter candidate
  post-ConstArc. Likely cleanest second adopter.
- **Frozen graph / DAG (hypothetical):** post-ConstArc adopter.
- **Frozen map / set (hypothetical):** post-ConstArc adopter.
- **Immutable bytes blob (hypothetical):** plausibly merges with
  StringBytes if shape aligns. Phase 1 records.

---

## Open questions (to resolve before phase 1 starts)

1. **Branch name.** `feature/const-arc-string-migration` (suggested
   in this plan) or shorter? User call.
2. **Audit tooling.** Pure manual grep, or a small one-shot script
   that enumerates the patterns? If script, lives under
   `work/string-ownership/` and never enters source/stdlib/tests.
3. **Phase-1 review cadence.** Inventory reviewed incrementally
   (subtree at a time) or as one big patch? Lean toward incremental.
4. **Phase 2 adopter choice.** JsonHandle is the natural candidate.
   Alternatives: a fresh `Frozen<T>` test type, an interned-symbols
   prototype. Phase 2 kickoff confirms.
5. **Phase 3 budget.** "2–4 weeks" is the rough estimate; phase 1
   findings tighten it. If phase 1 reveals SSO is required (current
   String has SSO and the perf regression isn't acceptable), budget
   stretches.
6. **Helper file name (phase 2.5, only if it runs).**
   `string_ownership.py` for honesty (one type today) or
   `const_arc_migration_aid.py` to signal scaffolding? Lean toward
   the latter — the name itself enforces the sunset.

---

## Notes on what this plan is NOT

- It is **not** a green light to start phase 1 — that requires user
  approval of this revised plan first.
- It is **not** a commitment to phases 2 / 3 / 4 — those gate on
  prior phase outcomes.
- It is **not** a guarantee of timeline. Estimates are
  rough-order-of-magnitude.
- It is **not** under source/stdlib/tests/tooling. `work/` is
  ephemeral; durable artifacts (inventory, matrix, ConstArc spec,
  language reference update) move to `docs/` as deliverables of
  their respective phases.
- It is **not** "internal helper as the destination." The
  destination is `ConstArc<T>` with String as one of its users. The
  helper is bounded scaffolding with a sunset clause.
