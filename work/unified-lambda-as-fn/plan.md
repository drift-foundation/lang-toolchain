# Unified Lambda-as-Function Body Checking — Plan

**Status:** planning / pre-branch. Not an implementation request. This
document is the alignment artifact between user and assistant on what
the architectural target is, what's blocking it, and how the migration
phases gate each other.

**Working directory:** `work/unified-lambda-as-fn/` (ephemeral per the
"work/ is ephemeral" repo discipline — nothing in source/stdlib/tests
imports from this tree). Durable artifacts (test files, diagnostic
preservation snapshot, eventual `CallableContext` source) move to
`lang/tests/...` / `lang/driftc/...` as deliverables of their
respective steps.

**Branch (when started):** suggested name
`feature/unified-lambda-callable-context`. Not opened yet.

---

## Architectural target

```
HLambda expression
  -> resolve expected callable shape, if present
  -> resolve explicit captures (consult outer scope ONCE here)
  -> create anonymous CallableContext:
       params, return type, throwability/auto-try contract,
       capture env, callable-local state
  -> check lambda body through the same body-checking machinery as
     named functions, parameterized over (CallableContext,
     CompilationUnitContext)
  -> return a function/callback value expression from the outer
     expression checker
```

**Core invariant (load-bearing — single property the migration must
validate at every step):**

> After expected-type + capture resolution, lambda body checking does
> not read outer callable state directly. Capture resolution may
> consult outer scope. Body checking sees only its own
> `CallableContext` and the shared `CompilationUnitContext`.

This invariant is the acceptance criterion for the work — not an
N% reduction in nonlocals or similar quantitative measure. Concretely
testable: at each step's completion, `grep` the body-check helpers
for any reference to outer-callable state. If a reference exists, the
step is incomplete. Final state — every closure-captured `nonlocal`
declaration in body-check helpers either (a) names a
`CompilationUnitContext` field, or (b) is a defect.

---

## TL;DR

Drift's lambda model is in a strong position to be cleaner than the
current implementation. Captures are explicit, so lambdas don't need
to behave like ambient closures during body-checking. Once expected
type + captures are resolved, a lambda can be treated like an
anonymous function with its own callable context.

**Recurring problem pattern:** lambda bugs keep coming from checking
lambda bodies as nested expression fragments that share mutable
ambient state from the enclosing function/checker path. **Bug A
(0.31.38) is the latest example** — outer `fn_declared_throws` and
`try_block_depth` crossed the lambda boundary and broke `match
&Result` inside nothrow lambdas nested in throws outer fns. Adjacent
issues seen around `CallbackN` expected typing, capture escape, and
retry behavior.

**This is compiler architecture work, not a workaround for any one
bug.** The goal is fewer lambda-specific edge bugs by making lambda
body checking structurally separate from the enclosing function,
while still preserving expression-context typing for expected
`Callback*` / `CallbackThrow*` inference.

---

## Decision summary (2026-04-30)

| Question | Decision |
|---|---|
| Goal of the track? | Make lambda body checking structurally separate from the enclosing function-body check, sharing the same body-check core via an explicit `CallableContext`. |
| Workaround for a specific bug? | **No.** Bug A (0.31.38) is fixed in mainline already. This track prevents a *class* of future bugs. |
| Single big bang or phased? | **Phased.** 8 steps, each independently reviewable and reversible, none individually large. |
| Touching lambda *semantics*? | **No.** The migration is structural; user-visible behavior is preserved (validated by representative diagnostic preservation, see Step 0b / S3 below). |
| Touching expected-type / capture resolution machinery? | Capture resolution stays at lambda *construction* time and may consult outer scope. Body checking must not. The split is sharp. |
| Need ABI bump? | **No.** Internal refactor; no compiler/runtime boundary shift. |
| Step 1 starts with write-through scaffolding? | **No.** Step 1 migrates one already-save/restored field (`return_type`) immediately onto `CallableContext` — no dead-weight write-through that nobody reads. |
| When does the track start? | After 0a + 0b land as a standalone patch, decide based on outcome whether to continue immediately or file as a refactor-trigger pending another Bug-A-class incident. |

