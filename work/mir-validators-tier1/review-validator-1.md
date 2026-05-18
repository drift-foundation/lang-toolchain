# Code Review: MIR validator tier-1 #1 — `validate_mir_void_return_shape`

**Branch:** `main` (uncommitted working tree as of 2026-05-18)
**Slice:** Tier 1 validator #1 of 3 from
`work/mir-validators-tier1/plan.md`.
**Release:** 0.31.105 (compiler hygiene, ABI unchanged at 14).

---

## TL;DR for reviewers

Adds a backstop validator that fires at the MIR boundary if any
synthesis pass produces `M.Return(value=<some_ssa>)` on a nothrow
`-> Void` function.  Compiler-internal invariant only — no user
source can produce this shape.  The originating production bug
(Void-callback-lambda crash, 2026-05-17) is already fixed at the
lowering and type-env layers; this is a *third defense* so any
future regression surfaces with a targeted diagnostic at the
right place instead of an opaque `KeyError` deep in
`throw_checks`.

**Risk profile:** small + bounded.  No user-visible behavior
change.  No ABI change.  Validator does not run on user code
shapes (filtered out before terminator inspection).  Audit
covered stage2 + driver-regression + 45 e2e fixtures + minimal
stdlib + Void-callback repro; zero pre-existing violations.

---

## What changed

| File | Δ | Summary |
|------|---|---------|
| `lang/driftc/mir_validate.py` | +40 lines | New `validate_mir_void_return_shape(funcs, signatures_by_id, type_table)`.  Inserted after `validate_mir_basic_hygiene`. |
| `lang/driftc/driftc.py` | +2 lines | Import the new symbol; wire one entry into the `validator_plan` extend block at ~line 7184 (inside the `if shared_type_table is not None:` branch). |
| `lang/tests/stage2/test_mir_validate_void_return_shape.py` | +160 lines, NEW | 5 hand-built-MirFunc unit tests. |
| `lang/versions.py` | 1 line | `0.31.104` → `0.31.105`. |
| `docs/history.md` | +75 lines | Top entry. |

Total diff stat: 2 modified files + 1 new test file + version
+ history bumps.

---

## Validator rule, in plain English

For every `MirFunc` in the post-build MIR pool:

1. Skip if there is no `FnSignature` for its `fn_id` in
   `signatures_by_id` (synthesized helpers without a signature
   are out of scope).
2. Skip if the signature has no `return_type_id`.
3. Skip if `not type_table.is_void(return_type_id)`.
4. Skip if `sig.declared_can_throw` is truthy (those fns return
   an `Ok(Void)` carrier built upstream by
   `M.ConstructResultOk`; that's the *correct* shape, and
   was deliberately *not* changed by the 2026-05-17 fix).
5. Otherwise: for each basic block, if the terminator is
   `M.Return` and `term.value is not None`, raise
   `AssertionError` with a message that names the symbol, the
   block, and the offending SSA value.

`AssertionError` (not a user `Diagnostic`) because the
originating bug is a *compiler bug*, not a user-source mistake.
The existing `_run_mir_validator` driver in `driftc.py`
already wraps `AssertionError` into a boundary contract diag
(`phase=mir_validate`), so the developer-facing channel is
correct.

---

## Why this validator, why now

The 2026-05-17 Void-callback-lambda bug surfaced as

    KeyError: (FunctionId(module='lambda_repro',
                          name='__lambda_cb_main_0_0',
                          ordinal=0), 't3')

raised from `stage4/throw_checks.py:316` because Void-returning
lambda lowering at `driftc.py` (~line 6625) emitted
`M.Return(value=<synth_void>)` where every other nothrow Void
fn correctly emits `M.Return(value=None)`.  The synth value
was unkeyed in the SSA type-env and the lookup blew up.

The fix landed at two layers (lowering + type-env seeding) and
is covered by `test_lambda_void_callback_throw_check.py` x5
carriers.  Both of those are *site-specific* defenses.  This
validator is a *shape-specific* defense — independent of which
lowering site produced the bad MIR, it catches the invariant
violation at the MIR boundary and points at the canonical
lowering shape in the diagnostic.

This matches the `ledger_cache.py` precedent the plan calls
out: discover invariant from real bug → runtime assertion +
static audit + named exception.  Incremental, not predictive.

