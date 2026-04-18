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

Implementation steps — strictly sequential, each keeps the repo
runnable:

### Step 1: the activation bundle.

The earlier draft tried to split layout specialization, fat
method lowerings, `ARC_AS_INTERFACE`, stdlib migration, and the
`conc.arc<T>(iface)` rejection into separate steps.  A probe
showed they cannot be separated without transient compiler
scaffolding that gets deleted the moment the bundle completes.
Specifically:

- `fn arc<T>(value: T) -> Arc<T>` in
  `stdlib/std/concurrent/concurrent.drift:446` has a body
  (`return Arc<type T>(buf = rawbuffer_from_parts(...))`) that
  is structurally valid **only** for the thin `{buf}` layout.
- `_arc_clone_impl<T>`, `_arc_get_impl<T>`, `_arc_destroy_impl<T>`
  read `self.buf` and are valid only for thin concrete T.
- The moment `T=interface` implies the fat `{ctrl, data, vtable}`
  layout, every instantiation of those bodies at `T=interface`
  is structurally wrong — the monomorphizer generates code
  against the thin schema against a struct with the fat schema.
- The supported construction path for `Arc<Interface>` is
  `arc(concrete).as_interface<I>()`, so `std.log` **must
  migrate in the same commit** — it currently uses
  `conc.arc<type ContextResolver>(move noop)` which is precisely
  the banned shape.
- Keeping `conc.arc<T=iface>` allowed after fat layout lands
  would let user code request an impossible construction —
  the rejection is not optional.

So Step 1 is **"activate fat Arc<I>"** as one coherent commit:

1a. **Layout specialization.**  `ensure_struct_instantiated` in
   `lang/driftc/core/types_core.py` routes `Arc<T>` with
   `is_arc_interface_view(schema, type_args)` to the fat
   `{ctrl, data, vtable}` layout via
   `_arc_interface_view_layout`.

1b. **Fat `ARC_CLONE` / `ARC_GET` / `ARC_DESTROY`.**  In
   `hir_to_mir.py::_lower_arc_intrinsic_call`, branch on
   receiver fatness (`is_arc_interface_view_instance(
   concrete_receiver_ty)`).  Fat side lowers via
   compiler-emitted MIR ops / a single non-generic fat helper
   in std.concurrent (I is irrelevant at these ops — all three
   operate on the `{ctrl, data, vtable}` erased triple).

1c. **`ARC_AS_INTERFACE` lowering.**  Emitted at
   `hir_to_mir.py` for the `&Arc<T=concrete>` → `Arc<I>`
   transition: read `ctrl` via `rawbuffer_ptr<ArcBox<T>>(&self.buf)`,
   `atomic_fetch_add(ctrl.strong, 1)`, compute `data = ctrl +
   offsetof(ArcBox<T>, value)`, look up the T-as-I vtable
   through the existing `_ensure_interface_vtable(I, T)` hook,
   construct the fat `Arc<I>{ctrl, data, vtable}`.

1d. **std.log migration.**  Rewrite the two call sites at
   `stdlib/std/log/log.drift:455` and `:590`:

       val concrete_noop = NoContextResolver();
       // was: conc.arc<type ContextResolver>(move concrete_noop)
       // now: conc.arc(move concrete_noop).as_interface<type ContextResolver>()

   Closes task #24.

1e. **Typecheck rejection of `conc.arc<T=iface>`.**  In
   `lang/driftc/checker/call_resolver.py` at the generic
   free-call resolution: when `decl.fn_id` resolves to
   `std.concurrent::arc` AND
   `is_arc_interface_view_instance(sig_inst.result_type)`,
   emit `E_ARC_OF_INTERFACE_DIRECT` with a directive to
   `arc(concrete).as_interface<type I>()`.  Turns
   `test_arc_rejects_interface_t.py` green.

Gate for the activation bundle:
- `test_fat_arc_interface_views.py`: 6/0.
- `test_arc_rejects_interface_t.py`: 3/0.
- `test_arc_intrinsic_bridge.py`: 5/0 (Stage 2 bridge unchanged).
- `std_log_resolver_active` e2e fixture: green (runtime pin).
- Full driver suite: matches post-ArcHeader-fix count
  (1035/0 or higher if the activation bundle unblocks tests
  that were waiting on fat Arc).

### Step 2: ABI bump 9 → 10 + version bump.

Follow-up after the activation bundle is gate-green.
`lang/versions.py`: `DRIFT_RT_ABI_VERSION: 9 → 10` and
`DRIFTC_VERSION` patch bump.  The only boundary-exposed
`Arc<Interface>` today is
`stdlib/std/log/log.drift::LoggerConfig.resolver`; no downstream
packages are published against the current tip, so no external
coordination required.

### Step 3: memory + docs.

Mark task #22 + #26 completed.  Add a Stage 3 memory note with
the fat layout fields and the ABI contract.  Update
`project_fat_arc_stage2_bridge.md` to cross-reference the
follow-up Stage 3 note.

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

## Close of the landing session (2026-04-17)

State on feature/fat-arc-interface-views:
- Stage 2 Option B bridge operational (4/4 pins green).
- ArcHeader.drop_thunk Fn-field package serialization fixed
  (task #41, full driver suite 1035/0).
- Stage 3 regressions landed
  (`lang/tests/driver/test_fat_arc_interface_views.py`,
  5 expected failures on ARC_AS_INTERFACE, 1 Stage 1 pass;
  `lang/tests/driver/test_arc_rejects_interface_t.py`,
  1 expected failure pending Step 4, 2 negative controls green).
- This plan doc landed as the Stage 3 implementation map.
- An earlier draft of this plan put rejection of
  `conc.arc<T>(iface)` first; the attempt uncovered a tight
  coupling to `ARC_AS_INTERFACE` lowering (stdlib's std.log
  uses the exact shape that rejection targets), so the checker
  change was reverted.  The sequence above now bundles the
  rejection + std.log migration into Step 4, after Step 3 makes
  `as_interface<I>()` operational.

Next session starts with **Step 1 — the activation bundle** as
described above: layout specialization + fat
`ARC_CLONE/GET/DESTROY` + `ARC_AS_INTERFACE` + std.log migration
+ `conc.arc<T=iface>` rejection, landing as one coherent commit.
A probe attempt (2026-04-17) showed the pieces cannot be
separated without transient scaffolding that the bundle itself
deletes — the earlier multi-step decomposition is preserved
above as a structural index, but the commit granularity is the
whole bundle.  ABI bump (Step 2) and docs/memory (Step 3) are
separate follow-up commits after the bundle passes its gate.
