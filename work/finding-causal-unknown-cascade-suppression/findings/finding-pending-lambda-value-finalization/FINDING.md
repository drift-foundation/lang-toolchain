# Child finding: pending stored lambdas do not finalize on value reference

Date filed: 2026-08-05

Parent: `finding-causal-unknown-cascade-suppression`.

Status: confirmed `LANGUAGE_BUG`, research-ready, proposed fold-in with the
parent. `PROGRESS.md` is intentionally absent until K starts implementation.

## User-visible defect

An unannotated stored lambda declaration installs `Unknown` plus a pending
entry. Only direct HCall/HInvoke and end-of-function drain resolve it. Every
ordinary HVar reference reads the placeholder.

Executed examples:

```drift
val f = || => { 7 };
val g = f;
return g() - 7;
```

and a contextual Callback alias both fail with `E-COPY-UNKNOWN`. A later direct
`f()` does not repair the earlier alias error. Return positions also see
Unknown.

This violates the existing v1 contract: non-capturing closures lower to thin,
Copy function pointers and may be stored/returned freely. Capturing bare
lambdas remain invalid and must still reject with the approved primary.

## Why it is coupled to the parent

Value finalization supplies the missing causal producer boundary:

- captureless success changes pending Unknown to concrete Fn and clears cause;
- capturing or unconstrained failure emits one primary and marks that exact
  binding as diagnosed Unknown;
- later copy/HCall/HInvoke consumers can query the parent's causal authority;
- direct aliases can propagate that cause without function-global scans.

The just-landed `PendingLambdaOwner` barrier also makes a mutation-site
first-reference hook viable under deferred-call transactions. Reimplementing a
second pending map or bypassing `begin_resolution()` is forbidden by the
rollback contract.

## Leading implementation shape (falsifiable)

Extract one pending finalizer shared by:

- current HCall pre-resolution;
- current HInvoke pre-resolution;
- ordinary HVar binding references;
- final pending drain.

The helper should accept exact binding identity and any real contextual value
type, derive a function expectation where possible, and produce one total
outcome:

1. not pending;
2. concrete thin function type + fnptr/spec publication + cause clear;
3. diagnosed Unknown + cause mark, with no invalid spec publication.

If the consumer expects a Callback interface, use that shape to infer the
lambda but install a thin function type on the original binding. The normal
callback conversion must remain an explicit HIR/lowering-visible wrapper.

## Context inventory requiring proof

- direct/indirect call callee;
- ordinary HLet alias;
- annotated Callback alias;
- return value;
- compatible call argument;
- explicit move and borrow;
- discarded `f;` reference;
- no-context unconstrained lambda;
- implicit and explicit capturing lambda;
- reference reached under a deferred resolver probe.

`used_as_value=False` suppresses Copy consumption in several of these paths;
it does not justify returning Unknown for the binding.

Planning round 2 settles two contexts:

- pending `&f` must finalize and accept, matching the compile/run behavior of
  borrowing the same binding after an earlier call finalized it;
- pending `f` in a Callback argument must finalize the binding to a thin Fn
  and then let the argument slot insert a real callback wrapper. Storing the
  Callback interface type on `f` produces an uninitialized-local MIR failure.

The named-function `&seven` control exposed a separate canonical-place defect,
tracked in sibling `finding-fnptr-borrow-materialization` and folded into the
same family slice.

## Boundary risks

- Type-checking a lambda with a Callback interface expectation must not label
  its binding as an interface value without constructing the wrapper.
- No `LambdaFnSpec` may retain Unknown parameter/return ABI types.
- `_apply_fnptr_consts` must still replace the stored HLambda so MIR never sees
  a raw lambda value.
- Captureless aliasing is lowering-visible and needs full compile/run tests.
- Capturing storage must not begin executing copy/move/share capture effects
  merely because the invalid bare value is referenced; current behavior and
  intended construction timing need a focused probe.
- Barrier propagation must remain structured and outermost-only.

## Acceptance criteria

- inferable captureless aliases compile and run;
- contextual Callback aliases compile/run with a real wrapper mark;
- source order (alias before/after direct call) does not change validity;
- unconstrained aliases produce one clean cannot-infer primary;
- bare capturing aliases produce one approved storage primary and no
  E-COPY-UNKNOWN/call-target cascade;
- all promised HVar contexts are pinned;
- direct HCall/HInvoke and final uninvoked flush retain their existing valid
  contracts;
- pending rollback barrier tests remain green;
- no spec or ABI change.

The contextual Callback target is no longer blocked on a language ruling.
Typed-let bare-lambda implicit wrapping is an established 0.31.17 contract;
the sibling `finding-typed-let-callback-wrap-regression` restores the broken
direct form. Alias finalization should then flow through that same explicit
wrapper authority rather than inventing a second conversion.

## Scope decision

Fold this child into the parent if one shared finalizer and one cause authority
solve both without widening callable semantics. Split it if evidence requires a
new representation, new lowering contract, or a materially larger ownership
change. Either decision belongs in K's evidence, not in reviewer fiat.
