# Baton message

Timestamp: 2026-08-05T10-52-13Z
From role: implementer
Actor: k
To role: reviewer
Kind: implementation
Thread: eb8634633475

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-05T10-47-12Z-b147206c7610-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-05T10-50-52Z`
Proposed commit message for the completed language slice.  Scope: the 8
modified + 5 new files under lang/ and doc/history.md ONLY (excludes
AGENTS*/tools/baton and all work/ artifacts).  Not committed — git is
Slawomir's.  Noted: perf + full MEMCHECK lanes passed; ASAN intentionally
not run and the message does not claim it.

---8<---
lang: exact causal Unknown provenance; total pending-lambda finalization; canonical Callback wrap at assoc calls (0.35.0)

Replace the checker's global any(error) cascade suppressor with exact
causal provenance owned by FnCheckState (unknown_cause_by_binding /
unknown_cause_by_node, probe-transaction covered).  Producers record the
primary that explains each poisoned binding/expression; consumers — the
copy check, HCall/HInvoke callee resolution, and method dispatch on a
caused receiver — suppress ONLY on a recorded cause and fail toward the
tripwire otherwise, so independent un-diagnosed Unknowns diagnose again.
Propagation is explicit: caused-binding reads, `move` through the
canonical projection-less place, and reachability-aware compound joins
for ternary, match, and try (ALL reachable Unknown contributors must be
caused; the HLet diagnostic watermark no longer applies to compound
initializers, so a one-arm primary cannot mask a sibling arm's
independent Unknown).  Suppressed call/method sites still observe their
arguments, keeping independent argument primaries.

Pending-lambda finalization is now ONE total classifier shared by all
four consumers (HVar value use, direct HCall/HInvoke callees, drain):
capturing lambdas get the single approved v1 primary and are never
typed; unconstrained lambdas without context get one clean cannot-infer
primary; inferable lambdas type exactly once and record their static
fnptr const per binding; a residual Unknown component poisons the
binding and RETRACTS the published LambdaFnSpec/fnptr const so no
Unknown-ABI contract stays lowering-consumable.  Value uses of pending
bindings finalize instead of cascading E-COPY-UNKNOWN — aliasing a valid
stored captureless lambda (`val g = f`) is now legal and runs.

Callback slots construct the canonical core.callbackN(...) wrapper
BEFORE typing the lambda (typed-let and return position), fixing the
re-check ICE where a raw HLambda reached HLet lowering under an
interface label.  Static fnptr provenance is seeded at
`val f = named_fn` bindings, propagates across immutable alias hops, and
is consumed by the wrapper's centralized static splice.

Fix (pre-existing LANGUAGE_BUG): associated/static calls
(`Type::fn(...)`) silently bypassed the wrapper via
coerce_args_for_params' INTERFACE retyping — bare lambdas emitted
invalid LLVM IR, named-fn args crashed the vtable lookup, and arity
mismatches were checker-silent.  The assoc-call success path now routes
Callback params through the same wrapper authority; arity mismatches and
runtime-only (mutable/computed) function values get real checker
diagnostics instead of invalid IR or MIR-invariant failures.

Fix (ICE): borrowing a named function (`val r = &seven`) crashed
_lower_addr_of_place on the checker's HFnPtrConst rewrite inside the
canonical place; the constant now materializes through
_materialize_owned_temp_for_borrow and the borrow is callable through
`*r`.  `&mut` keeps its checker rejection and lowering fails closed.

Tests: five new regression files pin the causal contract (tripwires,
single-primary chains, compound joins both ways, suppression-with-
argument-observation), total finalization (alias/drain/direct-call
matrices through both call families), callback-slot materialization
(pre-wrap, alias chains with the static __lambda_fn_ IR witness),
assoc-call wrap (red-first: lambda, named fn, stored bindings, arity
negative, mutable rejection, free-fn control), and fnptr-borrow
materialization (structural place-transition trace + consume-the-borrow
run).  test_implicit_callback_wrap.py's Site-1 tests pin the corrected
contract (approved edit); test_stored_capturing_lambda_diagnostic.py's
narrative states the causal mechanism; test_std_json_regressions.py pins
four independent legacy-helper primaries with an exact count and no
receiver-Unknown cascade (approved fixture update);
test_stmt_position_iife.py renames the value-position throwing-IIFE test
accurately and adds the true statement-position compile/run pin.

History: folded into the pending 0.35.0 entry.  Internal checker and
lowering only — no ABI change, DRIFT_RT_ABI_VERSION stays 22.
---8<---

If you prefer the IIFE test-coverage finding and the std.json fixture
update as separate commits (each is independently coherent), say so and I
will split the message; the single-commit form above matches how the
0.35.0 entry folds them.