---

## Audit — current state of HLambda body-check ambient state

After Bug A (0.31.38). Three categories of state in
`lang/driftc/type_checker.py:check_function`:

### (a) Save/restored explicitly at HLambda entry/exit (`:6266+` / `:6307+`)

| State | Save/restore status | History |
|---|---|---|
| `return_type` | Explicit save/restore | Existed pre-0.31.38 |
| `fn_declared_throws` | Explicit save/restore + reset by lambda's effective throwability | **Added in 0.31.38 (Bug A)** |
| `try_block_depth` | Explicit save/restore + reset to 0 | **Added in 0.31.38 (Bug A)** |

### (b) Stack-based push/pop (correct by construction)

| State | Mechanism |
|---|---|
| `scope_env` | `append({}) … pop()` per lambda body |
| `scope_bindings` | `append({}) … pop()` per lambda body |
| `explicit_capture_stack` | `append(capture_kinds) … pop()` if captures |

### (c) Closure-captured, inherited from outer fn-body, NOT save-restored — the latent landmines

| State | Where leaked from | Risk profile |
|---|---|---|
| `catch_depth` | Modified by `type_block` of catch arms (`:10100`) | Lambda constructed inside a catch arm sees `catch_depth > 0`. Currently consulted only in narrow paths; **untested for lambda-boundary correctness**. |
| `unsafe_context` | Set per-fn at `check_function` entry; modified inside `unsafe { }` blocks | Lambda inside `unsafe { }` body inherits unsafe_context. Whether intended is unclear today — **§S2 below pins the rule**. |
| `pending_lambda_by_binding` | Accumulates bindings → lambdas across fn body | Per-fn dict; stays alive for lambda body. Likely benign (binding_ids unique) but architecturally muddy. |
| `binding_types`, `binding_names`, `binding_mutable`, `binding_place_kind`, `local_const_binding_ids` | Accumulate; never cleared at lambda boundary | Lambda's own binding_ids are added to these dicts and persist after lambda body checks. Benign (ids unique) but conflates "this fn-body's bindings" with "every binding ever seen during this fn check." |
| `borrows_in_stmt`, `borrow_expr_ids_in_stmt` | Cleared per-stmt at `:9362-9363` | Cleared often enough that lambda-boundary leak rarely matters, but the design assumes stmt boundaries — false at lambda construction sites mid-expression. |

**Bug A's lesson, reread:** `fn_declared_throws` and `try_block_depth`
were in category (c) until 0.31.38. The category-(c) entries above
are the next candidates for the same class of bug.

---

## Target — `CallableContext` shape

```
@dataclass
class CallableContext:
    # Identity / contract
    fn_id_or_lambda_id: ...
    params: list[ParamSlot]
    return_type: TypeId | None
    declared_throws: bool
    declared_nothrow: bool   # for lambdas with explicit decl

    # Auto-try contract state (was Bug A)
    try_block_depth: int = 0
    catch_depth: int = 0

    # Effect-tracking
    unsafe_context: UnsafeContext

    # Scope
    scope_env: list[dict[str, TypeId]]
    scope_bindings: list[dict[str, int]]

    # Captures (lambdas only; empty for named fns)
    capture_env: dict[str, TypeId]
    capture_kinds: list[...]

    # Per-callable bindings — NOT shared across callables
    binding_types: dict[int, TypeId]
    binding_names: dict[int, str]
    binding_mutable: dict[int, bool]
    binding_place_kind: dict[int, PlaceKind]
    local_const_binding_ids: set[int]
    pending_lambda_by_binding: dict[int, HLambda]

    # Per-statement
    borrows_in_stmt: ...
    borrow_expr_ids_in_stmt: ...
```

**What stays shared at a higher level** (compilation-unit-wide,
NOT per-callable):

- Type table, signatures_by_id, callable_registry
- Trait/impl indexes
- ID allocators (`next_callsite_id`, `next_node_id`, `next_index`,
  `max_id`)
