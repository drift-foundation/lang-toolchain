# Research: reference-typed callback generic args — are they unsound, and if so when?

Scope: RESEARCH ONLY. No code changed. Prep for a possible future fix while the
primary UAF fix (`fix/callback-env-uaf-ref-args`, commit `dee458cc`) is in cert
review. All file:line references are against the tree at that commit.

## TL;DR

- Passing `&T` **arguments** to a boxed callback's `.call(&x, &y)` is **sound**.
  At the ABI/MIR level a ref arg is lowered to a raw pointer and passed
  positionally through the vtable-dispatched call exactly like any ordinary
  `fn f(a: &T)` — the pointer only needs to be valid for the (synchronous)
  duration of the call. This is why the immediate repro (`"field-" + *payload`,
  return a struct) compiled and ran clean under ASAN/UBSAN.
- There is **no REF gate** when a `CallbackN<..>`/`CallbackThrowN<..>`
  instantiation's type args are turned into the lambda's expected function
  shape (`call_resolver.py` and `type_checker._expected_function_shape`). The
  earlier finding is confirmed: refs are accepted as callback param types with
  no special validation. That absence is **correct** for the common case.
- The existing "closures with borrowed captures are non-escaping in v0" guard is
  about a **lambda's own explicit `captures(ref/ref_mut …)` clause**, NOT about a
  callback's declared **parameter** types. Confirmed — different code path.
- **A real, narrow gap exists (scenario 4a).** A nested, *escaping* boxed closure
  (one that is returned or `conc.spawn`-ed out of the outer callback body) that
  **implicitly captures the outer callback's `&T` parameter** copies the raw
  borrow-pointer into its heap env with **no diagnostic and no drop**. Capture
  discovery is type-blind and classifies the read of a `&T` param as a whole-value
  **MOVE** capture (for boxed callbacks); the borrow-checker's escape machinery
  only tracks **REF/REF_MUT** captures, so the MOVE capture of a reference value
  is invisible to it → escape level computed as `STATIC` (freely escapable) → a
  **dangling pointer** (use-after-scope), not a double-free. This matches the
  reviewer's "refs crossing a thread/static boundary via a returned/spawned
  callback" hypothesis. It is distinct from — and not covered by — the primary
  UAF fix, which is about implicit projected MOVE captures of *owned* fields.
- Recommended fix is **type-aware and narrow**: a direct diagnostic in
  `borrow_checker_pass.py::_check_lambda_captures` for an escaping capture whose
  root is reference-typed — NOT routed through the existing REF/REF_MUT
  loan-escape machinery (see reviewer correction in §6). It does **not** touch
  the existing `Callback2<&Req, &mut Ctx, R>` web/rest synchronous-dispatch
  pattern.
- **Reviewer correction applied (2nd pass):** the mechanism (capture
  classification, escape-level blindness) is confirmed by reading the code; the
  END-TO-END runtime behavior is **not yet empirically confirmed**. An attempt
  to compile a `conc.spawn`-based escape repro (avoiding the known
  interface-return codegen gap) hit an unrelated internal compiler error
  (`RuntimeError: SSA: load before store for local '__b8'`) before reaching
  either "compiles clean" or "hits a known blocker" — see §7 (new). This is
  NOT the same failure as the primary UAF fix's target, and is not yet
  root-caused. Do not treat 4(a) as confirmed-reachable until a repro either
  compiles clean (exposing the silent gap) or is deliberately traced through
  this ICE.

---

## 1. Existing positive coverage — exactly how ref-typed callback params are used today

### 1.1 Where it lives

