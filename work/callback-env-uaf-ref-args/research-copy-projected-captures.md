# Copy-typed projected lambda captures — end-to-end design/findings

Status: research only, no code written. This is the concrete follow-up design
for the deferred item recorded in `projected-copy-captures-followup.md`. It
supersedes that doc's "the prologue binds by root and that's the whole problem"
framing: the prologue is only one of several broken layers, and the body-read
machinery already does the hard part.

**Scope correction (2nd review pass):** the original framing of this doc as
"metadata/prologue guards" understated the work. Reviewer identified two
additional High-severity ownership/alias-bookkeeping gaps (§4e, new) that are
REQUIRED for correctness, not optional hardening — without them, "fixing" the
metadata crash reintroduces UAF-class bugs for non-bitcopy Copy fields
(String, refcounted structs) via a different mechanism than the one the
primary fix closed. Correct scope: **"projected capture lowering plus
ownership alias handling,"** not just metadata/prologue guards. Two more
narrow driftc.py gaps (Medium, Low — §4c items 4-5) were also found. All four
are verified against the actual code below, not just asserted.

All line numbers are against the tree at commit `dee458cc` ("Fix boxed callback
projected move captures"), the current `main`.

---

## 0. TL;DR / headline findings

1. **Projected captures are already broken end-to-end TODAY, before any Copy
   feature work.** A plain implicit REF-kind projected capture (not MOVE, so
   NOT caught by the shipped stage1 rejection) crashes at LLVM codegen with the
   exact error the task quotes. Reproduced on clean `main`:

   ```
   NotImplementedError: LLVM codegen v1: integer binop requires matching
   Int/Uint operands (have %Struct_main_Prepared_..., drift.int)
   ```

   from this program (immediate-invoked lambda, no `captures(...)`):

   ```drift
   fn use_it(p: Prepared) -> Int {
       return (| | => { return p.count + 1; })();   // p.count : Int (Copy)
   }
   ```

   So this is not "add a new feature onto working machinery" — it is "finish
   wiring a half-implemented one." The stage1 MOVE+proj rejection merely hides
   the MOVE subset; the REF subset was never gated and never worked.

2. **The read side is already projection-aware for TYPE resolution/dispatch,
   but not fully correct for OWNERSHIP once metadata is fixed (revised — see
   items 5-6).** Every body-read site keys on the *full* `HCaptureKey` (root
   **and** `proj`) and routes to the env slot: `_visit_expr_HField`
   (hir_to_mir.py:3496), `_infer_expr_type` for `HField` (11428) and `HVar`
   (11262), and `_lower_addr_of_place`'s longest-field-prefix match (11865).
   **No HIR body rewrite is needed** — the task's approach (a) (substitute
   `p.count` with a synthesized `__cap_*` local) is unnecessary. The source
   `p.count` `HField` already resolves to the slot. This part of the original
   claim stands. It does NOT mean the read side is alias-safe for non-bitcopy
   fields — see items 5-6.

3. **The broken layers are all on the metadata/prologue side, and all three
   ignore `key.proj` by keying on the root binding id:**
   - **driftc.py hidden-lambda worklist** (7096–7128 and 7271–7284): re-derives
     the capture's type and `env_field_types[slot]` from the *root local's*
     origin type (the whole `Prepared` struct), overwriting the correct
     projected field/ref type (`&Int`) that outer lowering already computed.
     **This overwrite is the proximate cause of the codegen crash.**
   - **`_emit_lambda_capture_prologue`** (hir_to_mir.py:5416): materializes a
     body-visible local named after the *root* (`p`) for every slot, typed from
     the (now-corrupted) `env_field_types[slot]`, storing the slot value into
     it. For a projection this is a bogus `p` local; for two projections of the
     same root it is a name collision.
   - **`_load_capture_from_env`** (1297) then mis-derefs because it trusts the
     corrupted field type.

4. **Recommended shape (revised):** a contained change across stage1-checker +
   driftc.py worklist (now 4 guard sites, not 2) + `hir_to_mir` prologue guard
   **plus two required ownership-alias fixes** (§4e). It is NOT a new HIR
   pass, but it is more than "metadata/prologue guards" — see the scope
   correction above and §7.

5. **(High, reviewer finding) COPY-branch env construction never calls
   `_copy_if_ref_alias` before storing the captured field's value into the
   heap env** (hir_to_mir.py:5051/5283). For a Copy-but-non-bitcopy field
   (`String` — Copy per `copy_status()`, but retains refcount on copy) this
   can store an ALIASED view into the env instead of an owned copy, reopening
   a UAF/double-drop shape via a different mechanism than the primary fix
   closed. See §4e.1.

6. **(High, reviewer finding) `_load_capture_from_env`'s REF-kind branch never
   marks its `LoadRef` result in `_ref_field_temps`**, unlike the otherwise-
   identical general deref path (hir_to_mir.py:3410-3411). A REF-projected
   read of a non-bitcopy field (`&String`, `&SomeStruct`) is invisible to
   every downstream ownership-transfer-boundary `_copy_if_ref_alias` call
   (return, bind, pass, store) — the body treats it as already-owned, and a
   later drop of both the alias and the original double-frees. See §4e.2.