- Diagnostics list
- `thunk_specs`, `lambda_fn_specs` accumulators

These belong on a `CompilationUnitContext` wrapper, not duplicated
per callable.

---

## Pseudocode for the unified shape

```
def check_callable_body(ctx: CallableContext, body: HBlock | HExpr,
                        cu: CompilationUnitContext) -> TypeId:
    # Body-checking is pure-functional in `ctx` + `cu`; nothing
    # closure-captured from the caller.
    ...

def check_named_function(fn_id, fn_sig, body, cu):
    ctx = CallableContext.from_signature(fn_sig)
    return check_callable_body(ctx, body, cu)

def type_lambda_expr(expr: HLambda, expected_type, cu) -> TypeId:
    expected_fn = _resolve_expected_callable(expr, expected_type, cu)
    captures = _resolve_captures(expr,
                                 outer_scope_for_capture_lookup, cu)
    ctx = CallableContext.from_lambda(expr, expected_fn, captures)
    return_ty = check_callable_body(ctx,
                                    expr.body_block or expr.body_expr, cu)
    return _construct_callable_value_type(expr, ctx, return_ty)
```

---

## Semantic decisions to lock before the migration starts

These three rules (S1, S2, S3) are not free choices made by the
refactor. They're contracts the migration preserves. **Sign-off on
S1, S2, S3 is mandatory before Step 1 starts.**

### S1 — Captured-binding identity rule (mandatory pre-Step 3)

> Captured outer bindings appear in the lambda's `CallableContext`
> under their **original binding_ids**, with the binding's type / name
> / mutability / place_kind as observed at lambda construction time.
> The body check looks up captures via the same binding_id-keyed
> dicts as it does for lambda-local bindings — never via fallback to
> outer-context dicts.

Concretely, at lambda construction (after `discover_captures`):

```
for capture in resolved_captures:
    bid = capture.binding_id            # SAME id as in outer ctx
    ctx.binding_types[bid] = outer_ctx.binding_types[bid]
    ctx.binding_names[bid] = outer_ctx.binding_names[bid]
    ctx.binding_mutable[bid] = outer_ctx.binding_mutable[bid]
    ctx.binding_place_kind[bid] = outer_ctx.binding_place_kind[bid]
    ctx.scope_env[0][capture.name] = outer_ctx.binding_types[bid]
    ctx.scope_bindings[0][capture.name] = bid
```

The lambda body reads outer state **never**; it reads only its own
`ctx.binding_*`. HIR→MIR's existing capture-rewrite handles the
runtime aspect (move/copy/share semantics, per `HCaptureKind`) — the
identity rule above is purely about checker-time state.

**Why copy and not remap to synthetic capture-slot ids:**

- Same id keeps lambda-body code identical to outer-fn-body code; no
  special "is this id a capture?" branch in body-check.
- Synthetic remapping would force HIR rewriting at construction time
  (binding_id changes mean HVar nodes referring to the binding need
  updating). Today HIR is stable post-construction; introducing a
  remap would ripple into every HIR pass.
- Stale-copy concern doesn't apply: once a binding's type is set in
  the outer ctx, it doesn't change. (`binding_types` is
  write-once-per-binding by construction. If that ever changes, the
  rule above gets a single explicit place to add propagation logic.)

**Edge case spec — nested lambda inside lambda:** The inner lambda's
captures are resolved against its outer lambda's ctx, not the
outermost named-fn's. Capture chains transit one level at a time, by
the same rule. New regression in Step 0a:
`test_nested_lambda_capture_identity_through_chain`.

### S2 — `unsafe_context` propagation rule

> A lambda's body inherits the **syntactic enclosing `unsafe { }`
> context at the lambda's construction site**. Unsafe operations
> inside the lambda body are permitted iff the lambda is constructed
> inside an `unsafe { }` block, or inside the body of an
> `unsafe`-marked function. The lambda's *invocation* site does not
> affect this — the unsafe contract applies at construction-time
> syntactic position.

