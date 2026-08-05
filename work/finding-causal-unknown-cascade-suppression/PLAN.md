# Plan: causally scoped Unknown cascade suppression

Refreshed: 2026-08-04 against the committed pending-`0.35.0` tree.

This is a research-backed work order, not an authoritative patch recipe. The
implementer owns `PROGRESS.md`, must reproduce the failures, and should record
where code evidence contradicts the reviewer. While the current full suite is
active, Slawomir authorizes only isolated probe additions/runs inside this
finding directory; those probes may invoke the compiler. Shared compiler,
repository-test, fixture, spec, history, and infrastructure files remain
read-only until the suite clears and Slawomir starts the implementation slice.

## Phase 0 — mandatory start gates

1. Read `FINDING.md`, `EVIDENCE.md`, this plan, and the work-only probe in full.
2. Read current `AGENTS.md` and `AGENTS-MAILBOX-PROTO.md`.
3. Scan `doc/refactor_triggers.md` at actual start. The reviewer found no match
   on 2026-08-04; K must make the independent current-tree determination.
4. Check `/tmp/drift-announce/` and current version/certification state.
5. Create implementer-owned `PROGRESS.md`; separate observed facts, hypotheses,
   and decisions. Do not edit reviewer `review-*.md` files.

## Phase 1 — reproduce before compiler edits

Run the existing work probe and retain the complete diagnostic lists:

```sh
./.venv/bin/python3 -m pytest -q work/finding-causal-unknown-cascade-suppression/probe_causal_unknown_suppression.py
```

Expected from the last executed baseline: two failures because only the
unrelated first copy diagnostic survives. If either test is now green, stop and
explain which intervening change invalidated the finding before implementing.

Run unchanged same-binding guards:

```sh
./.venv/bin/python3 -m pytest -q lang/tests/driver/test_stored_capturing_lambda_diagnostic.py
./.venv/bin/python3 -m pytest -q lang/tests/driver/test_uninvoked_stored_lambda.py
./.venv/bin/python3 -m pytest -q lang/tests/type_checker/test_type_checker_copy_unknown.py
```

Record exact counts/messages/spans for the relevant cases, not only pass/fail.

## Phase 2 — design and run isolated work probes

While the full suite remains active, place every new probe under this finding
directory. Probe the `HInvoke`, ordinary diagnosed producer, pending value-read,
one-hop alias, concrete recovery, and shadowing cases from `EVIDENCE.md`. Keep
the invocations narrow and record complete diagnostic streams in `PROGRESS.md`.
Do not edit shared tests or compiler code in this phase.

## Phase 3 — add in-tree red regressions after the suite gate

Prefer a new file such as
`lang/tests/type_checker/test_causal_unknown_cascade_suppression.py` so the
strict existing-test approval gate is not crossed. Add, before the fix:

1. unrelated first error + independent preseeded Unknown `HVar` value use →
   `E-COPY-UNKNOWN` must still appear;
2. unrelated first error + independent preseeded Unknown `HCall(fn=HVar)` → one
   call-target diagnostic must still appear;
3. ordinary source producer (`val bad = missing_name; bad();`) → primary only,
   if the isolated probe confirms that contract;
4. same diagnosed stored-lambda binding through `HCall` → primary only;
5. same diagnosed binding through synthetic `HInvoke` → decide and pin parity;
6. shadowed same-name/different-id bindings → no cross-suppression;
7. pending captureless lambda resolving concrete → no stale cause.

Before choosing the representation, add focused source or synthetic probes for:

- bare value read of a pending capturing lambda before final flush;
- one-hop alias from a diagnosed Unknown binding, then copy/call through the
  alias.

These two probes decide whether exact-binding marking is total, needs explicit
cause propagation, or should be split into a child finding. They must not be
silently ignored because the minimal two-test patch happens to pass.

If modifying any existing test/comment becomes necessary, publish an
`APPROVAL-PENDING-*` proposal with exact paths/assertions and wait for
Slawomir's approval. Adding new tests does not authorize rewriting old ones.

## Phase 4 — trace the state boundary

Audit all current writes from the inventory in `EVIDENCE.md`. For each write,
answer:

- Can it leave this binding `Unknown`?
- Was a new primary error emitted by this producer visit?
- Can the write occur during a deferred resolver transaction?
- Can it overwrite a previously marked binding with a concrete type?
- Can another binding inherit this Unknown without a new diagnostic?

