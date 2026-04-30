# String Ownership Normalization — Plan

**Status:** planning / pre-branch. Not an implementation request. This
document is the alignment artifact between user and assistant on what the
track is, what it isn't, and how its phases gate each other.

**Working directory:** `work/string-ownership/` (ephemeral per the
"work/ is ephemeral" repo discipline — nothing in source/stdlib/tests
imports from this tree). When the track lands, the audit + matrix
documents move to `docs/` as durable references; everything else under
`work/` gets cleaned at branch close.

**Branch (when started):** suggested name
`feature/string-ownership-normalization`. Not opened yet.

---

## TL;DR

Drift's String ownership / refcount behavior is encoded as scattered
TypeId-special-cases across the compiler. Every new MIR op or
ownership pass has to remember "and add the String handler"; the
recurring symptom is a one-line fix per occurrence (`M.RawBufferRead`
0.31.24, `M.MoveOut`, `M.PtrRead`, `M.ArrayElemTake`, drop-flags Site 4
strings/arrays residual, TypeBox owning extraction, …). Each fix lands
cleanly; the cost is in the *forgetting*. The standing repo memory
`feedback_no_string_special_case_in_generic_lowering.md` already names
this smell.

The track collapses those scattered gates into one **internal**
ownership capability layer answering five questions: `copy_means_retain`,
`move_transfers_stake`, `drop_releases`, `read_from_storage_returns_stake`,
`overwrite_needs_release`. **No public language surface, no ABI
implication, no package format change, no spec write.** String is the
only adopter today; that's fine for an internal abstraction. The
"three adopters" rule applies to public traits, not to removing a
1700-line failure mode.

The track is structured **audit-first / migrate-second**: phase 1 is a
pure documentation deliverable with zero compiler change. Phases 1.5
and 2 ride on the audit's findings.

---

## Decision summary (2026-04-30)

| Question | Decision |
|---|---|
| Public `ConstShare` trait now? | **No.** One adopter, large migration cost (package boundary, language surface, spec). Wait for second adopter or planned ABI bump that amortizes. |
| Internal String ownership normalization? | **Yes.** Symptom (per-pass forgetting) is recurring; cost (internal-only) is low; reverses cleanly per phase. |
| Single big bang or phased? | **Phased**, audit-first, with explicit acceptance gates between phases. |
| Bundling with hotfixes? | **No.** Own branch, own spec doc. |
| Touching `string_arc.py` instruction-emission logic? | **Out of scope** for phases 1 / 1.5 / 2. Phase 3 territory if at all. |

---

## Why now (and why this shape)

**Forcing function is real.** Per-pass String residuals across recent
work:

- 0.31.9 Phase 4 site-3: strings/arrays return-source remained on legacy
  alias-walk because broad ledger consultation broke
  `test_pkg_map_literal_string_leak` and `test_scope_drop_conditional_move`
  memcheck. Named residual in branch closure memo.
- 0.31.18 replace-store invariant: `_emit_assign_store_ref` had to be
  designed knowing the drop-bearing String case.
- 0.31.24 TypeBox owning extraction: shipped with a *co-landing*
  `M.RawBufferRead` String refcount-stake fix in `string_arc.py`,
  parallel to `M.PtrRead` / `M.ArrayElemTake` / `M.MoveOut` shapes —
  i.e. the same forgetting pattern that motivates this track.

Each fix landed cleanly. None of them needed this refactor to land.
But the cumulative "remember to add a String handler" tax is real and
diffuse.

**Cost shape favors internal.** The killer cost for a public-trait
track is migration: existing `.dmp` files, language surface, spec.
Keeping the abstraction internal eliminates all three. That's the
core insight in this plan.

**Risk shape favors audit-first.** A naive "introduce helper, migrate
gates" approach risks subtle behavior drift in MIR instruction order,
which can leak into observable behavior (drop sequencing, cleanup
authority, package determinism). Building the inventory + matrix
first means we know what we're preserving before we touch anything.

---

## What "a String gate" means (load-bearing definition)

A naive `grep '== string_ty'` misses real shape. The audit's inventory
item is:

> Any code path that branches on String-ness at HIR or MIR level, OR
> that emits a String-specific runtime helper call (StringRetain,
> StringRelease, StringCopy, etc.).

Concretely, an inventory entry covers:

1. Direct TypeId equality: `ty == string_ty`, `ty == ensure_string()`.
2. Kind / shape checks: `td.kind is TypeKind.STRING_*` (if any).
3. Whole-pass specialization: `string_arc.py` walks every function and
   bakes String semantics throughout. The whole pass counts as one
   inventory entry, broken down by sub-region (per-MIR-op handler, per
   ownership query, per emission point).
4. Runtime-helper call sites: anywhere a `StringRetain` / `StringRelease`
   / `StringCopy` op is emitted, or a corresponding LLVM call is
   lowered.
5. Ownership-query branches: `_query_copy`, `has_drop`,
   `_compute_drop_policy`, `verdict_at` consultation paths that branch
   on String-ness internally.
6. Storage / boundary integrations: package-boundary serialization,
   FFI shape, generic-instantiation paths that special-case String.

Anything that fits one of those six patterns is an inventory entry.
Anything that doesn't isn't, even if it mentions String.

---

## Phase plan

### Phase 0 — alignment (this document)

**Status:** in progress / under review.

**Deliverable:** this plan, reviewed by user. No compiler change, no
new files outside `work/`.

**Exit gate:** user approves the plan's shape, scope, acceptance
criteria, and stop rules.

### Phase 1 — pure audit (~2–3 days, zero compiler change)

**Deliverable:** two new documentation artifacts plus regression
coverage for any uncovered cell.

1. `docs/string_ownership_inventory.md` — every gate per the
   definition above. Schema per entry:

   ```
   ### <short title>
   - **File:line:** <path>:<lineno>
   - **Kind:** [direct-typeid | kind-shape | whole-pass | runtime-helper |
     ownership-query | boundary]
   - **Question answered:** [copy_means_retain | move_transfers_stake |
     drop_releases | read_from_storage_returns_stake |
     overwrite_needs_release | other-with-explanation]
   - **Current behavior:** one-line description.
   - **Migration risk:** [low | medium | high] + one-line reason.
   - **Test coverage:** test paths that exercise this gate (or "GAP" if
     none).
   ```

   Entries grouped by file, then by `Question answered`. Total entry
   count is itself a metric — it's the load-bearing measurement of
   "how scattered is this today."