Rationale:

- Matches existing behavior (verified by stdlib's
  `unsafe { val cb = || => { mem.write(...); }; ... }` patterns in
  `array.drift`).
- Source-level reasoning: the user wrote the lambda body inside
  `unsafe { }`; that's where they accepted the unsafe contract.
- Treating invocation site as authoritative would be source-incorrect:
  a lambda escaping its construction `unsafe { }` and called from a
  safe context would have a hidden unsafe surface, which is exactly
  what `unsafe { }` is supposed to make explicit.

**Adjacent decisions locked alongside:**

| Question | Answer |
|---|---|
| Nested lambda inside lambda inside `unsafe { }` — does inner inherit? | **Yes**, transitively. Each lambda's body inherits its own construction-site syntactic position; nested is unsafe by induction. |
| Lambda whose construction is *outside* `unsafe { }` but is invoked inside `unsafe { }`? | **Body is safe.** The lambda's own contract was set at construction. |
| Unsafe operation directly inside lambda body without explicit `unsafe { }` inside? | **Allowed iff construction site was unsafe.** Same rule as named functions inside `unsafe`-marked fn bodies — the marker propagates. |

**Pin in Step 0a:** new regressions `test_lambda_unsafe_inheritance_*`
covering the four cells of `(construction-unsafe? × invocation-unsafe?)`.
Any future refactor that breaks one of these fails the test, forcing
the change to be a deliberate semantic decision rather than an
accident of refactor shape.

**Risk acknowledgment:** if Step 0a reveals the *current*
implementation diverges from this rule in some edge case, that's a
separate finding — flagged before the migration starts. Migration
preserves whatever behavior the new tests pin; if the pinned behavior
diverges from intent, that's a small standalone fix patch *before*
the migration begins.

### S3 — Diagnostic preservation contract

Replaces an earlier "byte-for-byte equivalence" framing, which is too
brittle and would block legitimate wording improvements.