`lang/tests/driver/test_product_shape_consumer_patterns.py` is the primary
positive-coverage file for `Callback2<&Req, &mut Ctx, R>` / `Callback3<&Req, …>`
web/rest-style dispatch (it mirrors `web-rest`'s onion-fold middleware composer).
Other files that instantiate ref-typed callbacks:

- `lang/tests/driver/test_implicit_callback_wrap.py` — `CallbackThrow2<&Req, &mut Int, Resp>`, `Callback2<&Req, &mut Int, Int>`, overloaded `take_cb(CallbackThrow2<&ReqA, &mut CtxA, RespA>)`.
- `lang/tests/driver/test_cross_module_callback_named_fn.py` — nested `Callback3<&Int, &mut Int, Callback2<&Int, &mut Int, Result<…>>, Result<…>>`.
- `lang/tests/driver/test_pretty_type_name_diagnostic_rendering.py`, `test_product_shape_consumer_patterns.py` — signature pinning / diagnostics.

### 1.2 The key axis (reviewer's question): synchronous vs. stored?

I read `test_product_shape_consumer_patterns.py` end-to-end. Two important facts:

1. **Most of the ref-callback coverage is compile-only.** The tests use
   `_compile_source(...)` + `_assert_clean(rc, errs, …)` (file lines ~280–347,
   ~389, ~438, ~532, ~656, ~703, ~826). `_compile_source` invokes the compiler
   and only inspects diagnostics — it does **not** run the produced binary. Only
   two tests actually execute code:
   - Mode **M-D** (`subprocess.run` at file line ~771) compiles+links+runs `main`.
   - Mode **M-E** (`test_mw_callback3_emit_package_producer`, ~line 789) emits a
     signed `.dmp` and round-trips a consumer *compile* (still no invocation of a
     stored callback).

2. **The `register_mw` / `register` tests DO store the callback** in an
   `Array<Callback3<&Req, &mut Ctx, …>>` slot (`slot.push(move cb)`,
   file lines 251–276, 364–371, 721–733) — i.e. the "stored in a route table for
   later invocation" shape the reviewer flagged. **But these are compile-only**:
   the stored callback is never actually `.call()`-ed at runtime with fresh refs.
   They prove the *type/serialization* survives being stored, not the runtime
   behavior of a later invocation.

3. **Where a ref-callback is actually invoked, it is invoked synchronously**
   within one call frame. The representative body (file lines 265–274, and the
   captures-mix composer at 306–320):

   ```drift
   register_mw(&mut slot, |req, ctx, next| => {
       val m: String = req.method.clone();          // ref param used synchronously
       ctx.idx = ctx.idx + 1;                        // &mut param mutated synchronously
       val inner: core.Result<Resp, AppErr> = next.call(req, ctx);  // forwards the SAME refs
       return move inner;                            // returns an owned VALUE, not a ref
   });
   ```

   The `&Req`/`&mut Ctx` params are read/mutated and forwarded to `next.call(...)`
   **within the body**; the body returns an **owned value** (`Result<Resp,AppErr>`),
   never a reference, and never builds a nested closure that captures `req`/`ctx`
   and escapes.

**Characterization:** the existing positive coverage exercises the *synchronous*
use of ref params (and the *storage of the boxed value itself*), but **never**
exercises a callback body that captures its own `&T` parameter into a nested
closure that then escapes. That untested corner is exactly where the gap is.

---

## 2. How ref-typed generic args are validated (or not)

### 2.1 `call_resolver.py` — building the lambda's expected function shape

`lang/driftc/checker/call_resolver.py`:

- The callback family is table-driven from `_CALLBACK_ROWS` (lines 84–95) →
  `_CALLBACK_KIND_BY_IFACE` / `_CALLBACK_KIND_BY_INTRINSIC` (lines 111–120).
- In the `core.callbackN(lambda)` resolver (lines ~5216–5253), once the expected
  `Callback{N}<P1,…,PN,R>` instance is known:

  ```python
  # call_resolver.py ~5248
  param_types = list(inst.type_args[:want_args])   # <-- P1..PN taken verbatim
  ret_ty = inst.type_args[want_args]
  arg_expected_type = ctx.type_table.ensure_function(param_types, ret_ty, can_throw=is_throw)
  ```

  **Confirmed: there is NO check that any `Pi` is `TypeKind.REF`.** The type args
  are taken verbatim and handed to `ensure_function`. Ref params are first-class
  here (and that is correct for the synchronous case).

### 2.2 `type_checker._expected_function_shape` — same gap, same intent

`lang/driftc/type_checker.py:3520–3546`:

```python
def _expected_function_shape(expected_type):
    ...
    kind = _CALLBACK_KIND_BY_IFACE.get(base_def.name)
    if kind is not None:
        arity, is_throw = kind
        if len(args) >= arity + 1:
            return list(args[:arity]), args[arity], is_throw   # <-- no REF inspection
```

**Confirmed: no REF gate here either.** Both sites intentionally accept ref
params. Neither is the right place for the fix (they run before capture/escape
analysis and would over-reject the synchronous pattern).

---

## 3. The ONE existing ref-related guard — what it actually covers

Two emission sites, same diagnostic text:

- `call_resolver.py:5218–5230` (explicit `core.callbackN(lambda)`).
- `call_resolver.py:285–316` (implicit wrap `_wrap_explicit_capture_callbacks`).
- Mirror in `borrow_checker_pass.py:569–575` (`_report_lambda_escape_if_borrowed`).

The predicate is (call_resolver.py:5224):

```python
_ec = getattr(arg_expr, "explicit_captures", None) or []
if any(getattr(c, "kind", None) in ("ref", "ref_mut") for c in _ec):
    diagnostics.append(... "closures with borrowed captures are non-escaping in v0" ...)
```

**This is about the LAMBDA'S OWN `captures(ref x)` / `captures(ref_mut x)`
clause** — a lambda that explicitly borrows an *outer local* into its env. It has
**nothing to do** with the callback's declared **parameter** types (the `A,B` in
`CallbackThrow2<A,B,R>`). A boxed callback whose lambda has **no borrow captures
at all**, but whose declared param types are references, does **not** go through
this guard. Confirmed distinct code paths.