---

## 1. Baseline: how a WHOLE-LOCAL capture works (the contrast case)

Trace for `captures(move x)` / implicit whole-local read, so we can see exactly
which parts are root-keyed vs key-keyed.

### 1a. Discovery (stage1/capture_discovery.py)

`discover_captures` (36) walks the lambda body. For a bare `HVar x` read it
calls `_add_usage(x.binding_id, [], read=True)` (163) → `HCaptureKey(root_local=x,
proj=())`. Final kind decided at 433–459: `move`→MOVE, borrow_mut/write→REF_MUT,
borrow_shared→REF, plain read→`MOVE if capture_as_move else REF` (457). Result is
`HCapture{kind, key=HCaptureKey(root, ())}`.

`capture_as_move` is set True for escaping/boxed-callback lambdas, so a plain
field read inside `core.callback0(...)` defaults to MOVE — this is the path the
UAF fix targets. Immediate-call lambdas leave it False → plain reads become REF.

### 1b. Outer lowering — env construction

`_lower_lambda_immediate_call` (4922) and `_lower_lambda_callback` (5155) are
near-identical. Per capture (loops at 4991 / 5223):
- REF/REF_MUT: `_lower_addr_of_place(place)` → pointer; field type
  `ensure_ref(inner)`.
- MOVE (whole local only): `_move_from_callback_capture_slot` or `MoveOut` of the
  root local; field type = the local's type.
- SHARE: `_lower_share_capture`.
- else (COPY / plain): `env_val = self.lower_expr(expr)` where
  `expr = _expr_from_capture_key(cap.key)` (1233) rebuilds `HVar`/`HField`; field
  type = `_local_types[env_val]`.

A "reconcile" loop (4054 / 5286) then overwrites `env_field_types[i]` with
`_local_types[env_vals[i]]` when known — important land-mine, see §3.

The env struct is declared/defined (`declare_struct` + `define_struct_fields`),
`ConstructStruct`'d, and for callbacks boxed into a `std.mem.RawBuffer`. A
`HiddenLambdaSpec` (5124 / 5338) records `env_ty`, `env_field_types`,
`capture_map: {HCaptureKey→slot}`, `capture_kinds`.

### 1c. driftc.py hidden-lambda worklist

Each lambda body is re-type-checked and lowered as a hidden function by a FRESH
`HIRToMIR` instance (the worklist near driftc.py:7060–7370). Capture roots are
pre-seeded so the body type-checks and lowers with the right binding identities:
- `capture_id_map` remaps root binding ids into the hidden fn's id space;
  `remapped_capture_map` rebuilds each `HCaptureKey` with the new root but the
  **same `proj`** (7081–7084). Good: keys stay projection-carrying.
- `preseed_binding_types[bid]` / `preseed_binding_names[bid]` are filled from the
  ORIGIN function's `binding_types[root]` / `binding_names[root]` (7096–7128).
- After type-checking, the fresh `HIRToMIR` gets `_lambda_capture_slots =
  remapped_capture_map`, `_lambda_env_field_types` (7271–7284),
  `_lambda_capture_kinds`, `_binding_names`, and root-named `_local_types` seeds
  (7293–7299, 7356–7367).

### 1d. Prologue + body reads

`_emit_lambda_capture_prologue` (5416) iterates slots, and for each makes a
body-visible local named after `_binding_names[root]` (`_canonical_local`),
stores the loaded slot into it, and registers a drop (except MOVE/SHARE on
callbacks, 5449/5466). Body reads of the whole local (`HVar` root) hit the fast
path at `_visit_expr_HVar`:3022 → `_load_capture_from_env(slot)`.

For a whole-local capture this is coherent: the slot type == the local type ==
the root binding type, so the root-keyed derivation in 1c/1d is correct.

---

## 2. Where it diverges for a projection (the actual bug)

For `p.count` the capture key is `HCaptureKey(root=p, proj=(count,))`. Verified
by instrumentation on the repro program (immediate-call, REF-kind):

```
IMM lam captures: [('REF', p, ('count',))]
outer use_it MIR:
   AddrOfField(dest=.t3, base_ptr=&p, struct_ty=Prepared, field_index=0, field_ty=Int)
   ConstructStruct(dest=.t4, struct_ty=__lambda_env_use_it_0_0, args=[.t3])
HiddenLambdaSpec env_field_types at creation: [Ref<Int>]   # 899 = &Int  ✅ correct
```

So outer lowering correctly stores `&p.count` and records the slot as `&Int`.
But the FINALLY-lowered hidden lambda uses the WRONG slot type:

```
__lambda_use_it_0_0 MIR (final):
   StructGetField(..., field_index=0, field_ty=Prepared)   # ❌ should be &Int
   LoadRef(inner_ty=Prepared)                               # ❌ derefs a struct as a ptr
   ...
   BinaryOpInstr(ADD, left=<Prepared value>, right=<Int 1>) # → codegen crash
   local_types: {'p': Prepared, ...}                        # ❌ bogus root-named local
```

