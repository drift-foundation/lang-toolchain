# Stage 3 — fat Arc<Interface> representation boundary (plan)

Feature branch: `feature/fat-arc-interface-views`, after Stage 2
Option B and the ArcHeader.drop_thunk serialization fix landed.
Task #22 (in_progress) + task #26 (version/ABI).

## Regressions (landed up-front — `lang/tests/driver/test_fat_arc_interface_views.py`)

1. `test_happy_path_two_interfaces_dispatch` — `Arc<AppService>`
   → `as_interface<Greeter>()` + `as_interface<Counter>()`; both
   views dispatch correctly. **Fails today** on Stage 2 placeholder
   assertion.
2. `test_shared_mutation_across_interface_views` — mutation via
   one interface view observed through another, with sync.AtomicInt
   as the shared-mutable payload.  **Fails today.**
3. `test_drop_order_destructor_runs_exactly_once` — `Destructible::
   destroy` fires exactly once across `arc + v1 + v2` scope exits.
   **Fails today.**
4. `test_as_interface_rejected_when_require_fails` — Stage 1
   typecheck gate.  **Passes today** (kept colocated).
5. `test_clone_through_interface_view_preserves_dispatch` — `Arc<I>
   .clone()` produces a second Arc<I> sharing the same ctrl.
   **Fails today.**
6. `test_std_log_resolver_still_works` — std.log
   `context_resolver(Arc<ContextResolver>)` compiles + runs.
   **Fails today.**

## Implementation scope

### A. Compile-time rejection of `arc<T>(iface)`

Location: `lang/driftc/checker/call_resolver.py` or wherever `arc`
is resolved.  When the resolved `T` is a `TypeKind.INTERFACE`,
emit a diagnostic directing the caller to
`arc(concrete).as_interface<type I>()`.

No runtime changes.

### B. Fat layout for `Arc<T>` when T is interface

Location: `lang/driftc/core/types_core.py::ensure_struct_instantiated`
(or the per-base specialization hook).  The predicate
`is_arc_interface_view(type_args)` already exists dormant from
Stage 1.  When the predicate holds, the instantiated Arc<I> struct
gets fields:

    { ctrl:   mem.Ptr<Byte>,   // -> ArcBox<T>.header.strong (atomic ops)
      data:   mem.Ptr<Byte>,   // -> ArcBox<T>.value (iface receiver)
      vtable: mem.Ptr<Byte> }  // T-as-I vtable symbol

instead of the thin `{ buf: RawBuffer<ArcBox<I>> }` shape.

