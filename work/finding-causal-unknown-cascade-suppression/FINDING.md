# Finding: Unknown cascade suppression is not causally scoped

Date filed: 2026-08-03

Refreshed: 2026-08-05 against the committed pending-`0.35.0` tree after the
pending-lambda probe-rollback barrier landed and received terminal review. The
repository full suite for that committed slice is still running. This refresh
changes finding material only; it does not edit compiler code, shared tests,
the language specification, or implementer-owned `PROGRESS.md`.

Origin: R5 static review of `finding-nonflat-divergent-lambda`.

Status: next substantive compiler finding after the active full-suite gate.
The parent diagnostic-causality bug is still open. Its newly recorded child,
`findings/finding-pending-lambda-value-finalization`, is a distinct
`LANGUAGE_BUG` exposed by the preflight matrix, but it is a strong fold-in
candidate because both bugs need one total pending-lambda finalization path.
Planning review also confirmed a second child,
`findings/finding-typed-let-callback-wrap-regression`: direct bare-lambda
initialization of a typed Callback slot reaches MIR unwrapped, regressing an
explicitly documented/tested 0.31.17 contract. It should restore implicit
wrapping; clean rejection is not an open language choice.
Planning round 2 widened that child from typed HLet to one materialization
contract: a pending thin-function value passed to a Callback parameter is
instead mislabeled as an already-built interface, producing an uninitialized
interface-local MIR failure. The same round found
`findings/finding-fnptr-borrow-materialization`: `&named_function` lets
function-reference replacement violate the HVar-only canonical-place boundary
and ends in a raw AttributeError. Both are folded family children, subject to
red-first structural confirmation rather than the leading mechanism being
treated as authoritative.
The earlier probe-rollback child is implementation-complete and review-signed
off; its mutation barrier is now a precondition of this design, not work to
reimplement.

All claims below distinguish executed evidence, current static facts, and
design hypotheses. The implementer should reject or revise any proposal that
does not survive red-first probes or current source tracing.

## Classification

### A. Confirmed LANGUAGE_BUG: unrelated errors suppress independent errors

Two live guards treat “some error already exists in this function” as proof
that a particular `Unknown` value has already been diagnosed:

- `lang/driftc/type_checker.py:4124`, `_require_copy_value`, suppresses
  `E-COPY-UNKNOWN` when `diagnostics` contains any earlier error.
- `lang/driftc/checker/call_resolver.py:6786`, the local-binding
  `HCall(fn=HVar)` fallback, suppresses `call target is not a function value`
  under the same function-global condition.

The exact binding id is available at both sites, but neither predicate checks
it. Executed work probes prove that an unrelated earlier copy error suppresses
both independent tripwires on a separate preseeded `Unknown` binding.

`HInvoke(callee=HVar)` has the opposite drift at
`type_checker.py:10204`: it always emits the call-target diagnostic, even when
the same binding already has a causal primary. The two function-value call
routes therefore disagree.

This is user-visible diagnostic unsoundness. Invalid programs can lose an
independent error because another source line happened to fail first.

### B. Confirmed LANGUAGE_BUG child: pending captureless values do not finalize

The preflight matrix independently established that every non-call value read
of a pending stored lambda sees the placeholder `Unknown` rather than
finalizing the lambda:

- an inferable captureless lambda cannot be aliased;
- a contextual `CallbackN` alias does not propagate its expected shape;
- a later successful direct call does not repair an earlier alias read;
- pending lambdas in return and argument positions surface unrelated
  `Unknown`-based presentations.

This conflicts with the approved v1 specification:

- §22.0.1: non-capturing function pointers may be stored and returned freely;
- §22.2.3: only a *capturing* literal lacks a bare stored representation;
- §22.3: non-capturing closures lower to thin, `Copy` function pointers.

The child is not merely diagnostic polish: valid captureless programs are
rejected. See `findings/finding-pending-lambda-value-finalization/FINDING.md`.

### C. Confirmed LANGUAGE_BUG child: Callback slots accept labels without values

Two distinct sources prove the same checker/lowering breach:

- a direct bare lambda in a typed Callback HLet is recorded as Callback while
  the initializer remains raw HLambda;
- a pending captureless lambda passed as a bare HVar to a Callback parameter
  causes MIR to move an uninitialized interface local because the binding was
  stamped as the interface without a callback object construction.

Both must converge on the existing implicit `core.callbackN(...)` wrapper
authority. The original binding remains a thin function pointer; each Callback
slot owns its explicit HIR construction.

### D. Confirmed LANGUAGE_BUG child: named-function borrow breaks place shape

Borrowing an already-finalized stored fnptr compiles and runs, establishing
that shared borrow of a function-pointer value is accepted. The corresponding
`&named_function` source raises raw `AttributeError: 'HFnPtrConst' object has
no attribute 'name'`. Static evidence points to late fnptr replacement inside
an HVar-only canonical place, but the exact repair remains falsifiable. See
`findings/finding-fnptr-borrow-materialization`.