The slot type flipped from `&Int` to `Prepared` between spec creation
(§1b) and hidden-lambda lowering (§1c). The culprit is the driftc.py worklist:

- **7096–7128**: `cap_ty = origin_typed.binding_types.get(orig_bid,...)` — the
  ROOT binding's type = `Prepared`. The only override to the env-slot type
  `spec.env_field_types[slot]` is gated on `has_typevar(cap_ty)` or
  `UNKNOWN` (7105). `Prepared` is a concrete struct, so the override is skipped
  and `preseed_binding_types[p] = Prepared`.
- **7271–7284**: starts from the correct `spec.env_field_types` (`[&Int]`), then
  for each capture does `env_field_types[slot] = preseed_binding_types[root]`
  (7283), i.e. `= Prepared`, skipping only `UNKNOWN` (7281). **This overwrites
  `&Int` with `Prepared`.** This is the proximate cause of the crash.
- **7293–7299 / 7356–7367**: seed `_local_types["p"] = Prepared` for the body.

Then `_load_capture_from_env` (1297): `field_ty = env_field_types[slot]` =
`Prepared`; kind REF + `ref_is_value` (1305) does `td = get(Prepared)` which is
STRUCT not REF, so no unwrap, but STILL emits `LoadRef(ptr=field_val,
inner_ty=Prepared)` — treating the loaded struct as a pointer. Garbage → crash.

Key point: the read interceptor at `_visit_expr_HField`:3499 DID fire (the body
`p.count` matched the slot and went through `_load_capture_from_env`). The read
path was never the problem — it was fed a corrupted slot type.

---

## 3. The reconcile-loop land-mine (why outer lowering is only *accidentally* right)

Outer lowering's reconcile loops (4054 / 5286) overwrite `env_field_types[i]`
with `_local_types[env_vals[i]]`. For the REF branch, `env_vals[i]` is the
`AddrOfField` pointer `.t3`. If `_lower_addr_of_place` recorded
`_local_types[.t3]` as the pointee (`Int`) or base (`Prepared`) rather than
`&Int`, the reconcile could corrupt the slot at the OUTER site too. In the repro
it happened to stay `&Int` (spec showed `[&Int]`), so outer lowering is correct
here — but any Copy-feature work must re-verify this loop for the COPY branch
(5051–5053), where `env_val = lower_expr(HField(p,count))` and
`_local_types[env_val]` should be `Int`. This loop is the place a projected COPY
slot's type is established; treat it as load-bearing.

**This is TYPE correctness only.** It says nothing about whether `env_val`
itself is an owned value or an aliased view for a non-bitcopy field — that is
a separate, REQUIRED fix, not a re-verification footnote. See §4e.1.

---

## 4. Design: making projected captures work end-to-end

### 4a. Chosen mechanism for the body-visible binding

**Do NOT rewrite the body HIR (reject task approach (a)).** The body's
`p.count` `HField` already resolves to the env slot via the key-based read
interceptors (§0.2). The correct mechanism is the opposite of adding a binding:

> **For any capture whose `key.proj` is non-empty, do NOT materialize a
> root-named body-visible local at all. The body reads the projection directly
> from the env slot through the existing key-based interceptors.**

Concretely:
- `_emit_lambda_capture_prologue` (hir_to_mir.py:5416): add, at the top of the
  per-slot loop, `if key.proj: continue`. No `StoreLocal`, no
  `_register_drop_local`, no root-named local for projected slots. This alone
  removes the bogus `p` local AND the two-projections-same-root name collision
  (both projections would otherwise `_canonical_local(root,"p")` to the same
  name).
- The env slot must be typed as the FIELD's captured type (the value type for
  COPY, `&field`/`&mut field` for REF/REF_MUT). Fix in driftc.py (§4c).

This is HIR node type-agnostic: value reads only ever arrive as `HVar`/`HField`
(there is **no** `_visit_expr_HPlaceExpr`; dispatch is by class name at
`_lower_expr_raw`:2550, and `HPlaceExpr` only appears in place contexts — assign
target, borrow subject, move subject — all routed through `_lower_addr_of_place`
/ `_capture_key_for_expr`, which are projection-aware). So no per-shape body
handling is needed (task item 5 resolved: only `HField` reaches value lowering;
`HPlaceExpr` reaches place lowering; both are already key-aware).

### 4b. stage1 + checker: which projected captures to allow

Split by final kind and field Copy-ness:

| capture kind × proj | today | target |
|---|---|---|
| REF / REF_MUT, proj≠() | accepted by discovery, **crashes at codegen** | make it work (fix metadata + prologue) |
| MOVE, proj≠(), field Copy | rejected (capture_discovery.py:487) | **downgrade to COPY**, make it work |
| MOVE, proj≠(), field non-Copy | rejected (487) | **keep rejected** (partial move-and-zero-back of a field is unimplemented; this is the UAF) |
| COPY, proj≠() (explicit `captures(copy ...)`) | N/A — explicit caps always have `proj=()` (see 4946/5185) | out of scope; explicit caps cannot be projected |