> A snapshot of representative driver/checker test diagnostics,
> captured at HEAD, with three preservation invariants per diagnostic:
>
> 1. **Code/family** unchanged (e.g., `E-MATCH-ARM-TYPE` stays
>    `E-MATCH-ARM-TYPE`; user-visible code identifiers don't shift
>    across the migration).
> 2. **Severity** unchanged (`error`/`warn`/`note`).
> 3. **Span class** preserved — same source line, same node kind under
>    the span. Column drift within a reasonable tolerance (a few
>    chars) is acceptable when the message wording stays the same;
>    cross-line span shifts are not.
>
> Wording-only improvements *are* allowed mid-migration but require
> an explicit per-diagnostic note in the patch description ("rephrased
> E-CAPTURE-SHARE-NOT-SHARE for clarity"). The contract is on **kind
> of diagnostic for kind of input**, not byte-for-byte text.

Mechanically: the snapshot is
`tests/checker/diagnostic_preservation.json` keyed by test ID, with
`(code, severity, span_class)` tuples. CI fails if a tuple changes
without a corresponding patch-note acknowledging the change.

---

## Phase plan

### Step 0a — Audit + isolation tests (~1 day, zero compiler change)

**Deliverable:** new test file
`lang/tests/driver/test_lambda_callable_isolation.py`. Each test
independently fails if its specific isolation property is broken.
Coverage:

| Test | Asserts |
|---|---|
| (already present, 0.31.38) `test_throws_outer_with_nothrow_lambda_match_on_result_compiles` | `fn_declared_throws` does not leak across lambda boundary. |
| (already present, 0.31.38) `test_try_block_around_lambda_does_not_leak_into_lambda_body` | `try_block_depth` does not leak across lambda boundary (independently validated by removing only that reset). |
| (existing baseline) `return_type` boundary | Return-target binding is per-callable, not shared with outer. |
| **NEW** `test_lambda_inside_catch_arm_catch_depth_does_not_leak` | Lambda constructed inside a `catch ExcType(_) {}` arm doesn't see `catch_depth > 0` in its body. |
| **NEW** `test_lambda_unsafe_inheritance_construction_unsafe_invocation_safe_permits` | Pin S2 cell: construction in `unsafe { }`, invocation in safe context → body permits unsafe ops. |
| **NEW** `test_lambda_unsafe_inheritance_construction_safe_invocation_unsafe_rejects` | Pin S2 cell: construction in safe context, invocation in `unsafe { }` → body rejects unsafe ops. |
| **NEW** `test_lambda_unsafe_inheritance_both_unsafe_permits` | Pin S2 cell: both unsafe → permits. |
| **NEW** `test_lambda_unsafe_inheritance_both_safe_rejects` | Pin S2 cell: both safe → rejects. |
| **NEW** `test_nested_lambda_unsafe_inheritance_transitive` | Pin S2 nested rule: lambda-in-lambda-in-`unsafe{}` body permits unsafe. |
| **NEW** `test_lambda_capture_identity_through_construction` | Pin S1: outer binding's id is preserved in lambda ctx (same `binding_id`, same type/name/mutability/place_kind). |
| **NEW** `test_nested_lambda_capture_identity_through_chain` | Pin S1 transitively for nested lambdas: capture chain transits one level at a time. |
| **NEW** `test_lambda_local_bindings_do_not_pollute_outer_after_body_check` | Lambda-local binding_ids do not appear in outer-fn dicts post-body-check (architectural cleanness — not necessarily a current bug). |

**Exit gate:**

- All tests pass on HEAD (cleanliness audit), OR
- Some test fails on HEAD (latent bug surfaced) → fix as a small
  standalone patch *before* migration begins; the migration's
  acceptance criterion is "preserve what the tests pin," not
  "preserve whatever HEAD does."

**`git diff main` for this step touches `lang/tests/` only.**

### Step 0b — Diagnostic preservation snapshot (~0.5 days, zero compiler change)

**Deliverable:** `tests/checker/diagnostic_preservation.json` capturing
the `(code, severity, span_class)` tuple for every diagnostic emitted
by a representative slice of driver / checker / package tests at
HEAD. Plus a CI hook
(`tools/dev/check_diagnostic_preservation.py`) that reads the
snapshot and validates current output matches the contract per S3.

**Exit gate:**

- Snapshot covers ≥ 80% of `lang/tests/driver/` and
  `lang/tests/checker/` diagnostic-emitting tests.
- CI hook passes on HEAD.
- Hook documented in `AGENTS.md` as the contract migration steps
  must preserve.

### Step 1 — Introduce `CallableContext`, migrate `return_type` (~1 day)

**Deliverable:** define `CallableContext` dataclass with at least one
real field — `return_type`. Both named-fn body and lambda body
instantiate a ctx and read/write `return_type` through it.

**No write-through scaffolding.** This step is a real reduction (one
fewer closure-captured local + one fewer save/restore site), not
preparatory plumbing nobody reads.

Concretely:

- `CallableContext.return_type: TypeId | None` field.
- At named-fn entry:
  `ctx = CallableContext(return_type=signature.return_type_id, ...)`.
- At lambda entry:
  `lambda_ctx = CallableContext(return_type=lambda_ret_type, ...)`.
- Replace the closure-captured `return_type` local entirely:
  `nonlocal return_type` declarations removed; reads/writes go
  through `ctx.return_type`; lambda's existing save/restore at
  `:6266 / :6307` for `return_type` collapses to "build fresh ctx" /
  "discard ctx after body."

**Exit gate:**

- All Step 0a tests pass.
- Step 0b diagnostic preservation snapshot passes (no `(code,
  severity, span_class)` drift).
- `nonlocal return_type` no longer appears in `type_checker.py`.
- Full driver/stage/checker/packages suite green.

### Step 2 — Migrate auto-try state (`fn_declared_throws`, `try_block_depth`) (~1 day)

`fn_declared_throws` and `try_block_depth` (currently save/restored
from Bug A fix) move onto `CallableContext`. Save/restore at lambda
boundary collapses further — at this point the only thing the lambda
body-check entry does is "build a fresh ctx."

**Exit gate:** as Step 1 + grep confirms `nonlocal fn_declared_throws`
and `nonlocal try_block_depth` no longer appear.

### Step 3 — Migrate scope state (~2 days)

`scope_env`, `scope_bindings`, and the per-callable binding state
(`binding_types`, `binding_names`, `binding_mutable`,
`binding_place_kind`, `local_const_binding_ids`) onto ctx.
**Captured-binding identity rule (S1) is enforced here** — the rule
becomes load-bearing once binding state is per-callable.

**Pre-req:** S1 is signed off. Step 0a's `test_lambda_capture_identity_*`
and `test_nested_lambda_capture_identity_through_chain` tests pin the
rule.

**Exit gate:** as previous steps + Step 0a's capture-identity tests
remain green throughout.

### Step 4 — Migrate effect state (~1 day)

`catch_depth`, `unsafe_context` onto ctx. **S2 enforced here.**
Step 0a's `test_lambda_unsafe_inheritance_*` tests (4 cells +
nested) pin the rule.

