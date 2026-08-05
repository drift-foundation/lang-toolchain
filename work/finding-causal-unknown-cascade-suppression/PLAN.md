# Plan: causal Unknown provenance + callable-value finalization/materialization

Refreshed: 2026-08-05 against committed pending `0.35.0`; planning round 2
selected expression-aware cause flow and exposed two additional
callable-materialization boundary failures.

Status: ready for implementation. Planning was accepted by reviewer and
implementer on 2026-08-05, and the exact existing-test comment ledger was
approved by Slawomir. Slawomir intentionally stopped the preceding
`run_all_tests.sh` run after its observed phases remained clean and explicitly
cleared that start gate on 2026-08-05. This is a human gate waiver, not a claim
that the interrupted suite reached its terminal summary. The full scope is
deliberately deferred until the last queued finding is fixed, when one final
full-suite run will validate the accumulated train. Shared implementation is
authorized under the accepted plan; retain this finding tree through that
deferred run rather than cleaning it up after the current slice.

This is a falsifiable work order. K owns `PROGRESS.md`, should preserve prior
empirical results, and should replace any proposed design that current traces
or red tests disprove.

## Phase 0 — start gates

1. Read parent `FINDING.md`, `EVIDENCE.md`, implementer `PROGRESS.md`, and the
   nested value-finalization child's `FINDING.md`/`PLAN.md`.
2. Confirm the active full suite has completed cleanly before shared edits.
3. Read current `AGENTS.md` and `AGENTS-MAILBOX-PROTO.md`; use Baton for every
   handoff.
4. Re-scan `doc/refactor_triggers.md`. The 2026-08-05 reviewer scan found no
   matching trigger; this must be independently current at implementation.
5. Check `/tmp/drift-announce/`, `DRIFTC_VERSION`, ABI, and certification state.
6. Record whether the parent and child remain one implementation slice. A
   split is allowed, but must name the proven boundary rather than merely
   choosing the smallest diff.

## Phase 1 — preserve and rerun the red baselines

Run the existing work-only probes first and retain complete ordered diagnostic
streams:

```sh
./.venv/bin/python3 -m pytest -q work/finding-causal-unknown-cascade-suppression/probe_causal_unknown_suppression.py
./.venv/bin/python3 work/finding-causal-unknown-cascade-suppression/probe_preflight_hypotheses.py
./.venv/bin/python3 work/finding-causal-unknown-cascade-suppression/probe_pending_alias_matrix.py
./.venv/bin/python3 work/finding-causal-unknown-cascade-suppression/probe_txn_and_value_positions.py
```

Revalidate each script's invocation contract before running; some are pytest
modules and some are standalone probes. If a baseline changed after the
rollback child, explain the mechanism before adapting expectations.

Run unchanged compatibility guards:

```sh
./.venv/bin/python3 -m pytest -q lang/tests/driver/test_stored_capturing_lambda_diagnostic.py
./.venv/bin/python3 -m pytest -q lang/tests/driver/test_uninvoked_stored_lambda.py
./.venv/bin/python3 -m pytest -q lang/tests/type_checker/test_type_checker_copy_unknown.py
./.venv/bin/python3 -m pytest -q lang/tests/checker/test_pending_lambda_probe_barrier.py
```

## Phase 2 — preserve the resolved planning matrix

Planning rounds 1 and 2 resolved the following decisions with work-only
probes. Preserve them as red-first input; do not reinterpret the current
one-primary output as evidence that the global suppression implementation is
sound:

1. **Poisoned call result:** `bad = missing_name; x = bad(); x();` — planning
   review confirmed the suppressed binding-call node must carry cause into its
   receiving HLet.
2. **Transparent wrappers:** direct alias, `move bad`, and a literal-selected
   ternary all carry the poisoned value without another primary today. This
   disproves binding-only and call-result-only provenance.
3. **Pending HVar contexts:** annotated captureless pending binding referenced
   by ordinary alias, `return`, compatible Fn/Callback argument, explicit
   `move`, borrow, and discarded `f;`.
4. **Contextual callback alias:** inspect post-check HIR/marks, not only rc=0,
   to prove callback wrapping remains lowering-visible and the original
   binding is a thin function pointer.