The Copy downgrade needs the FIELD type, which `discover_captures` cannot see
(no type table). So keep discovery's job to *classify*, and move the
proj-specific decision to the borrow checker, which has types:

- `capture_discovery.py:487–495`: NARROW the blanket rejection. It must keep
  rejecting non-Copy MOVE+proj, but it has no types. Options:
  (i) leave the reject in place and have the checker special-case Copy — but the
  reject runs first (discovery is called from the checker's
  `_check_lambda_captures` at 589 and also standalone), so a rejected capture
  never reaches the downgrade. Therefore the discovery-time reject must be
  **removed** and replaced by a checker-side gate; OR
  (ii) keep a *provisional* MOVE+proj marker and let the checker resolve it.
  Cleanest is (i): drop the 487 reject, and in
  `borrow_checker_pass._check_lambda_captures` (587) add: for a capture with
  `cap.kind is MOVE and cap.key.proj`, compute the field type via
  `self._type_of_place(self._place_from_capture_key(cap.key))` (helpers at
  borrow_checker_pass.py:249 and 832), then `_is_copy` (693): if Copy, set
  `cap.kind = C.HCaptureKind.COPY`; else emit the existing "lambda move captures
  of projections are not supported yet" diagnostic. This is exactly the reverted
  prototype's checker logic — it was never wrong; it failed only because the
  lowering metadata/prologue (§4c/§4a) was still root-keyed.

- The MIR-lowering assertions that backstop MOVE+proj
  (hir_to_mir.py:5013 and 5245) stay as-is: after the checker downgrade, no
  MOVE+proj capture should reach lowering, so the assertion remains a valid
  defense-in-depth for the still-unsupported non-Copy case (which errors out in
  the checker before lowering).

Note the existing checker COPY-copyability check at
`_check_lambda_captures`:598 ALSO reads `binding_types[cap.key.root_local]` (the
root type) — for an explicit `captures(copy p)` that is right, but the new
downgrade path must use `_type_of_place` (field type), not the root type.

### 4c. driftc.py worklist: type the projected slot by the field, not the root

Five edits (was two — reviewer found two more, items 4-5), all in the
hidden-lambda worklist:

1. **Do not overwrite a projected slot's env type with the root type**
   (7271–7284). Guard the `env_field_types[slot] = cap_ty` overwrite (7283) on
   `not cap.key.proj`. For a projected key, trust `spec.env_field_types[slot]`
   (already the correct field/ref type from outer lowering, §1b/§3). Equivalent
   alternative: compute the projected field type by walking
   `origin_typed.binding_types[root]` through `cap.key.proj` (mirror
   `_type_of_place`) and use THAT — but trusting the spec is simpler and avoids
   re-deriving `&field` wrapping for REF/REF_MUT.

2. **Do not seed a root-named `_local_types` for a projected key** (7293–7299
   and 7356–7367). Guard those seeds on `not key.proj`. Harmless if left (the
   prologue skip in §4a means no root local is emitted, and the body never reads
   bare `p` unless it was separately captured), but cleaner to skip so a stray
   root read cannot silently pick up a whole-struct type.

3. `preseed_binding_types[root] = Prepared` (7125) can STAY — the type CHECKER
   wants the whole-struct root type so that `p.count` type-checks as a field
   access (task item 3, resolved below). It only becomes harmful when copied
   into `env_field_types` (fixed by edit 1).

4. **(Medium, reviewer finding) Guard the candidate-slot-type preseed
   fallback too** (7096-7128, specifically 7104-7106):
   ```python
   cap_ty = origin_typed.binding_types.get(orig_bid, shared_type_table.ensure_unknown())
   if spec.env_field_types is not None:
       slot = remapped_capture_map.get(cap.key)
       if slot is not None and slot < len(spec.env_field_types):
           candidate = spec.env_field_types[slot]
           if shared_type_table.has_typevar(cap_ty) or shared_type_table.get(cap_ty).kind is TypeKind.UNKNOWN:
               cap_ty = candidate          # <-- WRONG for a projected key
   ...
   preseed_binding_types[bid] = cap_ty
   ```
   This is a SEPARATE code path from item 1 (7271-7284) — it runs earlier, and
   feeds `preseed_binding_types[bid]`, which item 1 is guarded to leave alone
   for the root's own type-check preseed (item 3, "can STAY"). But if the
   ROOT's own origin type (`cap_ty` from `origin_typed.binding_types`) happens
   to be a typevar/Unknown (a real, if narrower, scenario — e.g. a generic
   context where the root's type wasn't fully concretized at the capture
   site), this fallback substitutes `candidate = spec.env_field_types[slot]` —
   which, for a projected key, is the FIELD's type (`Int`/`&Int`), not a valid
   stand-in for the ROOT's type. That would preseed the body-visible root name
   `p` as type `Int` instead of `Prepared`, corrupting the SAME root type-check
   preseed that item 3 deliberately preserves. **Fix: guard this substitution
   on `not cap.key.proj` too** — for a projected key with an unresolved root
   type, either leave `cap_ty` as Unknown (matches the previous, working,
   whole-local-only behavior) or resolve the root's type some other way; do
   NOT borrow the field's type.