## Current-tree facts after the rollback child

The just-landed rollback fix changed the safe design space:

- `PendingLambdaOwner` is the sole pending-state owner
  (`type_checker.py:606+`). Registration, mutation-intent lookup, retirement,
  and drain are exact-binding operations over a private map.
- `begin_resolution()` raises `PendingLambdaBarrier` before external mutation
  whenever a deferred-call probe transaction is active.
- nested probes roll back and propagate; only the outermost converts the
  barrier into ordinary expected-context deferral.
- current HCall/HInvoke pending resolution remains duplicated around
  `type_checker.py:10080` and `:10113`.
- declarations register the placeholder at `:12418`; unresolved entries drain
  at `:13854`.

Therefore a shared first-reference finalizer can be considered without
reopening the proven rollback leak, but it must enter through
`PendingLambdaOwner.begin_resolution()` before touching the lambda, binding
tables, captures, or `_lambda_fn_specs`. A helper that uses `peek()` and then
mutates would defeat the barrier.

`FnCheckState.OWNED_TABLES` remains the transaction authority
(`type_checker.py:462+`). Any mutable Unknown-cause state belongs there as a
`_TxnDict`, so rollback and `state_fingerprint()` cover it. The pending owner
itself deliberately remains outside the undo log because mutation is barred,
not rolled back.

No registered entry in `doc/refactor_triggers.md` matches diagnostic
provenance or pending-lambda finalization as of this refresh. The implementer
must repeat the scan when starting the LANGUAGE_BUG fix.

## Required diagnostic invariants

The patch must preserve all of these cases:

1. **Same-source poison:** a producer emitted a primary diagnostic and left
   binding `b` as `Unknown`; copy/call complaints over `b` are cascades.
2. **Independent Unknown:** binding `u` has no causal primary; an error on
   another line must not silence `u`'s copy/call tripwires.
3. **Alias propagation:** `g` initialized directly from causally poisoned `b`
   remains causally poisoned even if no new diagnostic is emitted at `g`'s
   initializer.
4. **Concrete recovery:** when pending `b` resolves to a concrete function
   type, any prior/pending cause state is absent.
5. **Lexical identity:** shadowed bindings with the same source spelling never
   share a cause.
6. **Consumer parity:** HCall and HInvoke deliberately apply the same causal
   rule for the same function-value binding unless a tested semantic reason
   proves they differ.
7. **No global fallback:** absence of provenance fails toward the tripwire,
   never toward suppression.

The existing stored-capturing-lambda tests pin case 1. The executed work probe
pins case 2. The preflight alias matrix proves case 3 is needed. Cases 4–6 need
in-tree red/green coverage in the implementation slice.

## Required pending-lambda invariants

The finalizer must distinguish pending from poisoned; `Pending` must never be
treated as “already diagnosed Unknown.”

For an exact pending binding id:

1. Enter through `PendingLambdaOwner.begin_resolution()`.
2. Resolve at most once, using a contextual function shape when one is
   actually available.
3. Store the *thin function type* on an accepted captureless binding. If the
   consumer expects `CallbackN`, normal callback wrapping must still be
   represented in HIR; the binding itself must not be mislabeled as an
   interface object.
4. Retire the pending entry only after the result is made total: concrete
   function type, or clean primary diagnostic plus diagnosed-Unknown cause.
5. Clear cause state on every concrete result.
6. Reject bare capturing lambdas with the approved v1 primary, anchored at the
   original lambda/binding, and make later uses quiet for that same cause.
7. With no contextual parameter types, an unconstrained lambda must receive
   one clean “cannot infer” primary; never publish a `LambdaFnSpec` containing
   `Unknown` ABI parameters.
8. Preserve `fnptr_consts_by_node_id`/`LambdaFnSpec` publication and the final
   `_apply_fnptr_consts` rewrite so accepted aliases lower as actual function
   pointers.

The HVar hook should be considered for *any reference to a pending binding*,
not only `_copy_use=True`: `defer_value_use`, explicit `move`, borrow, discarded
reads, return values, and call arguments change Copy policy but do not make an
`Unknown` placeholder a valid value type. Exact contexts still need empirical
coverage before declaring this one hook total.

## Selected causal-state shape (details remain falsifiable)

Planning round 2 disproved a binding-only table: caused Unknown values remain
quiet through HMove and a ternary before being rebound. Use `FnCheckState`-
owned binding-id and expression-node maps, storing immutable cause metadata
rather than booleans. A useful minimum is:

- cause category;
- root producer binding id;
- producer node id or stable source span;
- primary diagnostic code/category when available.

Operations:

- `mark(binding_id, cause)` only from producer-local evidence;
- mark/query expression nodes only for the tested transparent value flow;
- `propagate(dst, expr)` only when the initializer's effective type remains
  Unknown and that exact expression carries a cause;
- `clear(binding_id)` whenever the binding becomes concrete;
- `query(binding_id)` at copy/HCall/HInvoke consumers.