5. **Capturing first reference:** implicit borrow and explicit copy/move/share
   bare storage followed by alias/call. Count primary diagnostics and capture
   effects; verify the approved bare-storage rejection stays authoritative.
6. **Unconstrained params:** no context, concrete Fn context, and Callback
   context. Ensure no `LambdaFnSpec` with Unknown ABI types is published.
7. **Shadowing/recovery:** same source name/different ids and pending-to-
   concrete transition.
8. **True compatible bare-HVar argument:** passing pending `f` into a concrete
   Callback slot currently stamps the binding as the interface without
   constructing it, then fails MIR validation on a move from an uninitialized
   interface local. The accepted result requires thin-fn finalization followed
   by a real callback wrapper at the argument slot.
9. **Function-pointer borrow control:** borrowing an already-finalized stored
   fnptr compiles and runs, selecting finalize-and-accept for pending `&f`.
   Borrowing a named function currently raises `AttributeError` after its
   `HVar` becomes `HFnPtrConst`; repair that boundary in the nested
   `finding-fnptr-borrow-materialization` child.

Still pin a non-literal ternary join before implementing cause propagation.
The goal is not exhaustive syntax enumeration: it is to define a conservative
join rule under which a result is marked caused only when every reachable
Unknown-producing path is already explained. One caused arm must never silence
an uncaused arm.

## Phase 3 — install new in-tree regressions red-first

Prefer new files, for example:

- `lang/tests/type_checker/test_causal_unknown_provenance.py`;
- `lang/tests/driver/test_pending_lambda_value_finalization.py`.

Before production edits, pin:

### Causal negatives/controls

- unrelated error does not suppress independent Unknown copy;
- unrelated error does not suppress independent Unknown HCall;
- same-binding diagnosed Unknown is quiet through HCall and HInvoke;
- direct alias cause propagation;
- shadowing isolation and concrete clearing;
- poisoned call-result behavior selected in Phase 2.
- HVar, move, suppressed-call result, literal-selected ternary, and the
  non-literal ternary join matrix; do not silently regress today's one-primary
  presentation.

### Pending positives/negatives

- captureless alias full compile/run;
- contextual callback alias full compile/run plus structural wrapper mark;
- resolve-after-alias order independence;
- unconstrained alias one clean primary;
- capturing alias one approved primary for implicit and explicit capture
  classes;
- return/argument and any additional HVar contexts the Phase 2 evidence says
  the shared hook promises.
- borrowed pending value behavior against the concrete fnptr control.

### Callback materialization boundary

- direct typed-Callback HLet full compile/run and structural wrapper pin;
- bare pending HVar passed to a Callback parameter full compile/run;
- the original pending binding remains a thin function type while the slot
  contains a synthesized callback HCall;
- raw HLambda and uninitialized interface locals never reach MIR;
- explicit wraps do not double-wrap; capture/throw/arity diagnostics remain on
  their established authorities.

### Function-pointer borrow boundary

- pending `&f` finalizes and compiles/runs like the finalized-binding control;
- `&named_function` compiles/runs without turning an `HPlaceExpr.base` into an
  `HFnPtrConst` or otherwise violating the canonical-place invariant;
- a genuine non-addressable/illegal mutable-borrow negative retains a clean
  source diagnostic.

Every newly accepted lowering-visible shape needs a full compile/run companion.
Checker-only rc=0 is insufficient.

Existing-test changes are frozen separately in the authorization ledger below.

## Phase 4 — trace ownership and mutation order

Before code, write the exact mutation sequence for current HCall, HInvoke,
HVar, and final drain:

- pending owner lookup/barrier;
- lambda node stamping and typing;
- diagnostics emitted;
- binding type update;
- Unknown cause mark/clear;
- pending retirement;
- `_lambda_fn_specs`/fnptr publication;
- final HIR rewrite.

The barrier must happen first. A correct cause-table rollback does not make an
external HLambda mutation safe.

Audit all relevant binding-type writes. For each, record:

- can it leave Unknown?
- did this producer emit a local primary?
- can it inherit a cause without a new primary?
- can it run inside `CheckerStateTxn`?
- can it overwrite a caused Unknown with concrete type?