**Exit gate:** as previous + Step 0a's effect-state tests remain
green.

### Step 5 — Migrate per-stmt state (~1 day)

`borrows_in_stmt`, `borrow_expr_ids_in_stmt`,
`pending_lambda_by_binding` onto ctx. Decide whether
`pending_lambda_by_binding` is per-callable or per-CU (see §"What
blocks unification today" below).

**Exit gate:** as previous.

### Step 6 — Lift body-check helpers out of `check_function` (~3-5 days)

`type_stmt`, `type_expr`, `type_block` and their nested helpers move
out of `check_function` to take `(ctx, cu)` explicitly. Pure
refactor; no behavior change. This is the biggest single step and
where the diagnostic-preservation snapshot earns its keep — column
drift / deferred-error-ordering changes get caught immediately.

**Exit gate:** as previous + at completion, the core invariant
becomes verifiable: `grep` body-check helpers for any reference to
outer-callable state — must find none.

### Step 7 — Unify lambda + named-fn entry (~1 day)

Both call `check_callable_body(ctx, body, cu)`. The save/restore code
at lambda entry collapses to "build a fresh ctx, call
check_callable_body, return result." Same body-check core invoked
for both paths.

**Exit gate:**

- All previous tests green.
- `check_callable_body` is the single body-check entry point; lambda
  and named-fn entries differ only in ctx construction.
- The refactor-trigger entry referenced by this plan can be marked
  "landed in version X."

**Total: ~10-12 days across 8 patches.** None individually large;
each independently reviewable; each has regression net.

---

## What blocks unification today

In rough order of cost:

### (a) Architectural — body-check helpers nested in `check_function`

`type_stmt`, `type_expr`, `type_block`, `_pretty_type_name`,
`_apply_method_boundary`, dozens more — all closure-capture every
relevant local. ~5000 lines of nested helpers. Lifting them to take
an explicit `(ctx, cu)` parameter pair is the bulk of the work
(Step 6).

### (b) Outer-scope capture resolution

Captures legitimately consult outer scope at lambda construction time
— `discover_captures(expr)` (`:6053`) walks `scope_bindings[:-1]`
and the existing capture chain to find binders the lambda references.
This is the one cross-boundary access that must remain supported.
The fix is shape-clean: capture resolution happens *before* the
lambda's `CallableContext` is built and only at construction time;
once captures are resolved into `ctx.capture_env` per S1, the body
check has zero need for outer-scope access.

### (c) `pending_lambda_by_binding` is per-fn

The dict tracks lambdas held by `var f = |...| => {...}` bindings so
that subsequent calls `f.call(x)` can re-type them. Today this dict
spans the entire fn-body check. Under CallableContext, each callable
owns its own dict — but a deferred lambda whose binding is held in
an outer fn but invoked inside a *different* callable (rare but
possible) needs a lookup mechanism.

Two options for Step 5:

- **(i)** Keep it on `CompilationUnitContext` keyed by
  `(fn_id, binding_id)`. Cross-callable lookup works.
- **(ii)** Restrict deferred lambdas to same-callable invocation
  only. Cleaner; may break existing programs.

Decide at Step 5 based on whether (ii) breaks any program in the
test suite.

### (d) Test diagnostic-text dependence

Many existing tests grep for specific diagnostic strings at specific
lines. Refactoring the body-check call paths can subtly change spans,
deferred-error ordering, or message accumulation order. **Mitigation:
Step 0b** — the diagnostic preservation snapshot validates per S3
that `(code, severity, span_class)` tuples are preserved; wording-only
changes require explicit patch-note acknowledgment.

### (e) Cross-callable type inference

Lambda-as-expression contributes a type to its enclosing expression.
The current code uses a mix of immediate-checking and pending-checking
(`pending_lambda_by_binding`, `expected_type_from_require`). Under
CallableContext, the timing model needs an explicit answer: "lambda
body is checked when?" — at construction time (immediate, current
default) vs at first use (deferred, used for unannotated `var f =
lambda` bindings). The distinction stays; just gets named explicitly.