5. **(Low, reviewer finding) `_lambda_capture_name_to_slot` is root-name keyed
   and picks one slot arbitrarily for multiple projections** (7286-7291):
   ```python
   name_to_slot: dict[str, int] = {}
   for key, slot in remapped_capture_map.items():
       name = preseed_binding_names.get(int(key.root_local))
       if name:
           name_to_slot[name] = slot      # <-- last write wins across ALL
                                           #     projections of the same root
   lower._lambda_capture_name_to_slot = name_to_slot
   ```
   Consumed at hir_to_mir.py:3041-3044 as a fallback for a bare `HVar` read
   with NO resolved `binding_id` (`expr.binding_id is None`) — narrower than
   the main by-key path (3022-3024) or `_visit_expr_HField`'s interceptor
   (3497-3500), but still live: for `p.count` AND `p.execute` captured
   together, this dict has one entry `"p" -> <whichever slot iterated last>`.
   If a binding-id-less bare `HVar(name="p")` node is ever encountered (an
   edge case, but the code path exists and is reachable per its own guard), it
   silently resolves to the WRONG projected slot instead of failing to find a
   legitimate whole-`p` capture. **Fix: skip registering a `name_to_slot` entry
   for any capture with non-empty `key.proj`** — a bare-name fallback lookup
   must never resolve to a projected slot; per §6, a body that legitimately
   uses bare `p` always has its OWN `{p,()}` capture with its own slot, which
   this loop should register instead (when both a `{p,()}` and a `{p,(field,)}`
   capture of the same root exist, only the former belongs in this dict).

### 4d. `_load_capture_from_env` and read interceptors

Once `env_field_types[slot]` is correct, this is correct **only for bitcopy
fields (e.g. `Int`)**. For non-bitcopy fields (`String`, a refcounted struct),
see §4e — this is NOT a "no change needed" case, contrary to what an earlier
draft of this doc claimed.

- COPY projected, bitcopy (`Int`): `field_ty = Int`, kind COPY →
  `_load_capture_from_env` returns `field_val` directly (no ref unwrap, since
  kind∉{REF,REF_MUT}). Correct — nothing to alias.
- COPY projected, non-bitcopy (e.g. `String`, which IS Copy per
  `copy_status()` but retains refcount on copy — "Copy ≠ no-destructor"):
  **broken, see §4e.1.**
- REF projected: `field_ty = &Int`/`&String`, kind REF + `ref_is_value` →
  `LoadRef` unwrap. For a bitcopy inner type this is fine. For a non-bitcopy
  inner type (e.g. `&String`, `&SomeStruct`): **broken, see §4e.2.**
- `_visit_expr_HField`:3499 returns `_load_capture_from_env(slot)`.
- `_infer_expr_type` HField:11428 returns the (now-correct) field/unwrapped type.

### 4e. Ownership alias handling — REQUIRED, not optional (reviewer's two High findings)

The metadata fix (§4c) makes the SLOT TYPE correct. It does **not** make the
VALUE correct for anything beyond a plain bitcopy scalar. Two independent gaps,
both verified directly in the code:

#### 4e.1 (High) — COPY branch env construction never calls `_copy_if_ref_alias`

At the COPY-branch env-construction site (hir_to_mir.py:5051/5283 — the `else:`
arm of the per-capture loop in `_lower_lambda_immediate_call`/
`_lower_lambda_callback`):

```python
else:
    env_val = self.lower_expr(expr)      # expr = HField(p, "field")
    env_vals.append(env_val)             # <-- stored into the env with NO
                                          #     _copy_if_ref_alias() call
    env_field_types.append(...)
```

`_copy_if_ref_alias(value, ty)` (hir_to_mir.py:828-…) is the general mechanism
that "must be called at every ownership-transfer boundary (struct/variant
construction, return, variable binding, call args)" (its own docstring) — it
is a no-op unless `value` is in `self._ref_field_temps`, in which case it emits
a real `CopyValue` (deep, retained copy) instead of forwarding an aliased view.
Every OTHER ownership-transfer boundary already calls it (struct/variant
construct at 4747/4751, return at 9076/9123, assignment at 8704/8757, call args
at 10044). **Env construction — storing a captured field's value into the
heap-allocated closure env, which is exactly as much an ownership-transfer
boundary as a struct construction — does not.**