Trace all three `make_call_ctx(...)` sites (`type_checker.py` currently near
8612, 10097, and 10404) plus the separate HInvoke fallback.

## Phase 5 — install the selected causal authority

Add transaction-owned binding-id and expression-node provenance to
`FnCheckState`; include both `_TxnDict`s in `OWNED_TABLES`. Expose small
mark/propagate/clear/query helpers and store immutable cause metadata, not a
bare global flag or mutable Diagnostic object. No transaction wrapper may
escape through `TypedFn` or `TypeCheckResult`.

The initial explicit expression-flow set is:

- HVar read of an exactly caused binding;
- HMove of a caused subject;
- an HCall whose exact caused callee makes the call-target diagnostic a
  suppressed cascade;
- HTernary under a reachability-aware all-reachable-arms-caused join.

For a literal condition, only the selected arm participates. For a general
condition, mark the ternary only when every reachable Unknown result arm is
caused; if causes differ, retain sufficient immutable roots for explanation or
use a deterministic joined category. Any uncaused reachable Unknown fails
toward the downstream tripwire. HLet attaches expression cause only when its
actual inferred result remains Unknown and clears it on a concrete result.

Do not approximate causality by name, source order, “any child is Unknown,” or
function-global diagnostic state. Unproven expression shapes deliberately do
not propagate. Preseeded Unknown bindings remain unmarked.

## Phase 6 — centralize pending finalization

Extract one authority shared by HCall, HInvoke, ordinary HVar references, and
final drain, unless tracing proves a justified split. Required behavior:

1. Exact binding id enters `PendingLambdaOwner.begin_resolution()`.
2. Derive contextual function parameters/return/throw mode when available.
3. Type the lambda through the existing primary authority exactly once.
4. Captureless success installs a concrete thin-function type and clears cause.
5. Capturing/unconstrained failure emits one primary, installs diagnosed
   Unknown cause, and publishes no invalid ABI spec.
6. Retire only after the outcome is total.
7. Preserve fnptr spec/const publication and `_apply_fnptr_consts` lowering.

Do not store a Callback interface type on the original captureless binding;
normal callback wrapping must remain an explicit HIR/lowering-visible action.

Do not add a second body-inference path in the call resolver.

Coordinate this helper with the typed-let Callback child. That child restores
the established implicit-wrap contract at all concrete Callback slots reached
by this slice; the pending-alias/argument paths must feed the same
lowering-visible wrapper once the HVar finalizes to a thin function type.

## Phase 6A — restore Callback slot materialization

Do not fix the typed-let case by merely changing its recorded TypeId. Route a
direct HLambda through the existing WRAPPED/REJECTED/SKIP callback authority
before equality can bypass conversion. The wrapper must be constructed before
the inner lambda is typed so capture-capable Callback contexts use the
callback intrinsic's `allow_capture_invoke` path rather than the captureless
fnptr-coercion branch.

Apply the same contract to call arguments after pending HVar finalization:
retain the thin function type on the binding, synthesize and splice the
callback HCall in the argument slot, then record the slot's Callback type and
CallInfo. Audit every direct/static/free/method argument route reached by the
tests; remove any interface-label-only shortcut that can accept a lambda/fnptr
without a lowering-visible construction.

## Phase 6B — repair function-reference borrow materialization

Trace `&named_function` across stage1 borrow materialization, place
canonicalization, `fnptr_consts_by_node_id`, `_apply_fnptr_consts`, borrow
checking, and MIR lowering. The leading static hypothesis is that syntactic
stage1 treats the name as an HVar place, while semantic function-reference
replacement later puts `HFnPtrConst` into `HPlaceExpr.base`, violating the
documented HVar-only invariant. Confirm with a structural red test.

Repair the earliest authority that knows this is an rvalue function constant,
so the borrow gets real temporary storage (or another already-supported
lowering-visible representation). Do not teach canonical places to accept
arbitrary rvalue bases and do not catch the AttributeError. Preserve normal
local-fnptr borrowing and targeted invalid-borrow diagnostics.