### (f) Internal-error policy

`check_function` accumulates state that may be inspected post-error
(e.g., partial type resolution). The refactor must keep error-recovery
semantics the same — errors land in `diagnostics` (which is on
`CompilationUnitContext`, not per-callable) and partial state remains
observable. **No regression in user-visible diagnostic behavior under
partial failure.**

---

## Bug classes eliminated by this work

After Step 7, structurally-impossible:

1. **"State X leaks across the lambda boundary"** — Bug A's category.
   Eliminated because lambda entry creates a fresh `CallableContext`
   with explicit defaults, not by save/restore of N closure-captured
   locals (and "N" can grow without anyone noticing).
2. **"Outer fn's `catch_depth` bleeds into lambda body"** —
   currently theoretical but uncovered by tests; structurally fixed.
3. **"Outer fn's `unsafe_context` bleeds into lambda body"** —
   same shape as above; rule pinned by S2.
4. **"Captured ambient scope is too permissive"** — capture-env is
   built explicitly at construction and is the *only* outer-scope
   visibility the body has. Future capture-resolution bugs would be
   in `_resolve_captures`, not in body-check ambient state.
5. **"Throws inference confusion at lambda boundary"** — currently
   fragile (see Bug A); structurally fixed because each callable's
   effect surface is its own field.
6. **"Future state X added to fn-body that someone forgets to
   save/restore at lambda boundary"** — the most insidious shape,
   eliminated structurally because there's no "lambda boundary
   save/restore" to forget. New per-callable state goes on
   `CallableContext`; new per-CU state goes on
   `CompilationUnitContext`; the language of the code makes the
   choice explicit.

**NOT eliminated by this work** (separate concerns):

- Bugs in expected-type resolution at the lambda call site
  (`_expected_function_shape` / `_lambda_can_throw` / Callback-N
  inference)
- Bugs in capture resolution (`discover_captures`)
- Bugs in HIR→MIR lowering of lambdas
- Borrow-checker state at lambda boundaries (separate state machine)

Those have their own architectural patterns; this track is about
*body-check ambient state* specifically.

---

## Stop-and-escalate triggers (during the migration)

Halt and consult before proceeding if any of these fire:

- **Step 0a surfaces a latent bug** in the current implementation.
  Fix as a standalone patch before continuing; the migration
  preserves what the tests pin, not whatever HEAD does.
- **Diagnostic preservation snapshot mismatch** at any step. Stop,
  identify root cause, fix or revert. Per S3, wording-only
  improvements are allowed but require explicit patch-note.
- **Test diagnostic-string break** beyond the wording-only category.
  Distinguish "intended improvement" from "subtle regression";
  default to preserving exact current text unless explicitly
  improving.
- **Performance regression in `check_function` >5%** on a
  representative compile. Investigate before continuing — passing
  ctx through call chains has measurable cost; we're betting it's
  small but not zero.