Producer-local evidence means a diagnostic watermark around that producer,
not a scan of the whole function. Ordinary `HLet` needs two paths:

- new primary emitted and result remains Unknown → create cause;
- no new primary, but a direct source expression already carries a cause →
  propagate it.

The original required red probe was:

```drift
val bad = missing_name;
val x = bad();
x();
```

The cause must cross the HCall node before `HLet` can attach it to `x`.

Planning review confirmed the call-result example: current global suppression
keeps only the original unknown-name diagnostic. The minimum proven extension
is therefore a cause mark on the *suppressed binding-call node*, propagated by
its receiving HLet. Round 2 then proved caused HMove and literal-selected
HTernary propagation as well. The ternary authority must be reachability-aware:
one caused arm may not silence a distinct uncaused Unknown arm.

Do not extend this into a generic “Unknown child means caused parent” rule.
Unproven shapes fail toward the downstream tripwire; the test matrix governs
future additions.

## Likely shared finalization authority

Current HCall and HInvoke each duplicate pending-lambda resolution. The child
now adds non-call HVar uses. The leading consolidation is one local helper in
`check_function`, conceptually:

```text
finalize_pending_lambda(binding_id, expected_value_type, reason)
    -> concrete function type | diagnosed Unknown | not pending
```

This is intentionally a contract sketch, not a required signature. It should:

- derive an expected *function* shape from a concrete Fn/Callback context;
- type the HLambda once through the primary lambda authority;
- own capture rejection, unconstrained-parameter rejection, binding update,
  cause mark/clear, pending retirement, and spec/fnptr publication;
- serve HCall, HInvoke, ordinary HVar references, and final drain;
- preserve the outer probe barrier behavior instead of adding a second
  transaction policy.

If K proves that call-directed inference and value-directed finalization need
separate helpers, the split should still share one mutation/retirement
authority and one diagnostic contract.

## Acceptance matrix

### Diagnostic causality

- unrelated-error + independent preseeded Unknown copy → `E-COPY-UNKNOWN`;
- unrelated-error + independent preseeded Unknown HCall → call-target error;
- ordinary diagnosed producer `val bad = missing_name; bad();` → primary only;
- same diagnosed binding through HCall and HInvoke → primary only;
- one-hop direct alias of diagnosed Unknown → primary only at later use;
- shadowed same-name bindings → no cross-suppression;
- cause-table rollback, nested rollback, commit, fingerprint, and detached
  public-result contracts are pinned.

### Pending finalization

- inferable captureless alias compiles and runs;
- contextually typed callback alias compiles/runs and records its real wrapper;
- unconstrained alias yields one clean cannot-infer primary;
- later direct call after an earlier alias is no longer order-sensitive;
- explicit and implicit capturing bare bindings yield one approved primary,
  without earlier `E-COPY-UNKNOWN`;
- return, argument, move/borrow, and discarded-reference positions have
  deliberate tests or documented exclusions;
- direct HCall/HInvoke behavior remains green;
- lowering-visible positives include full compile/run companions.

## Scope and guardrails

- New regression files are preferred. Existing tests/comments require
  Slawomir's explicit approval before editing.
- No language-spec change is proposed or authorized. The captureless child
  implements the already-approved callable contract.
- No runtime/ABI shape change is currently supported; ABI should remain 22.
- Both diagnostic output and accepted captureless programs are user-visible.
  If `0.35.0` remains unreleased/uncertified, keep it and fold the history note
  into that pending entry. Otherwise apply the repository's mandatory minor
  bump rule.
- Do not patch stdlib or user programs around either defect.
- Do not remove or weaken the newly landed `PendingLambdaBarrier` to make
  first-reference finalization convenient.

## Scheduling

The parent and value-finalization child should be implemented together if the
shared finalizer remains the narrowest sound authority. K may split them after
red-first evidence if the child requires a materially broader callable
contract or lowering change. Either direction must be recorded; this research
does not decide it by assertion.

## Planning review round 1 (2026-08-05)

K's localized `probe_planning_review_matrix.py` produced five useful results:

- binding-only causality is disproved by a suppressed-call-result → HLet →
  later-call chain;
- pending references under `move` still inherit Unknown, and borrowing a
  pending binding is silently accepted as Unknown storage;
- direct typed Callback initialization reaches MIR as a raw HLambda;
- rejected bare captures show no current evidence of wrapper construction or
  capture effects, but the no-effect contract still needs a regression;
- unconstrained final drain rejects before publishing any LambdaFnSpec.

The plan is not ready yet. Remaining planning evidence is deliberately narrow:

1. caused-Unknown propagation through `move` and a literal-selected ternary;
2. a true compatible *bare pending HVar argument* (not `f()` as an argument);
3. a named/concrete function-pointer borrow control establishing what `&f`
   should type/lower as;
4. direct Callback typed-let structural confirmation sufficient to design the
   restoration without changing its established contract.
