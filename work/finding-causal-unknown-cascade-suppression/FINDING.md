# Finding: Unknown cascade suppression is not causally scoped

Date filed: 2026-08-03

Refreshed: 2026-08-04 against the committed, pending `0.35.0` tree while the
repository full suite was running. This refresh changed finding documents only;
it deliberately did not run probes or touch compiler/test files.

Origin: R5 static review of `finding-nonflat-divergent-lambda`.

Status: queued as the next substantive finding after the current full-suite
gate. `finding-true-statement-throwing-iife` remains separate because it is a
small coverage audit of already-landed IIFE routing, whereas this finding is a
checker diagnostic-state defect with its own state/transaction boundary.

`PROGRESS.md` remains implementer-owned and is intentionally absent until K
starts the pass. Every claim below is evidence or a patch hypothesis, not an
instruction to trust the reviewer over the code. K should reproduce the red
boundary and reject or revise the proposed design if a narrower invariant or a
counterexample emerges.

## Classification

**Statically reconfirmed on 2026-08-04:** two guards suppress a diagnostic when
*any* earlier error exists in the current function:

- `lang/driftc/type_checker.py:4012-4036`, `_require_copy_value`: an
  `Unknown` value bypasses `E-COPY-UNKNOWN` after a function-global scan of
  `diagnostics`.
- `lang/driftc/checker/call_resolver.py:6623-6719`, the local-binding
  `HCall(fn=HVar)` route: an `Unknown` callee bypasses `call target is not a
  function value` under the same function-global condition.

Neither guard checks whether the earlier error came from the expression,
binding, or producer responsible for the `Unknown` being consumed.

**Previously reproduced, not rerun during the active full suite:** the two
work-only boundary probes fail because an unrelated bad copy is the sole
diagnostic; the independent Unknown-copy and Unknown-callee tripwires vanish.

**Provisional classification:** `LANGUAGE_BUG` in diagnostic soundness. An
invalid program can lose an independent error solely because another error was
reported earlier. This changes user-visible compiler output, not successful
program semantics.

This classification remains falsifiable. If K finds a documented one-error-per-
function policy, or proves that the checker boundary cannot contain an
undiagnosed `Unknown`, that evidence should replace this finding's premise.
The current comments claim the narrower condition—an earlier error explains the
same poisoned slot—and the preseed boundary is explicitly supported by
`check_function`, so the present evidence points the other way.

## Required distinction

The patch must preserve three cases rather than replacing one global heuristic
with another:

1. **Diagnosed same-source poison.** A producer emitted a primary diagnostic and
   left a particular binding `Unknown`. Copy/call complaints over that same
   poisoned value are normally cascades and should remain absent.
2. **Independent Unknown.** A different expression emitted an earlier error,
   while this `Unknown` binding has no causal diagnosis. Copy/call tripwires for
   this binding must still fire.
3. **Concrete recovery.** If a deferred binding later resolves to a concrete
   function type, no stale poison state may suppress a real later error.

The existing stored-capturing-lambda driver test pins case 1. The work-only
probe pins case 2. Case 3 and transaction/shadowing isolation still need red-
first coverage if the chosen representation can retain state.

A non-lambda source companion should also pin case 1 independently of closure
machinery, for example `val bad = missing_name; bad();`: the unknown-name
diagnostic causally explains `bad`'s Unknown type, so copy/call noise over
`bad` should be suppressed even though no pending lambda is involved. This is
a proposed contract probe; confirm its current diagnostics before adopting the
expected count.

## Confirmed consumers and adjacent parity surface

### Copy consumer

`_require_copy_value` receives the current `expr`. For `HVar`, the already-
resolved `binding_id` is available; no name lookup or syntax guess is needed.
Calls for constants/projections do not always carry a binding expression, so a
binding-level cause must not accidentally suppress an unrelated non-binding
Unknown.

### `HCall(fn=HVar)` consumer

The call resolver computes `binding_id` before typing the callee and retains it
through its non-function fallback. A read-only predicate on
`CallResolverContext` can therefore ask about the exact binding. All current
`make_call_ctx(...)` construction sites are in `type_checker.py` (currently
near lines 8518, 10003, and 10310); every constructor must be updated if a new
required context field is introduced.

### `HInvoke` parity audit

**New static observation:** `type_checker.py:10010-10115` contains a separate
`HInvoke` non-function fallback that always appends `call target is not a
function value`. It is not one of the two function-global suppressors, but it
can consume the same pending/poisoned binding. Parsed stored-lambda calls are
currently pinned as `HCall(fn=HVar)`, while `HInvoke` remains a real internal
boundary used by synthetic tests. K should add a parity probe before deciding
whether the causal predicate belongs in both consumers. Do not silently leave
two different cascade policies for semantically equivalent function-value
calls.

## Producer/state inventory

Current binding writes worth auditing (line numbers are navigational):

- `type_checker.py:12318-12331`: an unannotated stored `HLambda` is deferred
  with binding type `Unknown` and **no diagnostic yet**.
- `type_checker.py:9986-10001`: first `HCall` through a pending lambda types the
  lambda, stores its function type or `Unknown`, then removes it from the
  pending table.
- `type_checker.py:10019-10033`: analogous `HInvoke` path.
- `type_checker.py:12331-12464`: an ordinary `HLet` types its initializer and
  records the resulting binding type. A diagnosed initializer can therefore
  create a non-lambda poisoned binding.
- `type_checker.py:13760-13823`: end-of-function pending-lambda flush emits the
  bare-capture/unconstrained-type primary diagnostics and leaves rejected
  bindings `Unknown`.
- Parameter, catch-binder, and match-binder writes elsewhere in the file use
  concrete or `Error` types; do not mark them merely because an unrelated
  diagnostic exists.