- **Cross-callable lookup case under (c) above** breaks an existing
  program. Choose between options (i) and (ii) explicitly; document
  the choice in the Step 5 patch.
- **S1 / S2 contract broken** at any step. The semantic decisions
  are the contract the migration preserves; if a step appears to
  require breaking S1 or S2, escalate before proceeding — either
  the step's design is wrong or the contract needs explicit
  amendment in this plan.

---

## Out of scope (will not do in this track)

- Changes to lambda *semantics* (capture rules, throwability rules,
  closure-vs-fn distinction). Pure structural refactor.
- Borrow-checker integration with `CallableContext`. Borrow-checker
  has its own state machine; touching it is a separate track.
- HIR→MIR rewrites. The HLambda node's HIR shape is unchanged by
  this work.
- Expected-type resolution improvements. Out of scope;
  `_expected_function_shape` / `_lambda_can_throw` stay as-is.
- Generic instantiation of lambdas. Untouched.
- Memory-layout changes. ABI-neutral throughout.

---

## Open questions (to resolve before Step 1 starts)

1. **`pending_lambda_by_binding` placement (Step 5).** Per-callable
   or per-CU? Decide based on whether any existing test exercises
   the cross-callable lookup case.
2. **Step 6 splitting.** `type_expr` is ~5000 lines. Lifting in one
   patch is risky; consider sub-patches by helper category
   (statement-handlers, expression-handlers, scope-helpers,
   diagnostic-helpers).
3. **`CompilationUnitContext` shape.** Does it deserve its own
   dataclass, or is it just "stuff that's currently on `TypeChecker`
   and shared"? Lean toward dataclass for symmetry with
   `CallableContext` and so the body-check helpers' `(ctx, cu)`
   signature is uniformly typed.
4. **Refactor-trigger registry entry.** This track's existence
   warrants an entry in `docs/refactor_triggers.md` referencing this
   plan. The entry's "trigger fired" condition is "another
   Bug-A-class state leak surfaces *and* Step 0a is not yet landed."
   If 0a is landed (regardless of whether further steps are done),
   the entry's trigger is "any Step 0a test starts failing" — which
   is a functional regression, not a refactor opportunity, and would
   reactivate the migration if other steps are paused.
5. **Migration timing.** Continue immediately after 0a/0b, or pause
   after 0a/0b and reassess? Recommendation in §"Notes on what this
   plan is NOT" below.

---

## Notes on what this plan is NOT

- It is **not** a green light to start Step 0a — that requires user
  approval of this revised plan first.
- It is **not** a commitment to Steps 1-7 — those gate on Step 0a
  outcomes.
- It is **not** a guarantee of timeline. Estimates are
  rough-order-of-magnitude.
- It is **not** under source/stdlib/tests/tooling. `work/` is
  ephemeral; durable artifacts (test files, diagnostic preservation
  snapshot, eventual `CallableContext` source) move to their proper
  homes as deliverables of their respective steps.
- It is **not** a workaround for any specific current bug. Bug A
  (0.31.38) is fixed in mainline. This track prevents a *class* of
  future bugs.
- It is **not** lambda-semantics work. Capture rules, throwability
  rules, closure-vs-fn distinction stay as they are — codified as
  S1 (capture identity) and S2 (unsafe propagation).

## Recommendation for first step

If/when the green light is given to start: **Step 0a + 0b only**,
as a single standalone patch. This:

- Pins what's currently working (or surfaces a latent bug if any
  test fails on HEAD).
- Defines the contract the migration will preserve (S1, S2, S3).
- Is reversible, ABI-neutral, no compiler change.
- Tells us whether the migration is worth the cost, by quantifying
  the latent-bug surface.

If 0a surfaces real latent bugs, the migration's value goes up. If
0a passes cleanly on HEAD, the migration is "structural-cleanup-with-
future-bug-prevention" — still valuable, but with the cost-benefit
math known not just assumed.

**Decision point after 0a:** continue full migration, or land just
0a as a documentation-of-current-invariants and defer the rest as a
refactor-triggers entry pending another Bug-A-class incident.