Also trace all three `make_call_ctx(...)` sites and the separate `HInvoke`
branch. Do not route causality through `binding_id_by_name` when the expression
already carries its lexical binding id.

## Phase 5 — select the authority

The current leading candidate is an `FnCheckState`-owned `_TxnDict` keyed by
binding id and storing an immutable cause description. Requirements:

- include the table in `OWNED_TABLES`, so transaction logs and
  `state_fingerprint()` cover it;
- mark only from producer-local evidence (new primary error and Unknown result,
  or a separately justified guaranteed-later primary state);
- clear on concrete resolution;
- propagate only where a regression establishes causal value flow;
- expose a read-only exact-binding predicate to `CallResolverContext`;
- do not place transaction wrappers on returned `TypedFn`/`TypeCheckResult`.

Reject this design if the Phase 2 probes show that binding identity cannot
represent the necessary provenance. In that case, document the smallest
node/expression provenance authority that can, and reassess scope before code.

## Phase 6 — implement consumers and producers

Suggested order, conditional on the selected design:

1. Add the transaction-owned cause state and small mark/clear/query helpers.
2. Mark ordinary diagnosed-Unknown `HLet` producers using a local diagnostic
   watermark; do not infer cause from preexisting diagnostics.
3. Mark/clear pending-lambda `HCall` and `HInvoke` resolution around the one
   real `type_expr(pending, expected_type=...)` visit.
4. Mark final-flush rejections for state totality, even if no later source
   statement can consume them.
5. Implement explicitly justified alias/pending propagation discovered by the
   red probes.
6. Replace `_require_copy_value`'s global scan with an exact-cause query for
   binding-backed Unknown values. Leave unmarked/non-binding Unknown values as
   tripwires unless separate provenance proves suppression.
7. Thread the predicate into every `make_call_ctx(...)` construction and
   replace the resolver's global scan.
8. Apply the same deliberate policy to `HInvoke`; do not duplicate a second
   causality authority.
9. Remove or rewrite the two comments that currently claim a causal relation
   their predicates do not prove. If those comments are in existing test files,
   obtain approval first; source-comment corrections are in scope.

Do not add a broad “diagnostics nonempty” fallback for missing cause state. A
missing marker should fail toward emitting the tripwire, which is the behavior
this finding is restoring.

## Phase 7 — transaction and invariant teeth

In a new focused test where possible, prove:

- cause-table mutation rolls back to byte-for-byte-equivalent fingerprint;
- committed mutation remains;
- nested transaction commit followed by outer rollback restores the original;
- the cause table is named in `state_fingerprint()`;
- no `_TxnDict`/owner leaks through public result objects;
- rebinding/shadowing cannot inherit a cause by source name.

If production never mutates the cause table inside today's allowed probe
shapes, keep the rollback test anyway: membership in `FnCheckState` is an
explicit future-safety contract, not a claim about current reachability.

## Phase 8 — focused verification

Minimum focused commands, adjusted for new paths:

```sh
./.venv/bin/python3 -m pytest -q work/finding-causal-unknown-cascade-suppression/probe_causal_unknown_suppression.py
./.venv/bin/python3 -m pytest -q lang/tests/type_checker/test_causal_unknown_cascade_suppression.py
./.venv/bin/python3 -m pytest -q lang/tests/type_checker/test_type_checker_copy_unknown.py
./.venv/bin/python3 -m pytest -q lang/tests/driver/test_stored_capturing_lambda_diagnostic.py
./.venv/bin/python3 -m pytest -q lang/tests/driver/test_uninvoked_stored_lambda.py
./.venv/bin/python3 -m pytest -q lang/tests/checker/test_defer_probe_state_transaction.py
```

Then run the checker/type-checker/driver suites proportionate to touched code.
Do not start another full suite or corpus run until review converges and the
user schedules it.

## Phase 9 — version/history and handoff

- No spec change unless Slawomir explicitly approves one.
- ABI remains 22 unless actual compiler/runtime boundary evidence requires an
  ABI change.
- If `0.35.0` is still unreleased/uncertified, keep that version and fold this
  user-visible diagnostic fix into its existing history entry. Otherwise apply
  the mandatory pre-1.0 minor-bump rule from the current release state.
- Record red/green evidence, exact files, disagreements, approval needs, and
  unresolved children in `PROGRESS.md`.
- Publish `work/IMPL-PENDING-<timestamp>` only when the implementation and
  focused verification are ready for review. Its sole payload is the relative
  path to `PROGRESS.md`.
