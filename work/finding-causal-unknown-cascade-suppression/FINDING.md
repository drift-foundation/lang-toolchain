# Finding: Unknown cascade suppression is not causally scoped

Date filed: 2026-08-03

Origin: R5 static review of `finding-nonflat-divergent-lambda`.

Status: queued reviewer research. The implementer must revalidate every claim
against the tree they pick up; `PROGRESS.md` remains implementer-owned and is
intentionally not created or edited by this research pass.

## Classification

**Observed:** two checker guards suppress a diagnostic whenever *any* earlier
error exists in the function:

- `lang/driftc/type_checker.py`, `_require_copy_value`: the `Unknown` branch
  suppresses `E-COPY-UNKNOWN` using a function-global scan of `diagnostics`.
- `lang/driftc/checker/call_resolver.py`, the local-binding `HCall(fn=HVar)`
  branch: an `Unknown` callee suppresses `call target is not a function value`
  using the same function-global condition.

**Confirmed:** neither guard currently proves that the earlier diagnostic was
produced by the expression or binding whose type is `Unknown`.

**Provisional classification:** `LANGUAGE_BUG` in diagnostic soundness. An
invalid program can lose an independent error solely because an unrelated
error appeared earlier in the function. This does not change accepted-program
semantics, but it is user-visible compiler output.

This classification is falsifiable. In particular, the implementer should
check whether the checker intentionally promises only one error per poisoned
function. No such contract was found in the reviewed code or tests, and the
comments at both guards make the narrower claim that the prior error explains
the same poisoned slot.

## Required contract

Two distinct cases must remain distinguishable:

1. A binding's producer emitted a primary diagnostic and assigned that exact
   binding `Unknown`. Later copy/call diagnostics over the same binding are
   cascades and should be suppressed.
2. An unrelated expression emitted an earlier error, while a different
   `Unknown` binding has no causal diagnostic. Its copy/call tripwires must
   still fire.

The existing stored-capturing-lambda driver pin covers case 1 only. It cannot
detect the function-global over-suppression in case 2.

## Minimal regression shape

`probe_causal_unknown_suppression.py` constructs a function with:

- one unrelated invalid explicit copy, establishing an earlier diagnostic;
- a separate preseeded `Unknown` binding with no poison marker;
- either a value use of that binding or a call through it.

The preseed is deliberate. A source program normally reaches `Unknown` through
an upstream diagnostic; the checker-unit boundary is the direct way to exercise
the last-line invariant that the current comments explicitly claim to retain.
The regression should fail before the fix because the independent second
diagnostic is absent.

## Proposed patch direction

**Proposed, not authoritative:** replace the function-global diagnostic scan
with an explicit causal poison table keyed by binding identity. A small value
such as the producer node id, diagnostic watermark, or reason enum would make
debugging clearer than a bare boolean, but the minimum semantic requirement is
exact binding identity.

Likely properties of a sound implementation:

- The table is owned by `FnCheckState` and uses `_TxnDict`, with its name added
  to `OWNED_TABLES`. Deferred call probes can roll back diagnostics and other
  checker tables; a plain set/dict outside that owner could leak poison from a
  rolled-back probe.
- A producer marks a binding only when typing that producer both leaves the
  binding `Unknown` and emits the diagnostic that explains it. A diagnostic
  watermark around the producer is one possible test; merely observing
  `Unknown` is insufficient.
- `_require_copy_value` suppresses only when its expression resolves to a
  binding present in that table. Non-binding `Unknown` expressions need their
  own causal identity or should retain the tripwire.
- `CallResolverContext` receives a read-only view or predicate for the poison
  table. The local-binding `HCall` branch suppresses only when that exact
  `binding_id` is marked.
- If a pending binding later resolves to a concrete function type, any stale
  poison entry for it is cleared.

The narrowest valid implementation may mark only the rejected stored-lambda
producer that motivated the cascade cleanup. Before generalizing to every
`Unknown` producer, audit which producer diagnostics genuinely make subsequent
copy/call errors redundant.

Candidate producer sites to audit include:

- deferred unannotated lambda resolution at first `HCall`;
- the analogous `HInvoke` resolution;
- end-of-function flush of never-invoked stored lambdas;
- ordinary `HLet` initializers that diagnose and leave the binding `Unknown`.

## Acceptance criteria

- Existing stored-capturing-lambda presentation remains exactly one primary,
  spanned diagnostic; its `E-COPY-UNKNOWN` and call-target cascades stay absent.
- An unrelated first error does not suppress `E-COPY-UNKNOWN` for a different,
  uncaused `Unknown` binding.
- An unrelated first error does not suppress `call target is not a function
  value` for a different, uncaused `Unknown` binding.
- If poison state joins `FnCheckState`, the transaction/fingerprint invariant
  suite proves commit and rollback behavior and proves returned `TypedFn`
  objects do not retain transaction wrappers.
- No raw node/name heuristic replaces binding identity; shadowing must not
  cross-contaminate poison state.

## Version/spec/ABI notes

- No language-spec change is proposed or authorized.
- No compiler/runtime ABI shape change is expected.
- Because diagnostics are user-visible, the repository version rule appears to
  require a compiler SemVer minor bump unless the implementer proves the change
  user-neutral. Re-evaluate against the version already selected for the larger
  branch before editing the version.

## Refactor-trigger scan

**Observed:** `doc/refactor_triggers.md` was scanned on 2026-08-03. No registered
trigger clearly matches causal diagnostic provenance. The implementer must scan
again when starting the `LANGUAGE_BUG`, because the registry and current tree
may have changed.

## Open questions

- Is binding-level poison sufficient for all desired suppressions, or does an
  `Unknown` temporary/node also need a causal marker?
- Should one producer reason suppress both copy and call-target diagnostics, or
  should each consumer opt into named poison categories?
- Can a pending lambda be diagnosed speculatively inside a transaction today,
  or is transaction ownership defensive future-proofing only? The table should
  still respect the existing state-owner contract if added there.