## Phase 7 — replace consumers

1. `_require_copy_value`: suppress Unknown only when the exact HVar binding (or
   proven expression provenance) has a cause.
2. HCall local function value: query exact `binding_id`; remove the global
   diagnostics scan.
3. HInvoke: apply the same tested causal predicate.
4. Correct the two source comments that currently overclaim causality.
5. Keep unmarked/non-binding Unknown values as tripwires.

## Phase 8 — invariant teeth

Pin cause state through:

- mutation + rollback;
- inner commit + outer rollback;
- commit persistence;
- inclusion in `state_fingerprint()`;
- exact identity under shadowing;
- concrete clear;
- plain-dict/non-wrapper public result objects.

Extend the pending rollback test only if a new file cannot prove the new state
without editing an existing test; existing-test edits need approval.

## Existing-test edit authorization ledger

Slawomir approved this exact list on 2026-08-05. New test files are listed
separately above and do not require approval.

Currently proposed existing-test edits are comment/docstring corrections only;
no existing assertion, fixture source, expected diagnostic, test name, or test
helper is proposed to change:

1. `lang/tests/driver/test_implicit_callback_wrap.py`
   - replace the Site-1 “NOT WRAPPED” / “silent interface coercion” narrative
     and the Site-1 no-double-wrap docstring with the restored canonical
     wrapper contract;
   - rewrite the Site-2, Site-5, and Site-6 docstrings that describe raw
     iface-coercion failure as current behavior into historical regression
     descriptions;
   - update the arity-mismatch/out-of-scope note only if the red/green matrix
     proves that the shared argument authority closes it.
2. `lang/tests/driver/test_stored_capturing_lambda_diagnostic.py`
   - replace “a prior error already explains” with exact causal binding/node
     provenance; assertions remain unchanged.

The historical lifecycle prose in
`lang/tests/driver/test_uninvoked_stored_lambda.py` remains accurate for its
0.34.2 regression and final-drain coverage. The call-probe-specific prose and
assertions in `lang/tests/checker/test_pending_lambda_probe_barrier.py` also
remain accurate. Neither file is approved or planned for editing.

K confirmed this list is complete in planning round 3. Any assertion, fixture,
expected-output change, test/helper rename, deletion, conditional contingency,
or additional existing test file remains unapproved until explicitly added and
approved by Slawomir.

## Phase 9 — focused verification

Minimum, adjusted to actual new paths:

```sh
./.venv/bin/python3 -m pytest -q lang/tests/type_checker/test_causal_unknown_provenance.py
./.venv/bin/python3 -m pytest -q lang/tests/driver/test_pending_lambda_value_finalization.py
./.venv/bin/python3 -m pytest -q lang/tests/driver/test_callback_slot_materialization.py
./.venv/bin/python3 -m pytest -q lang/tests/driver/test_fnptr_borrow_materialization.py
./.venv/bin/python3 -m pytest -q lang/tests/checker/test_pending_lambda_probe_barrier.py
./.venv/bin/python3 -m pytest -q lang/tests/checker/test_defer_probe_state_transaction.py
./.venv/bin/python3 -m pytest -q lang/tests/driver/test_stored_capturing_lambda_diagnostic.py
./.venv/bin/python3 -m pytest -q lang/tests/driver/test_uninvoked_stored_lambda.py
./.venv/bin/python3 -m pytest -q lang/tests/type_checker/test_type_checker_copy_unknown.py
```

Then run checker/type-checker/driver suites proportionate to the touched code.
Do not start another full suite or corpus run until review converges and
Slawomir schedules it.

## Phase 10 — version/history/handoff

- No spec change without Slawomir's explicit approval; none is currently
  needed.
- ABI stays 22 unless actual compiler/runtime boundary evidence contradicts
  the current analysis.
- If 0.35.0 remains unreleased/uncertified, retain it and fold both fixes into
  its pending history entry. Otherwise take the mandatory minor bump.
- K records evidence and disagreements in implementer-owned `PROGRESS.md`.
- Publish the implementation handoff through Baton only when focused gates are
  complete. The reviewer may recurse into a child finding if implementation
  exposes a genuinely separate bug.