Whether this actually bites depends on whether `lower_expr(HField(p,"field"))`
marks its result in `_ref_field_temps` in the first place. Tracing
`_visit_expr_HField` (3496-…): its FIRST check (3497-3500) is the
capture-slot interceptor itself — `if self._lambda_capture_slots is not None:
... return self._load_capture_from_env(...)`. On the OUTER lowering instance
(the one doing env construction), `self._lambda_capture_slots` reflects
whatever capture context THAT instance is itself in (`None` for a plain `fn`,
or the OUTER lambda's own slots if this is a doubly-nested closure — i.e.
exactly the "closure built inside another closure" shape from the PRIMARY UAF
bug). So:
- **Simple case (outer function is a plain `fn`):** `_lambda_capture_slots is
  None`, the interceptor is skipped, and the plain (non-array-index) field
  path further down in `_visit_expr_HField` runs. That path's non-bitcopy
  `_ref_field_temps.add(dest)` marking (mirroring the array-index sub-branch
  already shown at 3556-3557) needs to be confirmed present for the "sub_def
  is REF" and plain-struct-field sub-cases too — **verify this specifically**
  (I traced the array-index sub-branch in detail; the plain field sub-case
  past line 3559 needs the same confirmation before assuming `_ref_field_temps`
  gets set at all in the simple case).
- **Nested case (outer function is itself a captured lambda):** the
  interceptor at 3497-3500 fires FIRST and returns `_load_capture_from_env(...)`
  directly — bypassing the plain-field-path `_ref_field_temps` marking
  entirely. This return value's alias-safety then depends on §4e.2 (below)
  having already marked it when the OUTER capture was itself loaded.

**Required fix:** regardless of what `lower_expr` does internally, the env-
construction COPY branch must call
`env_val = self._copy_if_ref_alias(env_val, field_ty)` before
`env_vals.append(env_val)` — exactly like every other ownership-transfer
boundary. This is the belt to the read-side's suspenders (§4e.2); do both, not
either.

#### 4e.2 (High) — `_load_capture_from_env` never marks REF-projected reads as aliases

`_load_capture_from_env` (hir_to_mir.py:1297-1313), REF/REF_MUT + `ref_is_value`
branch:

```python
if kind in (C.HCaptureKind.REF, C.HCaptureKind.REF_MUT) and self._lambda_capture_ref_is_value:
    ...
    dest = self.b.new_temp()
    self.b.emit(M.LoadRef(dest=dest, ptr=field_val, inner_ty=inner_ty))
    return dest          # <-- NOT added to self._ref_field_temps
```

Compare to the general deref path (3395-3412), which is otherwise IDENTICAL
(`LoadRef` into a fresh temp) but follows it with:
```python
if not self._drop_policy(inner_ty).is_bitcopy:
    self._ref_field_temps.add(dest)
```
`_load_capture_from_env` has no equivalent line. So a REF-projected capture of
a non-bitcopy field (`&String`, `&SomeStruct`) loads a SHALLOW VIEW into the
lambda body with no alias marking. If the body then RETURNS it, BINDS it to a
`val`/`var`, PASSES it as a call arg, or STORES it in a struct — all of which
route through the ownership-transfer-boundary `_copy_if_ref_alias` calls that
already exist at those sites — none of them will deep-copy it, because it was
never flagged as needing one. The body-side consumer treats it as an already-
owned value; when BOTH the lambda's view and the original struct's field get
dropped, that is a double-free of the same backing allocation. This is the
SAME failure class as the primary UAF fix (double-drop via un-tracked
aliasing), reachable through a different code path.

**Required fix:** in `_load_capture_from_env`'s REF/REF_MUT branch, add
`if not self._drop_policy(inner_ty).is_bitcopy: self._ref_field_temps.add(dest)`
— mirroring the deref path exactly, right before `return dest`.

#### Consequence for scope and framing

These two fixes are not "harden it later" — they are required for the SAME
correctness property the primary UAF fix protects (no aliased view escapes an
ownership boundary undetected). Do not land Copy-projected-capture or
REF-projected-capture support without both. §7's file/line estimate and test
matrix are updated below to include them.

---

## 5. Type inference impact (task item 3)

**Minimal to none.** The hidden lambda body is re-type-checked by
`type_checker.check_function` (driftc.py:7155) with `preseed_binding_types[root]
= whole struct`. Inside the body, `p.count` type-checks as a normal field
projection on `p:Prepared` → `Int`. The checker does not need to know "`p.count`
is the captured unit"; it only needs `p`'s type, which is present. So keep the
root preseed (§4c.3). The capture GRANULARITY is decided by discovery, not by the
checker's view of `p`.

One consequence to accept deliberately: with `p` preseeded as a whole binding,
the checker will NOT reject a body that also mentions `p.other_field` or bare
`p` — but that's fine, because discovery would then record ADDITIONAL captures
for those uses (§6). The checker's whole-struct view and discovery's
per-projection capture set stay consistent.

---

## 6. "p must not be usable as a fake whole struct" (task item 4)

Discovery is **usage-driven**, so this is enforced structurally, not by a check:

- `_walk_expr` records a capture for exactly what the body references:
  bare `HVar p` → `_add_usage(p, [])` → key `{p,()}` (163); `p.count` →
  `_flatten_field_chain` → `_add_usage(p, [count])` → key `{p,(count,)}`
  (165–169). So a body that only ever touches `p.count` produces ONLY the
  `{p,(count,)}` capture; there is no `{p,()}` slot, hence no whole-`p` value
  anywhere. Bare `p` is simply un-referenceable without creating its own
  capture.