### 3.1 ABI/MIR: how `.call(&tk, &payload)` actually passes the refs

`lang/driftc/stage2/hir_to_mir.py::_lower_iface_call` (lines 10233–10297):

```python
for idx, arg in enumerate(args):
    param_ty = info.sig.param_types[idx] if idx < len(info.sig.param_types) else None
    arg_vals.append(self._lower_call_arg(arg, param_ty))
...
self.b.emit(M.CallIface(dest=..., iface=iface_val, args=arg_vals,
                        param_types=param_types, user_ret_type=..., slot_index=slot_index))
```

Each argument — including a `&T` — is lowered by the ordinary `_lower_call_arg`
and passed positionally through the vtable-dispatched `CallIface` (or
`CallIndirect`). **A ref arg is a raw pointer, passed identically to any other
function call taking `&T`.** Nothing about the boxed/escaping nature of the
*callback value* changes how a ref *argument* is passed at a `.call()` site: the
pointer must be valid only for the synchronous duration of that call. This is
sound — same contract as `fn f(a: &T)`.

**Therefore the unsoundness, if any, is NOT at the `.call()` argument boundary.**
It can only arise if a `&T` value *outlives the call* by being stored/escaped,
which the argument boundary itself never does.

---

## 4. The actual unsoundness scenario

### 4(a) — REAL GAP: nested escaping closure captures the outer callback's `&T` param

**Mechanism, traced through code:**

1. **Capture discovery is type-blind.** `lang/driftc/stage1/capture_discovery.py`
   classifies captures purely from *usage* + the `capture_as_move` flag, never
   from the captured variable's type (lines ~450–457):

   ```python
   if use.move:            kind = MOVE
   elif use.borrow_mut:    kind = REF_MUT
   elif use.borrow_shared: kind = REF
   elif use.read:          kind = MOVE if capture_as_move else REF
   ```

   Boxed/escaping callbacks are marked `capture_as_move = True` (set in
   `call_resolver.py:5232`, `arg_expr.capture_as_move = True`). So a **plain read**
   of a free variable inside a boxed lambda becomes a **whole-value MOVE capture**.