Soundness hooks that likely need an is-fat branch:
- Struct field-access typecheck (consumer code can't see these
  fields — they're compiler-internal).
- Destructor/drop selection (thin helpers assume `self.buf`; fat
  needs its own helper).
- Size/alignment computation for boundary ABI.
- Package serialization: this is a Stage-3-specific schema shape
  that must round-trip cleanly for downstream consumers.

### C. MIR / LLVM lowerings for fat Arc operations

Four intrinsic kinds need a fat-T branch parallel to Stage 2's
concrete-T helper redirect (`hir_to_mir.py::_lower_arc_intrinsic_call`):

- **ARC_CLONE (fat):** `atomic_fetch_add(ctrl.strong, 1)` →
  return a new `Arc<I>{ctrl, data, vtable}` with the same fields.
- **ARC_GET (fat):** return `&I = {data, vtable}` — a borrowed
  interface reference.  No refcount touch.
- **ARC_DESTROY (fat):** `atomic_fetch_sub(ctrl.strong, 1)`; if
  previous value == 1, load `drop_thunk` from
  `(ctrl as *ArcHeader).drop_thunk`, call it with `ctrl`.
- **ARC_AS_INTERFACE (from thin → fat):** This is the new one.
  1. Read `self.buf` (thin Arc<T>) → get `ctrl_ptr = rawbuffer_ptr(&self.buf)`.
  2. `atomic_fetch_add(ctrl_ptr.header.strong, 1)`.
  3. `data_ptr = ctrl_ptr + offsetof(ArcBox<T>, value)`.
  4. Look up T-as-I vtable symbol (reuse existing
     `_ensure_interface_vtable(I, T)` codegen hook).
  5. Construct fat `Arc<I>{ctrl_ptr, data_ptr, vtable_ptr}`.

Path choice:
- **Option i (Drift-source helpers for fat-T)**: would mirror
  Stage 2's `_arc_*_impl<T>` pattern.  Blocker: Drift source
  cannot express `offsetof(ArcBox<T>, value)` or dynamic vtable
  lookup keyed on both `T` and `I`.  A new set of MIR intrinsics
  would be needed anyway.
- **Option ii (compiler-emitted inline lowering)**: emit MIR ops
  directly in `hir_to_mir.py::_lower_arc_intrinsic_call` for the
  fat-T branch.  More natural fit — the operations aren't
  expressible in source, and the intrinsic is compiler-owned
  end-to-end per Stage 2's directive.

Plan: **Option ii**.

### D. ABI bump 9 → 10 + compiler version bump

Location: `lang/versions.py`.

Boundary-exposed `Arc<Interface>` inventory (from phase1.md §ABI):
- `stdlib/std/log/log.drift`: `LoggerConfig.resolver:
  conc.Arc<ContextResolver>`.

No downstream packages published against 0.27.203 tip; no
coordination needed.  The bump lands *with* the layout change.

## Step-by-step work plan

Prerequisite (already landed, not part of implementation sequence):
6 regression tests in
`lang/tests/driver/test_fat_arc_interface_views.py`.  5 fail as
expected on the Stage 2 `ARC_AS_INTERFACE` placeholder; test #4
(negative require) passes via Stage 1 typecheck gate.

Implementation slices (user decision 2026-04-17, REVISED):
activation-bundle-as-one-commit rule loosened.  Each slice is
either (a) green and behaviour-preserving or (b) clearly dormant
behind an inactive branch.  The strict invariant remains: no
shippable broken intermediate state where public `Arc<I>`
behaviour is half-enabled.

### Slice 1 — dormant runtime primitives.

`stdlib/std/concurrent/concurrent.drift` adds two non-generic
private helpers that operate only on the erased `ctrl: Ptr<Byte>`
(I-independent):
- `_arc_fat_bump_strong_via_ctrl` — atomic strong-count +1.
- `_arc_fat_drop_via_ctrl` — null guard + atomic strong-count
  −1 + `drop_thunk` on last drop.

Both mirror the thin-path ordering (`_arc_clone_impl<T>`,
`_arc_destroy_impl<T>`) exactly.  No call sites.  Full driver
stays at baseline.  **Landed, gate green.**

### Slice 2 — fat dispatch scaffolding (dormant).

- New layout predicate `TypeTable.is_arc_fat_layout_instance`
  (distinct from semantic `is_arc_interface_view_instance`):
  returns True only when the struct-instance field names are
  exactly `("ctrl", "data", "vtable")`.
- `hir_to_mir.py::_lower_arc_intrinsic_call` detects fat
  receivers via the layout predicate and dispatches to
  `_lower_arc_fat_intrinsic_call`:
  - `ARC_CLONE` — extracts ctrl/data/vtable, calls Slice 1
    `_arc_fat_bump_strong_via_ctrl(ctrl)`, constructs a new
    `Arc<I>{ctrl, data, vtable}`.
  - `ARC_DESTROY` — extracts ctrl, calls Slice 1
    `_arc_fat_drop_via_ctrl(ctrl)`.
  - `ARC_GET` — scaffolded with an explicit slice-3
    `NotImplementedError` (needs coordination with layout
    activation and the existing `IfaceUpcast`/`CallIface`
    vtable machinery; no Arc-specific vtable namespace
    introduced).

Dormancy: `ensure_struct_instantiated` does NOT yet emit the
fat layout, so the layout predicate returns False everywhere,
and the entire fat dispatch is unreachable.  **Landed, gate
green.**

### Slice 3 — fat Arc<I> activation.

This is the slice that flips public `Arc<I>` behaviour.  Every
piece listed below must land TOGETHER in one commit — there is
no intermediate world where the layout flip is live but one of
these is missing without producing a half-broken state.

**Why all in one commit (coupling rationale):**

- Layout flip makes `Arc<I>` a fat `{ctrl, data, vtable}`
  struct.  Any code path that still constructs `Arc<I>` as
  thin `{buf = …}` — including `fn arc<T=I>` in
  `stdlib/std/concurrent/concurrent.drift` — hard-fails to
  typecheck ("struct 'Arc' constructor expects 3 args, got 1"
  at `concurrent.drift:494`).  The ONLY call site in the
  current tree that exercises that broken path is
  `stdlib/std/log/log.drift:455` / `:590`, which constructs
  `conc.arc<type ContextResolver>(move noop)` and triggers
  the `fn arc<T=ContextResolver>` instantiation.  Without
  slice 3 migrating those two sites, the stdlib itself stops
  compiling and the entire driver gate blows up.
- Direct `conc.arc<T=interface>` from user code fails after
  activation too — same cryptic constructor error.  Adding
  the typecheck rejection (`E_ARC_OF_INTERFACE_DIRECT`)
  in the same commit gives users the proper diagnostic
  from the moment layout activates; deferring it to a
  follow-up leaves users with a confusing cryptic error in
  the meantime.  No user-visible benefit to separating.
- Fat `ARC_GET` emission and fat rvalue-receiver support
  are load-bearing for every call chain through
  `as_interface<I>().get()` / `.clone()` / `.destroy()`,
  which is every fat Arc test's hot path.  Missing either
  leaves the activation non-shippable.

3a. **Layout specialization flag flip.**  Set
   `STAGE3_FAT_ARC_ACTIVE = True` in
   `lang/driftc/core/types_core.py` so
   `ensure_struct_instantiated` routes `Arc<T>` where T is
   an interface to the fat `{ctrl, data, vtable}` layout
   (via `_arc_interface_view_layout` and the existing
   `is_arc_interface_view` predicate).  Flipping the flag is
   the atomic cut-over moment; this step must land in the
   same commit as 3b–3g below.

3b. **Fat `ARC_GET` emission.**  Replace the Slice 2
   `NotImplementedError` in
   `_lower_arc_fat_intrinsic_call` with the actual
   `&I = {data, vtable}` construction.  Reuse the existing
   `IfaceUpcast` / `CallIface` / `_ensure_interface_vtable(I, T)`
   machinery — do not invent an Arc-specific vtable namespace.
   The `data` and `vtable` fields of the fat Arc<I> ARE the
   borrowed-interface shape; `.get()` just materializes that
   pair as a `&I` value, no refcount touch.

3c. **`ARC_AS_INTERFACE` lowering.**  Emit at
   `hir_to_mir.py` for the `&Arc<T=concrete>` → `Arc<I>`
   transition:
   - Read the thin `self.buf` and extract
     `ctrl = rawbuffer_ptr<ArcBox<T>>(&self.buf)`.
   - Call Slice 1 `_arc_fat_bump_strong_via_ctrl(ctrl)`.
   - Compute `data = ctrl + offsetof(ArcBox<T>, value)` —
     per-T compile-time constant.
   - Look up T-as-I vtable via
     `_ensure_interface_vtable(I, T)`.
   - Construct fat `Arc<I>{ctrl, data, vtable}`.

3d. **Destructible-scan update.**  At `driftc.py:3824`, fat
   `Arc<I>` instances must point `destructor_fns` at a
   compiler-emitted fat wrapper (single non-generic shim that
   extracts `ctrl` and calls `_arc_fat_drop_via_ctrl`) rather
   than queuing the thin per-T `_arc_destroy_impl<T>` helper.

3e. **Helper-instantiation update.**
   `driftc.py::_queue_instantiations` and
   `_arc_helper_template_key_for_intrinsic` must NOT queue
   thin `_arc_*_impl<T>` helpers when the callsite receiver
   type is fat `Arc<I>`.

3f. **Fat rvalue-receiver support.**  Slice 2's
   `_lower_arc_fat_intrinsic_call` reuses the Stage 2
   by-borrow lvalue path; chained-rvalue receivers currently
   hit `NotImplementedError`.  Slice 3 MUST cover the
   idiomatic shapes as part of activation:

       val face2 = app.as_interface<type Face>().clone();
       val x     = app.as_interface<type Face>().get().method();

   Regressions already pinned:
   `test_as_interface_chained_rvalue_clone` and
   `test_as_interface_chained_rvalue_get_method` in
   `lang/tests/driver/test_fat_arc_interface_views.py`.

3g. **std.log migration.**  Rewrite the two call sites at
   `stdlib/std/log/log.drift:455` and `:590`:

       val concrete_noop = conc.arc(NoContextResolver());
       // was: conc.arc<type ContextResolver>(move noop)
       // now: concrete_noop.as_interface<type ContextResolver>()

   And change the `context_resolver` builder method signature
   from `var r: ContextResolver` to
   `var r: conc.Arc<ContextResolver>` — caller now constructs
   the Arc+coercion explicitly.  Update
   `lang/tests/codegen/e2e/std_log_resolver_active/main.drift`
   to the new builder shape.  Closes task #24.

3h. **Typecheck rejection of `conc.arc<T=iface>`.**  In
   `lang/driftc/checker/call_resolver.py` at the generic
   free-call resolution: when `decl.fn_id` resolves to
   `std.concurrent::arc` AND
   `is_arc_interface_view_instance(sig_inst.result_type)`,
   emit `E_ARC_OF_INTERFACE_DIRECT` with a directive to
   `arc(concrete).as_interface<type I>()`.  Turns
   `test_arc_rejects_interface_t.py` green.  Bundled into
   Slice 3 alongside layout activation because after the
   flip the `fn arc<T=iface>` body is structurally invalid —
   a direct call either hits this rejection (clean
   diagnostic) or the cryptic 3-args-vs-1-arg constructor
   failure from inside the stdlib body (bad UX).  Users
   deserve the clean diagnostic from the moment layout
   activates; no reason to ship layout without it.

Slice 3 gate:
- `test_fat_arc_interface_views.py`: all tests green
  (all 7 fat-Arc regressions flip, including the two
  rvalue shapes).
- `test_arc_rejects_interface_t.py`: 3/0.
- `test_arc_intrinsic_bridge.py`: 5/0 (Stage 2 thin bridge
  unchanged).
- `std_log_resolver_active` e2e fixture: green (runtime pin).
- Full driver: baseline (1038+) restored with activation live.

### Slice 4 — ABI bump 9 → 10 + version bump.

Small follow-up after Slice 3 is gate-green.
`lang/versions.py`: `DRIFT_RT_ABI_VERSION: 9 → 10` and
`DRIFTC_VERSION` patch bump.  Only boundary-exposed
`Arc<Interface>` today is
`stdlib/std/log/log.drift::LoggerConfig.resolver`; no
downstream packages published against the current tip, so no
external coordination required.

### Slice 5 — memory + docs.

Mark task #22 + #26 completed.  Add a Stage 3 memory note
with the fat layout fields and the ABI contract.  Update
`project_fat_arc_stage2_bridge.md` to cross-reference the
Stage 3 follow-up.

## Estimated surface

- `lang/versions.py` — 2-line bump.
- `lang/driftc/checker/call_resolver.py` — 1 diagnostic site.
- `lang/driftc/core/types_core.py` — is-fat branch in
  `ensure_struct_instantiated`, fields + getters.
- `lang/driftc/stage2/hir_to_mir.py` — is-fat branch in
  `_lower_arc_intrinsic_call`; new `ARC_AS_INTERFACE` lowering
  emitting 5 MIR ops.
- `lang/codegen/llvm/llvm_codegen.py` — possibly new helper for
  vtable-symbol lookup at call site (or reuse existing
  `_ensure_interface_vtable`).
- `stdlib/std/concurrent/concurrent.drift` — **no changes**
  (layout is compiler-emitted; `@intrinsic` methods stay bodyless).
- `stdlib/std/log/log.drift` — **migrated in Step 4** (two
  call sites at lines ~455 and ~590 move from
  `conc.arc<type ContextResolver>(move resolver)` to
  `conc.arc(move concrete).as_interface<type ContextResolver>()`).
  The shape change is otherwise transparent to users; closes
  task #24.

Expected diff size: medium.  ~200-500 compiler LOC plus the 6
regressions.

## Resolved architectural questions (user decisions, 2026-04-17)

1. **Destructor wiring at drop time — single non-generic fat
   helper.**  For `Arc<Interface>`, use ONE non-generic
   fat-destroy helper that operates on `{ctrl, data, vtable}`
   (all `Ptr<Byte>`).  I is irrelevant at drop time because
   last-drop runs `drop_thunk` (captured at `arc<T>(concrete)`
   time, not via the vtable).  One destructor symbol shared
   across every Arc<I> instance; the Destructible-impl scan at
   `driftc.py:3824` points every fat Arc<I> layout at this
   single symbol, bypassing the per-T `_arc_destroy_impl<T>`
   template route used for thin Arc.

2. **`ctrl_ptr` points at the header/base — one meaning only.**
   `ctrl` carries a single, fixed semantic: the base address
   used by BOTH the atomic `strong` refcount ops AND the
   `drop_thunk` lookup.  Given `ArcHeader`'s `strong` field is
   at offset 0 of the header, and the header is at offset 0 of
   `ArcBox<T>`, `ctrl = &ArcBox<T>` is the natural choice and
   the one we commit to.  `drop_thunk` lookup reads
   `(ctrl as *ArcHeader).drop_thunk`.  No parallel "header
   offset pre-applied" variant — one meaning for `ctrl`
   everywhere.

3. **T-as-I vtable symbol — reuse existing interface vtable
   path, do NOT invent an Arc-only namespace.**  `ARC_AS_INTERFACE`
   lowering references the same symbol shape (and the same
   `_ensure_interface_vtable(I, T)` hook) that `IfaceUpcast` and
   `CallIface` use today.  If the link-time emission is driven
   by other coercion sites, extend that driver to recognize
   `ARC_AS_INTERFACE` as a vtable-requiring site — do not emit
   a separate Arc-specific vtable symbol.

---

## Historical notes (superseded — slice structure above is authoritative)

These notes are kept for context but do NOT drive the work.  If
anything below conflicts with the slice structure above, the
slice structure wins.

- Stage 2 Option B bridge operational (4/4 pins green).
- ArcHeader.drop_thunk Fn-field package serialization fixed
  (task #41, full driver suite 1035/0).
- Stage 3 regressions landed
  (`lang/tests/driver/test_fat_arc_interface_views.py`,
  5 expected failures on ARC_AS_INTERFACE + 2 rvalue pins,
  1 Stage 1 pass; `lang/tests/driver/test_arc_rejects_interface_t.py`,
  1 expected failure pending the later bundled activation step,
  2 negative controls green).
- An earlier draft of this plan put rejection of
  `conc.arc<T>(iface)` as a standalone first step; a probe
  attempt uncovered a tight coupling to `ARC_AS_INTERFACE`
  lowering (stdlib's std.log uses the exact shape that
  rejection targets), so the checker change was reverted.  The
  rejection is now bundled into the Slice 3 activation commit
  alongside `ARC_AS_INTERFACE`, std.log migration, and the
  layout flag flip.
- An earlier draft also tried a "one-commit-or-nothing"
  activation rule for Stage 3 as a whole.  The user loosened
  that on 2026-04-17 to allow the 5-slice split above, with
  Slices 1 and 2 landing as dormant scaffolding behind the
  inactive `STAGE3_FAT_ARC_ACTIVE` flag, and Slice 3 being
  the single atomic activation commit.

**Current state on feature/fat-arc-interface-views:**
Slices 1 and 2 are landed (dormant scaffolding); Slice 3
opener landed the `STAGE3_FAT_ARC_ACTIVE = False` flag and
the two rvalue regression pins; the real Slice 3 activation
commit (flag flip + all coupled pieces) is the next
implementation step.