- **Two projections of the same root** (`p.count` AND `p.execute`): two distinct
  usage entries → two distinct keys → two slots. The read interceptors key on
  the full key, so each resolves to its own slot independently. The design in
  §4a (prologue skips ALL projected slots) is REQUIRED here — otherwise both
  slots would `_canonical_local(p,"p")` to the same local name and the second
  `StoreLocal` would clobber the first. So yes, the design must (and does)
  handle N independent projected captures of one root simultaneously; the fix is
  free because "don't emit a root local for projections" covers all N.

- **Bare `p` AND `p.execute` together**: keys `{p,()}` and `{p,(execute,)}`. The
  `_overlaps` check (498–517) fires (proj-prefix overlap); it is allowed ONLY if
  both are REF (509–510), else "overlapping lambda captures are not supported
  with mutable or move captures". When both are REF: `{p,()}` gets a legit
  root-named body local (whole struct), `{p,(execute,)}` is a projected slot
  (skipped by the prologue, read via interceptor). Consistent — but note the
  prologue skip must key on `key.proj`, so the `{p,()}` slot still materializes
  its local while `{p,(execute,)}` does not.

No new diagnostic is needed for "used the uncaptured root": it is impossible to
reference the root without capturing it.

---

## 7. Scope estimate, files, land-mines

**Framing (revised): "projected capture lowering plus ownership alias
handling," not just metadata/prologue guards.** Still a contained multi-file
fix, NOT a new pass — but two of the five file-level changes are
correctness-required alias bookkeeping (§4e), not hardening. Roughly:

- `lang/driftc/stage1/capture_discovery.py` (~10 lines): remove/narrow the
  MOVE+proj blanket reject at 487–495 (move the decision to the checker).
- `lang/driftc/borrow_checker_pass.py` (~15 lines in `_check_lambda_captures`,
  587): for MOVE+proj, resolve field type via `_type_of_place` /
  `_place_from_capture_key`, downgrade to COPY if `_is_copy` else emit the
  existing rejection. (This is the reverted prototype, now with working
  lowering behind it.)
- `lang/driftc/driftc.py` (~12 lines, up from ~6): guard FOUR sites on
  `not cap.key.proj`/`not key.proj`, not two:
  1. `env_field_types[slot] = cap_ty` overwrite (7283).
  2. Root-named `_local_types` seeds (7293–7299, 7356–7367).
  3. **(Medium, new)** the candidate-slot-type preseed fallback (7104-7106) —
     see §4c item 4.
  4. **(Low, new)** `_lambda_capture_name_to_slot` registration (7286-7291) —
     see §4c item 5.
- `lang/driftc/stage2/hir_to_mir.py` (~10 lines, up from ~2):
  1. `if key.proj: continue` at the top of `_emit_lambda_capture_prologue`'s
     per-slot loop (5416).
  2. **(High, new)** COPY-branch env construction: `env_val =
     self._copy_if_ref_alias(env_val, field_ty)` before `env_vals.append(...)`
     at both 5051 and 5283 — see §4e.1.
  3. **(High, new)** `_load_capture_from_env`'s REF/REF_MUT branch: add
     `self._ref_field_temps.add(dest)` for non-bitcopy `inner_ty`, mirroring
     the deref path at 3410-3411 — see §4e.2.
  4. Verify (not necessarily change) whether `_visit_expr_HField`'s plain
     (non-array-index) field-read path already marks `_ref_field_temps` for a
     non-bitcopy field the same way the array-index sub-branch does
     (3556-3557) — needed to know whether 4e.1's outer-lowering `lower_expr`
     call produces an already-marked temp in the simple (non-nested-closure)
     case, or whether that's a THIRD gap. Flagged, not resolved, in §4e.1.