`FnCheckState` currently owns every mutable side table that a deferred call
probe may touch. `OWNED_TABLES` drives both transaction rollback and
`state_fingerprint()`. If causal state is mutable during expression typing, it
belongs in that owner as `_TxnDict`, even if today's shape gate makes some
producer paths unreachable from probes. A plain closure `dict` would create a
future rollback leak.

## Additional suspected edge: use before pending-lambda flush

This was not in the original finding and has not been executed during the
current suite, so treat it as a hypothesis requiring a red-first probe:

```drift
val x = 1;
val f = || => { x }; // stored lambda: binding is Unknown, no diagnostic yet
val alias = f;       // ordinary HVar value read happens before final flush
```

Static control flow suggests `_require_copy_value` can emit `E-COPY-UNKNOWN` at
`alias = f` before the final flush later emits the primary borrowed-capture
rejection. If confirmed, a table containing only "a diagnostic has already
been emitted for this binding" cannot by itself preserve the intended one-
primary presentation. Possible resolutions include resolving/rejecting the
pending lambda at its first value use, representing a narrowly-scoped
"primary diagnosis is guaranteed at flush" state, or treating the early copy
error as independently meaningful. K must determine the contract from source
behavior and existing v1 closure rules rather than assuming this review's
preferred outcome.

Also probe one alias hop from an already diagnosed binding. A new binding that
inherits `Unknown` from a causally poisoned `HVar` may need cause propagation;
otherwise the first binding is quiet but use of its alias can produce a fresh
cascade. If that occurs, either define an explicit propagation rule or keep the
patch deliberately narrow and record the uncovered case as a child finding.
Do not infer causality from “some descendant is Unknown” or from a name match.

## Proposed patch shapes (not authoritative)

### Narrow binding-cause table

The smallest design matching the confirmed red boundary is a transaction-aware
map keyed by integer binding id, for example
`unknown_cause_by_binding: _TxnDict[int, UnknownCause]`.

Useful cause data would include at least a stable category/reason and producer
node or diagnostic identity. A bare boolean is sufficient for suppression but
makes propagation and debugging ambiguous. The semantic invariant should be:

> A binding is present only when its current `Unknown` type is causally
> explained by a specific primary failure (or, if separately justified, a
> pending producer whose final primary failure is guaranteed).

Likely operations:

- mark after a producer-local diagnostic watermark observes a new error and the
  producer leaves this binding `Unknown`;
- propagate only through explicitly justified value-flow shapes;
- clear whenever the binding becomes concrete;
- query by exact `binding_id` in `_require_copy_value`, `HCall`, and—if the
  parity probe proves it—`HInvoke`;
- never seed preexisting/preseeded Unknown bindings automatically.

### Wider expression provenance

If source probes show causes must flow through calls, casts, aliases, or other
Unknown-producing expressions, a binding-only map may be an attractive but
incorrect local patch. A node-level Unknown provenance table, or a structured
typing result, may be the sounder authority. That is a larger change and is not
justified by the two current failures alone. Prefer the narrow design only if
the regression matrix proves it total for the promised suppression surface.

In either design, a diagnostic watermark is necessary but not sufficient:
checking `diagnostics[start:]` prevents an unrelated *earlier* error from being
misattributed, but a recursive producer can emit several errors and aliasing
can carry a cause without emitting a new one.

## Acceptance criteria

- Existing invoked and uninvoked stored-capturing-lambda cases retain exactly
  one primary, spanned diagnostic; no `E-COPY-UNKNOWN` or redundant call-target
  message appears for the same poisoned binding.
- An unrelated earlier error does not suppress `E-COPY-UNKNOWN` for a distinct,
  uncaused `Unknown` binding.
- An unrelated earlier error does not suppress `call target is not a function
  value` for a distinct, uncaused `Unknown` binding.
- `HCall(fn=HVar)` and `HInvoke(callee=HVar)` have deliberately tested parity,
  or the implementer documents why their contracts differ.
- A binding that resolves from pending `Unknown` to a concrete function type
  has no stale cause marker.
- Shadowed bindings with the same source name do not share cause state.
- Any transaction-owned cause state commits/rolls back exactly and is included
  in the owner fingerprint. Returned `TypedFn`/`TypeCheckResult` objects must
  not retain transaction wrappers.
- The pending-lambda value-read and one-hop alias hypotheses are tested and
  either handled in this slice or recorded explicitly as child findings with a
  justified scope boundary.
- No function-global `any(error in diagnostics)` remains as an Unknown-cascade
  causality test.

## Test-edit boundary

New regression files may be added regression-first. Existing test files and
their comments must not be edited without Slawomir's explicit approval. The
existing stored-lambda tests can be run unchanged as compatibility guards. If
K concludes an existing assertion/comment needs correction, use the mailbox
approval protocol before editing it.

## Version/spec/ABI

- No language-spec change is proposed or authorized.
- No compiler/runtime ABI shape change is expected; ABI should remain 22 unless
  implementation evidence finds a real boundary change.
- Diagnostic behavior is user-visible. If this lands on the still-unreleased,
  uncertified `0.35.0` train, keep `DRIFTC_VERSION` at `0.35.0` and fold a
  concise note into that pending history entry. If `0.35.0` is certified or
  released first, reapply the repository's mandatory minor-bump rule rather
  than assuming this old work order controls versioning.

## Refactor-trigger and announcement scan

`doc/refactor_triggers.md` was statically rescanned on 2026-08-04. No registered
entry matches causal diagnostic provenance. K must scan it again at actual
LANGUAGE_BUG start, because the registry can change.

No files were present in `/tmp/drift-announce/` at refresh time.