2. **Nested lambdas are discovered independently** (capture_discovery.py:152–155):

   ```python
   def _walk_expr(e):
       if isinstance(e, H.HLambda):
           return          # "Skip nested lambdas; captures are per-lambda."
   ```

   `discover_captures` is called separately per lambda. For an **inner** boxed
   lambda, the outer callback's parameter `payload: &String` is a **free
   variable** → captured. Because the inner lambda is a boxed callback
   (`capture_as_move=True`) and reads `payload`, it is a **whole-local MOVE
   capture of the reference value `&String`** (empty projection → not caught by
   the projected-move rejection at lines ~460–484, which only fires on
   `cap.key.proj`).

3. **The escape machinery ignores MOVE captures of references.**
   `lang/driftc/borrow_checker_pass.py`:
   - `_captured_loan_binding_ids` (444–450) collects **only REF/REF_MUT** capture
     roots.
   - `_lambda_escape_level` (452–462): `if not capture_ids: return EscapeLevel.STATIC`.
     A MOVE capture contributes no capture_id → the lambda is judged `STATIC`
     (maximally escapable) → returned/spawned freely.
   - `_check_lambda_captures` (587–605) only validates **COPY** captures (must be
     `_is_copy`). A **MOVE** capture gets **no type check at all** — so moving a
     `&String` value into the env is not rejected.

4. **No drop, so no double-free — a dangling pointer instead.** A `&String` is a
   borrow, not owned/droppable, so the inner env's cleanup never drops it. When
   the inner closure is later invoked from a different frame (after a
   `conc.spawn`, or after being returned up and the original call frame unwound),
   the copied raw pointer refers to a `String` the *call site's* owner has since
   dropped → **use-after-scope read** of freed memory.

**Constructed (NOT compiled) example illustrating the path:**

```drift
// Outer boxed callback: params are refs (A=&TaskKey, B=&String).
val prepare: core.CallbackThrow2<&state.TaskKey, &String, core.Callback0<String>> =
    core.callback_throw2(|tk: &state.TaskKey, payload: &String| => {
        // Inner boxed callback. It reads `payload` (a &String PARAM of the
        // OUTER lambda). capture_as_move=True → implicit WHOLE-VALUE MOVE
        // capture of the &String pointer. No explicit captures(...) clause,
        // so the "borrowed captures are non-escaping in v0" guard never sees it.
        return core.callback0(| | => {
            return payload.clone();     // deref of a &String that may already be freed
        });
        // ^ inner closure ESCAPES (returned). Escape level computed STATIC
        //   because the only capture is a MOVE (not REF/REF_MUT). No diagnostic.
    });

// Later, at a call site:
val tk = state.TaskKey(...);
val payload = "hello".clone();
val inner = try prepare.call(&tk, &payload) catch { ... };
// `payload` (and the referent of the escaped &String) is dropped when THIS
// scope ends. `inner` still holds the dangling &String in its env.
// Invoking `inner` after `payload` is gone → UAF.
```

The immediate repro that ran clean did NOT build step 2's inner escaping closure
— it did `"field-" + *payload` and returned a struct, consuming the ref
synchronously. That is precisely why it was sound: no `&T` escaped.

**Verdict: 4(a) is a real gap BY CODE-READING** (capture classification +
escape-level blindness confirmed by tracing the actual functions). It is
*silent in principle* (no diagnostic emitted at the classification/escape
sites) and *distinct from the primary UAF fix* (which handles implicit
**projected MOVE captures of owned fields**, not whole-value MOVE captures of
references) — **but end-to-end reachability is not yet empirically confirmed**;
see §7.

### 4(b) — NOT a problem: the boxed callback *value* stored and called later

Storing the `CallbackThrow2<&T,&U,R>` value itself in a struct/registry and
`.call(&tk, &payload)`-ing it later is **sound**: each call site supplies its own
freshly-valid refs, and (per §3.1) the refs live only for that synchronous call.
The env captured at *construction* time does not include the ref args (they are
parameters supplied per call). Identical to a stored `fn(&T)` function pointer.
The only way 4(b) becomes unsound is if the body does the 4(a) thing internally —
i.e., 4(b) alone is fine; 4(a) is the operative condition.