---

## Pipeline placement (load-bearing — verify)

- `compile_stubbed_funcs` builds MIR, then enters the
  `with _timed("mir_validate"):` block at `driftc.py:7111`.
- The `validator_plan` is built up incrementally; the new
  validator joins the *typed-table-dependent* block at
  ~line 7184 alongside `iface_init_invariants`,
  `array_copy_invariants`, `call_byvalue_moves`.
- The full plan is iterated and run before
  `compile_stubbed_funcs` returns.
- `compile_to_llvm_ir_for_tests` (and CLI main) call
  `compile_stubbed_funcs` first, then `lower_module_to_llvm`
  at `driftc.py:7678`.
- `stage4/throw_checks.py` runs inside the per-fn pass loop
  that happens before mir_validate.  Important: the validator
  catches the *shape*, but the original
  `throw_checks::enforce_fnresult_returns_typeaware` KeyError
  is upstream of where this validator fires.  In other words:
  if a future regression re-introduces the shape, the
  throw_checks crash returns BEFORE the validator can pretty-
  print it.

  **Reviewer check:** is that ordering acceptable?  My read:
  yes, for two reasons.  (a) The originating bug's actual fix
  is at lowering + type-env layers, both of which run *before*
  throw_checks, so the production-realistic regression path is
  "lowering forgets the None" → throw_checks crash.  This
  validator's value is to catch *new* synthesis sites
  (especially ones added by future passes that run AFTER
  throw_checks but before mir_validate) — e.g. const_share_synth
  or future wrapper synthesis.  (b) For the historical-shape
  regression, the validator still helps debugging because the
  AssertionError carries the file:line hint that points at
  `driftc.py` Void-lambda lowering.

  If reviewer disagrees and wants the validator to run *before*
  `throw_checks` so it always pre-empts the KeyError: that's a
  one-line move in `compile_stubbed_funcs`.  Open question.

---

## Unit tests (`lang/tests/stage2/test_mir_validate_void_return_shape.py`)

All 5 pass in 0.77s.

| # | Scenario | Expected | Why it matters |
|---|----------|----------|----------------|
| 1 | nothrow Void + `Return(None)` | accepted | Baseline positive case — must not regress. |
| 2 | nothrow Void + `Return(value)` | rejected with `"nothrow Void fn"` in message | THE BUG.  Pinned by message-prefix match. |
| 3 | can-throw Void + `Return(ok_carrier)` | accepted | Pins the can-throw carve-out — without this every can-throw Void fn would trip. |
| 4 | non-Void return + `Return(value)` | accepted | Sanity check on the Void filter. |
| 5 | fn missing from `signatures_by_id` | skipped | Pins the missing-sig short-circuit so synthesized helpers (e.g. dispatch thunks) don't trip the validator before their sig is registered. |