2. `docs/string_ownership_matrix.md` — String × storage-shape ×
   ownership-op coverage matrix:

   | | Copy | Move | Share* | Clone | Drop | Overwrite |
   |---|---|---|---|---|---|---|
   | bare local | ... | ... | n/a | ... | ... | ... |
   | RawBuffer<String> slot | ... | ... | n/a | ... | ... | ... |
   | Array<String> elem | ... | ... | n/a | ... | ... | ... |
   | TypeBox payload | ... | ... | n/a | ... | ... | ... |
   | struct field | ... | ... | n/a | ... | ... | ... |
   | variant payload | ... | ... | n/a | ... | ... | ... |
   | Optional<String> | ... | ... | n/a | ... | ... | ... |
   | Result<String,_> ok | ... | ... | n/a | ... | ... | ... |
   | callback param | ... | ... | n/a | ... | ... | ... |
   | closure capture (move/copy/share) | ... | ... | ... | ... | ... | ... |
   | package-boundary in/out | ... | ... | n/a | ... | ... | ... |

   *Share column included for completeness; per `std.core.shareable`
   doc, String is `copy`-only by design — the column entries should
   read "n/a (String is not Share — see shareable.drift)".

   Cells are `(behavior, test_coverage)`. Behavior is one-line
   description (e.g., "StringRetain on copy", "StringRelease on
   drop"). Coverage is test path or "GAP."

3. **Memcheck carriers** for any matrix cell currently lacking
   coverage. New tests under `lang/tests/memcheck/` only. No source
   changes outside the test tree.

**Exit gate:**

- Inventory document covers every gate matching the six-pattern
  definition. Reviewer can grep-spot-check (`ensure_string()`,
  `string_ty`, `StringRetain`, `StringRelease`, `string_arc`) and
  every hit either has an inventory entry or is explicitly marked
  "see whole-pass `string_arc.py` entry."
- Matrix document has zero `GAP` cells.
- All new memcheck carriers green on full memcheck suite (`pytest -n16
  lang/tests/memcheck/`).
- No compiler source change in this phase. `git diff main` for the
  phase-1 commit touches only `docs/` and `lang/tests/memcheck/`.

### Phase 1.5 — first migration (~1–2 days, one gate)

**Goal:** prove the helper layer works on the simplest possible gate,
with the strictest possible behavior-preservation gate.

**Deliverable:**

1. New file `lang/driftc/string_ownership.py` (or whatever fits the
   pass layout — settled in phase 1). Module docstring states the stop
   rule explicitly:

   > **Stop rule.** This module's predicates handle String only. If a
   > second type wants in, that's the trigger to either (a) widen the
   > predicates with a documented new rule at the top of this
   > docstring, naming the new type and *why* it shares String's
   > behavior, or (b) escalate to the public-trait
   > (`docs/refactor_triggers.md` § "ConstShare track") discussion.
   > **Do not** silently accumulate type-special-cases here.

2. The five predicates as plain functions:

   ```python
   def copy_means_retain(ty: TypeId, type_table: TypeTable) -> bool: ...
   def move_transfers_stake(ty: TypeId, type_table: TypeTable) -> bool: ...
   def drop_releases(ty: TypeId, type_table: TypeTable) -> bool: ...
   def read_from_storage_returns_stake(ty: TypeId, type_table: TypeTable) -> bool: ...
   def overwrite_needs_release(ty: TypeId, type_table: TypeTable) -> bool: ...
   ```

   Each is a one-line `return ty == type_table.ensure_string()` today.
   Implementation is not the point; the *interface* is the deliverable.

3. **One** gate migrated, picked from the inventory's lowest
   `Migration risk` tier. Candidates surfaced during phase 1; expected
   shortlist:
   - the `_query_copy` early-return for String, OR
   - one of the well-isolated `string_arc.py` predicate sites (e.g. a
     single `td.has_drop` consultation that decides whether to insert
     a release).

4. Cert tests + memcheck unchanged. New unit tests for the helper
   itself.

**Exit gate (load-bearing):**

- Full driver/e2e + memcheck gate green on `pytest -n16`.
- **`.dmp` byte-determinism check.** Build a representative downstream
  package (web-rest 0.4 or net.tls) before and after, diff the `.dmp`
  bytes. Must be byte-identical. This is the real test that internal
  refactor didn't accidentally change package format.
- **Source-rebuild trust gate.** Run the orch certify lane (the lane
  that caught 0.31.36's collision-check regression). Must pass.

### Phase 2 — sweep (~3–5 days, batched)

**Deliverable:** remaining low-risk gates migrated, in 2–4 small
patches. Each patch:

- Migrates 1–3 gates from the inventory.
- Full test gate + memcheck + `.dmp` determinism check between patches.
- Updates the inventory document to mark migrated entries.

**Out of scope (load-bearing):** anything inside `string_arc.py`'s
**instruction-emission** logic. Predicate consultations inside
`string_arc.py` can migrate; reordering or replacing instruction
emission cannot. Reordering MIR instructions risks observable
behavior even when "equivalent."

**Exit gate:**

- All inventory entries marked `Migration risk: low` are migrated, OR
  explicitly marked "deferred to phase 3 (reason: …)."
- Test gate + memcheck + `.dmp` determinism green.
- Inventory's "load-bearing measurement" — count of remaining
  scattered gates — is reported in the phase-2 close-out memo.

### Phase 3 — stop & re-evaluate

**Not committed.** Phase 3 is a re-evaluation, not a planned
implementation phase.

**Trigger to start:** phase 2 closes. Look at the residual inventory
entries:

- (a) **Residual is small and obviously String-specific** (string
  layout-aware code, runtime helper emission). Leave it. The
  abstraction has done its job. Track wraps up; the helper file stays;
  branch merges.
- (b) **Residual is still large and gate-heavy.** Phase 3 is its own
  design discussion, possibly with a second adopter on the horizon.
  If a second adopter has materialized by then, this is also the
  natural moment to re-open the public-`ConstShare` discussion in
  `docs/refactor_triggers.md`.

---

## Acceptance criteria — full-track close-out

The track wraps when **all** of these are true:

1. `docs/string_ownership_inventory.md` and `docs/string_ownership_matrix.md`
   exist, are accurate, and have zero `GAP` cells.
2. Memcheck carriers cover every matrix cell.
3. `lang/driftc/string_ownership.py` (or final name) exists with the
   five-predicate interface and the documented stop rule.
4. The "load-bearing measurement" — count of remaining scattered
   gates — has dropped meaningfully versus phase-1 baseline. Target:
   ≥ 50% reduction in scattered gates outside `string_arc.py`'s
   instruction-emission logic. (Inside `string_arc.py`'s emission
   code is allowed to remain — that's phase-3 territory.)
5. Source semantics unchanged (full driver/e2e/memcheck gate green
   throughout).
6. `.dmp` package format unchanged (byte-determinism check passes
   throughout).
7. ABI version unchanged (no compiler/runtime boundary shift).
8. Compiler version bumped per AGENTS.md "Compiler versioning rule"
   (one bump per landing patch with behavior-relevant change; phase 1
   may be doc-only and skip the bump).

---

## Stop-and-escalate triggers (during the track)

Halt the track and consult before proceeding if any of these fire:

- **`.dmp` determinism breaks.** Stop. The change is not internal-only
  by definition — it touched serialization shape. Either revert or
  rescope to bundle with a planned ABI/format bump.
- **A second type wants in on the helper.** Stop. Decide between
  widening the helper (with documented rule) or escalating to the
  public-`ConstShare` discussion. Don't silently accumulate.
- **A migration touches MIR instruction emission order.** Stop. That's
  phase-3 territory; revert and document the gate as deferred.
- **Memcheck regression in any cell.** Stop. The matrix's job is to
  catch this; if it didn't, the matrix is wrong before the migration
  is wrong. Fix the matrix first.

---

## Out of scope (will not do in this track)

- Public `ConstShare` trait surface. Filed in
  `docs/refactor_triggers.md`; track gates the trait on a second
  adopter or an ABI-bump carrier.
- `string_arc.py` instruction-emission rewrite. Phase 3 territory.
- Changes to String's surface semantic (still `copy`, not `share`,
  per `shareable.drift`).
- Package format / `.dmp` schema changes.
- ABI bumps.
- Migration of call-mediated escape detection or borrow-checker
  walker logic. (Different track — see
  `docs/refactor_triggers.md` § "Consolidate borrow-checker walkers".)

---

## Open questions (to resolve before phase 1 starts)

1. **Helper file location.** `lang/driftc/string_ownership.py`
   (top-level driftc) or `lang/driftc/stage2/string_ownership.py`
   (next to `string_arc.py`)? Phase 1 settles this based on which
   passes consume the predicates.
2. **`Optional<String>` row in matrix — do we treat it as its own
   row, or fold it into "variant payload"?** Lean toward own row —
   `Optional` is the most-tested shape and any divergence from
   "generic variant payload" is itself a finding.
3. **Audit tooling.** Pure manual grep, or a small one-shot script
   that enumerates the patterns? If script, it lives under
   `work/string-ownership/` and never enters source/stdlib/tests.
4. **Phase-1 review cadence.** Does the inventory get reviewed
   incrementally (subtree at a time) or as one big patch? Lean toward
   incremental — easier to spot misses, smaller review surface.

---

## Notes on what this plan is NOT

- It is **not** a green light to start phase 1 — that requires user
  approval of this plan first.
- It is **not** a commitment to phases 2 / 3 — those gate on phase-1
  findings and phase-1.5 outcomes.
- It is **not** a guarantee of timeline. Estimates are
  rough-order-of-magnitude; real numbers come out of phase-1's
  inventory.
- It is **not** under source/stdlib/tests/tooling. `work/` is
  ephemeral; when the track closes, this file gets cleaned along with
  the rest of `work/string-ownership/`. Durable artifacts (inventory
  + matrix) move to `docs/` as a phase-1 deliverable.