### 4(c) — Adjacent, likely-also-a-gap but out of the reported shape: ref-typed RETURN

v1 *does* allow returning references from ordinary functions
(`test_autoborrow_receiver_place.py:97`: `fn node(self:&Outer) -> &Inner { return &self.inner; }`).
Soundness there relies on the **caller-side** borrow checker tying the returned
loan back to the argument's loan (`borrow_checker_pass.py:~1967` "borrow the
receiver/arg0 place and tie the loan"). Across a **vtable-dispatched `.call()`**,
the callee is behind dynamic dispatch, so a `CallbackThrow2<&T,&U,&U>` whose body
`return`s one of its `&U` params could, in principle, hand a reference back that
the caller does not know to keep alive. **However, the reported shape returns a
VALUE (`Prepared`), not a reference**, so this is not the reported issue and I did
not construct/confirm it. Flagging it as an adjacent question, not the target of
this research. (If a future fix addresses 4(a) by a general "reference may not
escape a boxed-callback boundary" rule, it should consider this sibling.)

---

## 5. Escape-level enforcement scope — does it cover captures of PARAMS?

`EscapeLevel` = IMMEDIATE / LOCAL / SCOPED / THREAD / STATIC
(`borrow_checker.py:23–30`). Default for an unannotated param is **THREAD**
(`borrow_checker_pass.py:216`, `_effective_escape_level` returns
`EscapeLevel.THREAD` when there is no world/annotation).

The enforcement (`_lambda_escape_level`, `_report_escape_violation`,
`_check_lambda_escape_level`, `_report_lambda_escape_if_borrowed`) is driven
**entirely by REF/REF_MUT captures and their tracked loans** (`state.loans`,
keyed by `ref_binding_id`). Two independent reasons it does not catch 4(a):

1. **Kind mismatch.** A captured `&T` binding read implicitly in a boxed lambda is
   a **MOVE** capture, not REF/REF_MUT. `_captured_loan_binding_ids` skips it →
   escape level `STATIC`. The param-vs-local distinction is *not* the issue;
   `cap.key.root_local` is used regardless of PARAM/LOCAL. The issue is that a
   *reference value* copied by MOVE is invisible to loan tracking.

2. **No originating loan for a `&T` param.** Even conceptually, a `&T`
   **parameter** carries no loan in the *current* function's flow state — the
   borrow that produced it happened in the caller, across the dispatch boundary.
   So there is no `state.loans` entry to bound the escape level against, and
   `_lambda_escape_level` has nothing to key on. The v1 model that keeps ordinary
   `&T`-returning functions sound (caller-side loan tying) simply does not reach
   across a boxed `.call()`.

**Answer to Q5: yes, there is a gap specific to reference-typed capture sources.**
The "non-escaping in v0" rejection does **not** fire when a nested escaping lambda
captures a `&T`-typed enclosing binding, because that capture is classified MOVE
(value copy of the pointer) rather than REF/REF_MUT, and the escape machinery only
inspects REF/REF_MUT captures. `_check_lambda_captures` (587–605) type-checks only
COPY captures; MOVE captures of references pass unexamined.

---

## 6. Recommendation

**A real gap exists (scenario 4a), and it is narrowly scoped.**

- **Condition (precise):** a lambda capture whose **captured root binding's value
  type is a reference** (`TypeKind.REF`, incl. `ref_mut`) is classified as a
  **MOVE** (or COPY) capture — i.e. the raw borrow-pointer is copied into an env —
  **and** the capturing lambda is escaping (`capture_as_move` / boxed / not
  immediately invoked). This copies a borrow past the point where the caller can
  guarantee the referent's liveness.

- **Fix location — REVISED AGAIN, 2nd reviewer pass (Medium):** my previous
  revision (put an unconditional diagnostic directly in
  `_check_lambda_captures`, lines 587-605) is ALSO wrong, for a different
  reason than the first correction. Traced the actual call-site mechanism:
  `_check_lambda_captures` is called from many places
  (`_add_lambda_capture_loans`, `_lambda_has_borrow_capture`,
  `_captured_loan_binding_ids`, `_lambda_escape_level`, `_apply_lambda_capture_moves`,
  ...) with **no `required` escape-level context** — it has types
  (`self.binding_types`/`self.type_table`), but not the caller's knowledge of
  whether THIS lambda is actually escaping in a way that matters (e.g. an
  immediately-invoked, non-retaining use vs. `conc.spawn`/return/store). An
  unconditional diagnostic there would fire even for a lambda that's boxed
  via `core.callback0(...)` but only ever `.call()`-ed once and discarded in
  the same scope (a real, legal v1 pattern per `_check_lambda_scope_escape`) —
  over-rejecting exactly the non-escaping case the reviewer warned about.
  Conversely, `capture_as_move` alone (my proxy for "is escaping") is a
  reasonable signal for the IMPLICIT-read MOVE path, but explicit
  `captures(copy ref_param)` doesn't obviously funnel through the same
  signal, so gating only on it risks under-rejecting that path too.

  **Correct fix location: `lang/driftc/borrow_checker_pass.py::_lambda_escape_level`**
  (lines 452-462) — NOT `_check_lambda_captures`, and NOT a new unconditional
  diagnostic. `_lambda_escape_level` is the function whose result
  `_check_lambda_escape_level` (551-567) compares against the call-site's
  actual `required` level (which callers — `conc.spawn`, a return statement,
  a struct-field store — already pass in with full context). Extend
  `_lambda_escape_level` itself: in addition to its existing REF/REF_MUT
  loan-bound computation, also scan `lam.captures` (already computed via the
  `self._check_lambda_captures(lam)` call at its own top, line 454) for a
  MOVE or COPY capture whose root type (`self.binding_types.get(cap.key.root_local)`)
  is `TypeKind.REF` (use `_is_optional_ref_type`, 702-718, for `Optional<&T>`
  too); if found, the computed level for THIS lambda is bounded at
  `EscapeLevel.LOCAL` regardless of what the loan-bound computation would
  otherwise produce (do not let `capture_ids` being empty default it to
  `STATIC` when a ref-valued MOVE/COPY capture is present). This way:
  - `_check_lambda_escape_level` at an immediate-invocation call site
    (`required <= LOCAL`) sees `lambda_level == LOCAL`, no violation — the
    synchronous/non-retaining pattern stays legal, matching existing
    `_check_lambda_scope_escape` allowances.
  - The SAME check at a `conc.spawn`/return/store call site
    (`required >= THREAD`/`STATIC`) sees `lambda_level == LOCAL < required` and
    correctly fires `_report_escape_violation` (464-...) through the EXISTING,
    already-context-aware mechanism — no new diagnostic call site needed.

  This still explicitly avoids routing through `_captured_loan_binding_ids`'s
  loan-set machinery (the FIRST reviewer correction, still valid) — the fix is
  a bound on the RESULT of `_lambda_escape_level`, not a fabricated loan
  entry for a binding id with no real loan.

  (Do **not** add the check in `call_resolver.py` / `_expected_function_shape` —
  those run pre-capture-analysis, lack per-capture type context, and would
  over-reject the synchronous pattern.)

- **Blast radius (does NOT break the web/rest pattern):** the existing
  `Callback2<&Req, &mut Ctx, R>` / `Callback3<&Req, …>` dispatch (§1) is
  unaffected, because:
  - those callbacks *receive* `&Req`/`&mut Ctx` as their **own** params and use
    them **synchronously** (no nested escaping closure captures them);
  - the captures-mix composer (`_SOURCE_CAPTURES_MIX`, test lines 306–320)
    captures **owned** values (`move next`, `copy mw_idx`, `share app_arc`) — none
    of which is reference-typed — into an escaping lambda, which stays legal;
  - a type-keyed check fires **only** when the captured root is itself `&T`/`&mut T`,
    which none of the positive tests do.

  Recommend adding a driver regression that (i) the 4(a) nested-escaping-ref-capture
  shape is rejected, and (ii) `test_product_shape_consumer_patterns.py` +
  `test_implicit_callback_wrap.py` still pass unchanged (they must, given the above).

- **Relationship to the primary fix:** independent. The primary fix
  (`capture_discovery.py`, commit `dee458cc`) rejects implicit **projected MOVE
  captures of owned fields** (`p.execute`). 4(a) is a **whole-value MOVE capture of
  a reference** — different key (`proj` empty), different type (reference not owned
  field), different failure mode (dangling read, not double-free). The primary
  fix's blanket projected-move rejection does not cover it.

- **COPY is a real reachable path, not just defense-in-depth (reviewer's
  Low-point, verified — revises earlier wording):** checked
  `TypeTable.copy_status()` directly (`lang/driftc/core/types_core.py:2669-2679`):
  an immutable `&T` (`TypeKind.REF`, `ref_mut=False`) **is** classified Copy;
  only `&mut T` returns `False`. Two consequences:
  1. The **implicit** path (no explicit `captures(...)` clause) never produces
     a COPY-kind capture at all — `capture_as_move` only ever chooses between
     MOVE and REF for a plain read (`capture_discovery.py` `elif use.read: kind
     = MOVE if capture_as_move else REF`), never COPY. So the *implicit* 4(a)
     repro is MOVE-only, as the reviewer suspected.
  2. But an **explicit** `captures(copy payload)` where `payload: &String` is
     a SEPARATE, currently-live path to the identical hazard: since `&String`
     passes `_is_copy`, `_check_lambda_captures`'s existing COPY-kind check
     (lines 618-627, checks `binding_types.get(...)` against `_is_copy`) does
     **not** reject it today — the raw pointer is copied into the env exactly
     as with MOVE, with the same escape-after-scope-ends hazard. COPY must
     stay in the fix's scope as a real, independently-reachable case, not
     speculative "defense-in-depth."

## 7. Empirical verification attempt (added per reviewer's Medium-severity note)

The reviewer correctly flagged that this document should not be treated as
"confirmed" without an actual repro showing current mainline either (a)
incorrectly accepts the unsafe shape, or (b) hits the known downstream
blocker. I attempted this — compile-only, using the `conc.spawn`-escape route
recommended above (to sidestep the `throws -> interface` codegen gap) — and
did **not** get a clean result either way:

```drift
fn make_vt(prepare: core.CallbackThrow2<&String, Bool, conc.VirtualThread<String>>) throws -> conc.VirtualThread<String> {
    val payload = "hello";
    return prepare.call(&payload, true);
}
// (outer callback spawns core.callback0(| | => { return payload.clone(); })
//  and returns the VirtualThread<String> handle — an owned struct, not an
//  interface, so it should not trip the FnResult/INTERFACE gap)
```

This hit an **internal compiler error**, not a diagnostic and not a clean
compile:
```
RuntimeError: SSA: load before store for local '__b8'
  at lang/driftc/stage4/ssa.py:163, _run_single_block
```

This is a THIRD outcome, distinct from both hypotheses in this document's
earlier draft. It is not yet root-caused, and I do not know whether it is:
(a) an unrelated pre-existing ICE for this general shape (nested boxed
callback returning a `VirtualThread<T>` built from a spawn inside another
boxed callback's body) unrelated to the ref-escape question, or (b) itself a
symptom of the SAME metadata confusion the sibling research doc
(`research-copy-projected-captures.md`) found in `driftc.py`'s per-slot type
preseeding (that doc found real crashes from similar prologue/metadata gaps
for projected captures generally — worth checking whether this ICE shares a
root cause before assuming it's unrelated noise).

**Revised status: 4(a) remains code-verified (the classification and
escape-blindness mechanism is real, per §4-§5) but NOT runtime-confirmed.**
A future implementer must first get a clean compile+run (varying the repro
shape — e.g. avoid `VirtualThread<T>` as the return type, try a simpler
escape route, or root-cause the ICE above) before treating this as an active,
silently-unsound runtime bug rather than a code-reading-level finding. Do not
skip this step.