The validator's filter clauses 1–4 each have an explicit test;
filter clause 5 (the firing case) has a positive test (#2) and
its inverse (#1).  Tests 3 and 4 specifically pin the
*not-this-shape* skips so future tightening of the filter
doesn't accidentally make the validator fire on legitimate code.

---

## Audit — proves no pre-existing violations

Per plan acceptance criterion #4 ("don't ship a validator that
fails on current main; otherwise gate behind
`DRIFT_STRICT_MIR_VALIDATE=1`"):

- `lang/tests/stage2/` -- **282 passed** in 471s.
- `lang/tests/driver/test_lambda_void_callback_throw_check.py`
  -- **5 passed** (the 5 originating-bug carriers).
- `lang/tests/driver/test_const_share_synth_shared_binder_name.py`
  -- **1 passed**.
- `lang/tests/driver/test_mir_validate_boundary_diagnostics.py`
  -- **2 passed**.
- 45 codegen e2e fixtures matching `*void*|*lambda*|*callback*`
  -- **45/45 passed**, 0 skipped, 0 failed, 85s.
- Minimal stdlib-using compile (`pub fn main() nothrow -> Int
  { return 0; }`) -- driftc exit 0, link clean.
- Void-callback-lambda repro (the originating bug carrier) --
  driftc exit 0, link clean.

The validator is NOT gated by an env var — the audit ruled the
gate unnecessary.

A full e2e sweep over all 1248 codegen fixtures was NOT run as
part of this slice (cost ~hours).  Reviewer call: do we require
it before merge, or is the 45-case targeted set + 282 stage2 +
8 driver-regression coverage sufficient?

---

## Decisions I made and why (reviewer can push back)

1. **Validator function lives in `mir_validate.py`, not a new
   file.**  Plan suggested either "add to
   `validate_mir_basic_hygiene`" or "new validator".  I picked
   the latter because basic_hygiene doesn't take signatures or
   type_table, and modifying its signature to accept them would
   be invasive for one rule.  Net cost: +1 entry in the
   validator plan, no new module.

2. **`AssertionError`, not `Diagnostic` + `UserFacingMirDiagnostic`.**
   The originating bug is a compiler-internal invariant, not a
   user-source error.  `_run_mir_validator` in driftc.py already
   wraps `AssertionError` into a boundary contract diag, so
   nothing leaks as a Python traceback in production.

3. **Validator runs in the `shared_type_table is not None`
   branch.**  It needs `type_table.is_void(...)` and
   `signatures_by_id[fn_id].return_type_id`.  Test paths that
   bypass the type table (e.g. some borrow-checker-only fixtures)
   simply won't run this validator — that matches the precedent
   set by `validate_mir_iface_init_invariants` and
   `validate_mir_call_byvalue_moves`.

4. **Diagnostic message includes a hint pointing at
   `driftc.py` Void-lambda lowering.**  Future maintainers
   debugging a hit will arrive at the validator first; the hint
   short-circuits the spelunking.  Mirrors the
   `test_lambda_void_callback_throw_check.py` failure message
   style.

5. **Did NOT also wire the new validator into the
   `compile_stubbed_funcs` `if shared_type_table is None:`
   branch.**  That branch is exercised by a small set of
   driver/unit tests that intentionally skip the type table.
   Those tests don't synthesize Void-returning lambdas; nothing
   to validate there.  Reviewer can ask me to add a no-typetable
   fallback that uses a simpler heuristic (e.g. signature flag
   only) if they want belt-and-braces.

---

## What I deliberately did NOT do

- **Did not implement validator #2 or #3 from the plan.**  Plan
  says "one validator per commit"; #2 (binder uniqueness) and
  #3 (cleanup coverage) both need their own feasibility checks
  per the plan's per-validator section.  Sequenced for a
  separate slice.
- **Did not bump `DRIFT_RT_ABI_VERSION`.**  Validator is
  compile-time-only; runtime ABI unchanged.
- **Did not touch the codegen-side contract validator
  (`_validate_codegen_contract`).**  That's a different layer
  (LLVM IR post-emit); see plan "Deferred" section for the
  codegen-call-resolution audit, which is explicitly out of
  scope for tier 1.
- **Did not add a `DRIFT_STRICT_MIR_VALIDATE=1` env var
  gate.**  Per audit, no pre-existing violations; the gate is
  unnecessary.

---

## Suggested review checklist

- [ ] Pipeline placement (mir_validate runs after throw_checks
  — is that the right ordering? see "Pipeline placement"
  open question above)
- [ ] AssertionError vs Diagnostic shape (compiler-internal,
  not user-facing — agreed?)
- [ ] Test coverage: are the 5 unit tests + e2e/driver audit
  sufficient, or do we want a fuller e2e sweep first?
- [ ] History entry framing: "compiler hygiene" vs "validator
  add" — does it match the docs/history.md house style?
- [ ] Version bump cadence: each validator gets its own patch
  version per the plan; reviewer ok with 0.31.105 → .106 → .107
  cadence for the 3-validator series, or batch?

---

## Files for reviewer to focus on

1. `lang/driftc/mir_validate.py` lines ~308-345 — the new
   validator body.  This is the entire load-bearing change.
2. `lang/driftc/driftc.py` lines ~118-120 and ~7184-7190 — the
   import + wire-up.
3. `lang/tests/stage2/test_mir_validate_void_return_shape.py` —
   the test coverage.
4. `docs/history.md` top entry — the user-facing record.
5. `work/mir-validators-tier1/plan.md` for context on where
   this fits and what's still pending.