- Tests: see expanded matrix below (was 4 cases, now 6 — reviewer's ask).

**Land-mines (renumbered — former item 3 promoted to §4e.2, a required fix,
not a land-mine):**

1. **The driftc.py capture-id remap** (`capture_id_map`, 7081–7085) preserves
   `proj` when rebuilding keys — good; do not break it. The `_remap_ids` /
   `_scan_binding_ids` passes (6460–6621) rewrite body binding ids to the hidden
   fn space; the remapped body `HField(p',count)` must still flatten to the
   remapped key. Verified consistent for whole-local captures; re-verify for
   projected `HField` bodies specifically.
2. **The reconcile loops** (4054 / 5286) at the OUTER site (§3) — the projected
   COPY slot's type is established there via `_local_types[lower_expr(HField)]`.
   Confirm it is the field type (`Int`), not the base or `&base`.
3. **Prologue drop registration** (5466): today it registers a drop for REF/COPY
   slots. With projected slots skipped entirely (§4a) no drop is registered for
   them — correct for Copy (bitcopy, no drop) and for REF (borrow, no drop). Do
   NOT "helpfully" re-add a drop for projected slots.
4. **SHARE captures** read `explicit_captures`/`share_value` and always have
   `proj=()` (explicit caps are never projected). The prologue's `key.proj`
   skip does not touch them. No interaction.

### Test matrix (expanded per reviewer — was 4 cases, now 6)

1. **Current REF-projected `p.count + 1` crash** (regression for the codegen
   crash this doc opened with) — the §8 reproduction recipe, formalized as a
   driver test. Must go from `NotImplementedError` to compiles+runs.
2. **Copy-typed projected field, boxed callback, positive** — `p.count: Int`
   read implicitly inside `core.callback0(...)` (the shape
   `test_copy_typed_projected_field_also_currently_rejected` currently pins as
   REJECTED) must FLIP to compile+run correctly once this lands.
3. **Non-Copy MOVE-projected, negative** — `p.execute` (existing
   `test_implicit_projected_move_capture_into_boxed_callback_rejected`) MUST
   STAY rejected; this fix must not accidentally widen acceptance to non-Copy
   fields.
4. **Two projections from one root** — a lambda body using BOTH `p.count` AND
   `p.execute` (or two Copy fields) in the same body; proves the prologue skip
   doesn't collide two slots under one root-named local (§6).
5. **Bare root plus projection** — a lambda body using both bare `p` (whole
   struct, REF kind) and `p.execute` (projected); proves the `_overlaps`
   REF/REF logic and that the root gets a real local while the projection
   doesn't (§6, third bullet).
6. **Non-bitcopy projected field — alias/CopyValue proof** — a projected
   field whose type is Copy-but-non-bitcopy (`String`) or a non-Copy REF
   projection that gets returned/bound/passed/stored from the lambda body;
   must prove EITHER (a) `_copy_if_ref_alias`/`_ref_field_temps` correctly
   produces an independently-owned value with no aliasing (via memcheck/ASAN,
   not just compile success), OR (b) this specific shape is intentionally
   still rejected if full alias support isn't landed in the same patch. Do
   NOT ship this case silently uncovered — it is exactly the class of bug
   the primary UAF fix exists to prevent.

**(Low, reviewer finding) `String`-is-Copy caveat — verify test infra before
relying on this in case 2 or case 6.** `copy_status()` (types_core.py:2567-)
has TWO authorities, and they DISAGREE about `String`:
- **With the stdlib's Copy-trait query installed** (`self._copy_query` set —
  true for any real driver/e2e compile that loads the stdlib, which registers
  `String`'s Copy impl per `project_string_constshare_transitional.md`):
  `query(String_tid)` returns `True` (line 2741-2746) — retain-copy, correctly
  Copy.
- **With NO query installed** (an isolated unit test constructing a bare
  `TypeTable` directly, not through a full compile): falls straight to
  `_is_copy_structural(ty)` (line 2769), whose SCALAR-kind branch explicitly
  special-cases `String` to `False` (`if td.kind is TypeKind.SCALAR and
  td.name == "String": return False`) — i.e., in this context `String` is
  classified NOT Copy.

**Consequence:** case 2 (Copy-typed projected field positive) and case 6
(non-bitcopy alias proof) MUST be driver/e2e tests compiled through the real
pipeline with the stdlib loaded (matching this repo's existing driver-test
convention — see `lang/tests/driver/test_boxed_callback_projected_move_capture_rejected.py`,
which already compiles via `lang.driftc.driftc` end to end) — not
stage1/stage2 unit tests that hand-construct an isolated `TypeTable`. If a
lower-level unit test is ever wanted for this specific Copy-classification
boundary, use an explicit user-defined fixture
(`implement core.Copy for Wrapper { inner: String }`) that goes through the
SAME trait-query path as any other user type, rather than assuming bare
`String`'s Copy-ness is consistent across both authorities.

---

## 8. Reproduction recipe (for whoever picks this up)

Clean `main`, immediate-call REF-projected capture crashes at codegen:

```
source .venv/bin/activate
cat > /tmp/imm2.drift <<'EOF'
module main;
struct Prepared { count: Int, }
fn use_it(p: Prepared) -> Int {
	return (| | => { return p.count + 1; })();
}
pub fn main() nothrow -> Int {
	val p = Prepared(count = 41);
	if use_it(p) == 42 { return 0; }
	return 1;
}
EOF
python3 -m lang.driftc.driftc --dev --stdlib-root stdlib /tmp/imm2.drift \
	--entry main::main -o /tmp/imm2
# => NotImplementedError: integer binop requires matching Int/Uint operands
#    (have %Struct_main_Prepared_..., drift.int)
```

To inspect the corrupted slot type vs the correct spec type, instrument
`HiddenLambdaSpec` creation (prints `env_field_types=[Ref<Int>]`) and dump the
final `__lambda_use_it_0_0` MIR (uses `field_ty=Prepared`); the divergence is
introduced by driftc.py:7283 as described in §2.

The MOVE variant (boxed `core.callback0`, field read defaults to MOVE) is the
one currently rejected at capture_discovery.py:487 and covered by
`test_boxed_callback_projected_move_capture_rejected.py`.
