# E-population triage — the 7 residual c3_moveout_not_owned events (report-only)

Status: TRIAGE REPORT — no implementation. Stops for scheduling. All MIR
dumped at string_arc entry on the current branch tree; probe binaries built
and run from scratchpad only; nothing in-tree changed.

## Headline

The 7 events are THREE distinct shapes — and two of them are **confirmed
LANGUAGE_BUGs reachable from valid user source with silent value
corruption** (not drop/ownership imbalance: observable WRONG VALUES). The
third is a compiler-authored dead drop, runtime-safe.

| # | events | fixture(s) | shape | classification |
|---|---|---|---|---|
| 1 | 3 | match_stmt_nested_match_last_stmt | re-match of a consumed scrutinee | **LANGUAGE_BUG (confirmed by probe)** |
| 2 | 3 | std_io_{file_builder_chunked_large, stdin_read_line_eof_helper, pipe_reverse_stdout} | non-Copy error binder passed by value twice | **LANGUAGE_BUG (checker gap, confirmed mechanism)** |
| 3 | 1 | catch_binder_visible_in_arm | authored cleanup drop of an explicitly-moved catch binder | compiler dead-drop, runtime-safe |

## Shape 1 — re-match of a consumed scrutinee (3 events)

`m::main`, `match_arm_01/11/21` `[0]`, subject `r`, raw MOVED_OUT.

Source (valid, accepted by the checker):
```drift
val r: core.Result<Int, Int> = core.Result::Ok(1);
match r {
	Ok(v) => {
		match r { ... }    // r was CONSUMED by the outer match
	},
	...
}
```

MIR: outer `match_arm_0[0]` does `MoveOut(r)` into the scrutinee temp; the
MoveOut expansion ZERO-BACKS `r`'s storage; `match_arm_0[7]` then
`LoadLocal(r)` for the inner dispatch — reading ZEROED bytes — and each
inner arm re-does `MoveOut(r)` (the 3 audited events).

**Probe-confirmed silent corruption** (scratchpad, not committed):
- `Ok(5)` + inner `Ok(w)` binder: prints `outer v=5` then **`inner w=0`**.
- Non-Copy payload (`Result<String, Int>`, `Ok("payload")`): ALSO compiles;
  inner binder reads the zeroed String → **empty string** (null-safe, so no
  crash — the value is silently gone).

The fixture never observes it (inner binders discarded) — that is the only
reason it passes. Root cause is a checker/lowering mismatch: the checker
permits the second `match r` (both for Copy-able and non-Copy payloads),
while the lowering unconditionally consumes the scrutinee
(`MoveOut` + zero-back). Either the checker must reject the re-match of a
consumed non-Copy scrutinee AND the lowering must COPY for Copy-classified
scrutinees, or match-by-value semantics need an explicit ruling. Adjacent
resolved family: 0.33.39 (match arm `move <local>` zeroed variant) — same
zero-backed-storage observable, different trigger.

## Shape 2 — non-Copy error binder consumed by value twice (3 events)

`m::main`, `tern_else*/if_join` `[0]`, subjects `__match_binder_*_e`, raw
MOVED_OUT.

Source shape (all three std_io fixtures):
```drift
Err(e) => {
	if io.is_eof_error(e) { return 0; }         // moves e (IoError is `pub error` — non-Copy; by-value param = move)
	if io.is_would_block_error(e) { return 0; } // USE AFTER MOVE — checker accepts
	return 4;
}
```

MIR: first call consumes the binder via `MoveOut` (zero-back); the second
call's `MoveOut(e)` at `if_join[0]` loads ZEROED error storage. Control
probes: a plain bitcopy struct in the identical shape lowers the calls as
`LoadLocal` (no move, correct) — the move convention is specific to the
non-Copy error type, so the MOVE ITSELF is correct semantics; **the bug is
the checker failing to reject the second use**. Runtime effect on the real
fixtures: the would-block path calls `is_would_block_error` on a ZEROED
error (kind 0) → false → `main` returns 4 instead of 0. The fixtures pass
only because the test environment takes the EOF path first.

Two deliverables when scheduled:
- **LANGUAGE_BUG (checker)**: use-after-move of a moved match binder must
  be rejected (regression-first: failing pin with the exact fixture shape).
- **Stdlib API smell (separate, small)**: `io.is_eof_error` /
  `is_would_block_error` take `IoError` BY VALUE — an error-classification
  predicate should take `&IoError`; the current signatures force the
  broken pattern on users. Fixing the signatures also lets the three
  fixtures express their evident intent.

## Shape 3 — authored cleanup drop of an explicitly-moved catch binder (1 event)

`main::main`, `try_catch_0[9]`, subject `e`, raw MOVED_OUT.

```
[0] MoveOut(__try_err) → [1] StoreLocal(e)     catch binder materialization
[3] MoveOut(e)         → [4] StoreLocal(moved) user's `val moved = move e`
[7] MoveOut(moved)     → [8] DropValue         authored cleanup of `moved` ✓
[9] MoveOut(e)         → [10] DropValue        authored cleanup of `e` — ALREADY MOVED
```

The catch-arm binder cleanup emits an unconditional end-of-arm drop for
`e` even though the ledger at that point says MOVED_OUT (the user moved
it). Runtime-safe: `[3]`'s expansion zero-backed `e`, and dropping a zeroed
error envelope is a null-safe no-op. Not user-observable; it is a dead
drop + C3 noise. Options:
- (a) Narrow emission fix: the catch-binder cleanup path consults the
  ledger (skip at MUST_NOT_DROP) like cleanup_authoring's hook decisions
  do — an emission change needing its own exact-delta corpus row; or
- (b) Reporter structural recognition (no emission change): extend the
  zero-safe rule to raw MOVED_OUT **when the MoveOut feeds an
  immediately-following DropValue** (the authored-cleanup pairing already
  snapshotted as `moveout_feeds_drop`). This is safe AND would not have
  masked shapes 1–2: their second consumers feed a scrutinee store and
  call arguments, never a paired DropValue.

## Recommendation

1. **Shapes 1 and 2 are LANGUAGE_BUG candidates and I am calling them out
   explicitly per the triage instruction**: both are reachable from valid
   user source and corrupt observable VALUES (zeroed reads), not merely
   drops. File two issue bundles with the scratchpad probes promoted to
   repros + failing regression-first pins, and fix root causes as a
   dedicated hotfix-style slice BEFORE Array release-elision — elision
   stacks more machinery on exactly these ownership invariants.
   Fallout to plan for: a correct checker fix likely REJECTS the four
   carrier fixtures as invalid source (they contain latent use-after-move),
   changing the corpus universe partition — the fixtures need intent-
   preserving rewrites in the same slice, and the corpus reference gets
   re-recorded.
2. **Shape 3**: recommend option (b) — the drop-paired MOVED_OUT zero-safe
   extension (reporter-only, one new pin, byte-identical emission), keeping
   option (a) as a later cleanup_authoring precision item. After shapes 1–2
   are fixed and shape 3 reclassified, `c3_moveout_not_owned` reaches a
   true zero and can join the hard-gate set.
3. No permanent allowlist is warranted for any of the 7: two are bugs, one
   is a compiler wart with a principled structural rule.

## STOP

Awaiting scheduling. Probes live in the session scratchpad only; promoting
them to `issues/` bundles + pins is part of the recommended follow-up, not
this report.

---

## FIX SLICE (2026-07-13) — root causes, fixes in place, STOPPED on stdlib fallout scope

### Trigger-registry ruling (SUPERSEDED — see "FIX SLICE COMPLETION" below)

Scanned `doc/refactor_triggers.md` §"Carry implicit-move classification
structurally from borrow-check to MIR lowering". Interim ruling at this
point in the slice: considered, NOT fired. REVIEW CORRECTION (blocking
clarification): the phrasing "neither accepts implicit moves" was
inaccurate — the shape-1 semantics deliberately KEEP bare `match r` as the
language's ONE implicit-consume position for non-Copy place scrutinees.
The authoritative final ruling (considered — NOT fired — with that ONE
recorded exception, the "pattern-match consume" wording addressed
directly, and the capture-slot per-site containment) lives in the
completion section below and, durably, in `doc/refactor_triggers.md`'s
dated ruling note. The shape-2 fix DOES restore the explicit-move
rejection contract at call-arg/value positions without accepting any new
implicit move; the shape-1 exception is scrutinee-position-only.

### Root causes (both surgical, both walker/coverage-class)

**Shape 2 (deeper than the triage guessed):** the explicit-move call-arg
gate `_check_explicit_move_required_at_call_arg` EXISTS and works — but
`_walk_expr_for_borrowed_boundaries`'s HMatchExpr case walked only the
scrutinee and `arm.result`, never **`arm.block`**. Every call inside a
statement-form match arm escaped the gate (and the borrowed-arg boundary
checks); `_lower_call_arg`'s documented "internal backstop" MoveOut then
silently consumed bare non-Copy binders. Contributing discoveries, recorded
for follow-ups, all probe-verified:
- plain locals in the same shape ARE rejected (the gate works outside arms);
- a same-module `pub error {String, Int}` gets a SYNTHESIZED ConstShare
  impl and the bare pass legally const_share-wraps — but synthesis
  qualification proves fields against the DECLARING module's import-visible
  trait world, so a module that does not import std.core silently cannot
  derive ConstShare (probe: identical type in an import-less module →
  ConstShare proof REFUTED in the consumer). Follow-up candidate, separate
  slice: qualify against a world that always includes the trait-defining
  prelude.
- io.IoError has NO ConstShare impl (stdlib is excluded from synthesis by
  design and has no hand-written one), so its binders ride the hole.

**Shape 1:** the borrow checker's HMatchExpr handler visits the scrutinee
with `consume=False` — no consumption tracking at all. The lowering's
`_ensure_arm_scrut_ptr` moves + zero-backs non-`_should_copy_value`
scrutinees; nothing upstream records it.

### Decided semantics (shape 1, per the delegated decision)

By-value match of a **non-Copy place scrutinee consumes it**; every later
use (including a re-match) rejects with the standard E_USE_AFTER_MOVE.
Bare `match r` stays legal (ecosystem-wide pattern; requiring `move r`
would be a much larger language change). Matching a borrow is the
ownership-preserving escape. Copy-classified scrutinees keep the lowering's
existing copy branch (non-consuming). PROJECTED-place scrutinees
(`match self.field`) are deliberately excluded (consuming one would be a
partial move, which Drift rejects as a language rule) — flagged for a
follow-up audit. Spec §1.3/§4 wording update flagged as follow-up.

### Implemented so far (tree state: RED mid-slice, nothing committed)

- `type_checker.py`: HMatchExpr boundary walk now descends into
  `arm.block` (shape-2 gate restoration).
- `borrow_checker_pass.py`: implicit consumption of bare-place non-Copy
  non-ref scrutinees at HMatchExpr (shape 1); borrow suite 90/90.
- `stdlib/std/io/io.drift`: `io_error_code` / `is_would_block_error` /
  `is_eof_error` / `is_line_too_long_error` now take `&IoError`.
- `match_stmt_nested_match_last_stmt` fixture rewritten (inner match on a
  fresh value; intent — nested match as last statement — preserved).
- 5 regression-first pins in
  `lang/tests/driver/test_match_consume_and_arm_call_gate.py`, 4 verified
  FAILING pre-fix.

### STOP: the restored gate reveals 49 stdlib sites in 9 modules

Enumerated on a trivial compile (every driftc run currently fails):
JsonErrorData ×20 (std.json), RegexError ×11 + RegexNode ×6 (std.regex),
ConcurrencyError ×6 (std.concurrent), LoggerRuntimeState ×2 (std.log),
Utf8Error, SourceError, Token<K>, CliError ×1 each. Every site is today a
SILENT implicit move inside a match arm — exactly the class where the
std_io double-use bugs hid. Each needs eyeball verification (single-use →
spell `move`; classification-style → borrow, like the io predicates;
double-uses = MORE latent zero-read bugs to fix), not a blind sed.

Decision needed before proceeding:
(a) **Sweep all 49 in this slice** (recommended: they are precisely the
    latent-bug surface this fix exists to expose; mostly mechanical with
    json/regex dominating), then fixtures beyond stdlib, batteries, corpus
    re-record (fixture sources changed → universe re-record expected).
(b) Split: land shape 1 + io predicates now, defer the walker fix + sweep —
    NOT recommended (leaves the corruption class open and the two halves
    entangle at the fixtures).
(c) Grandfather via ConstShare impls for the error types — NOT recommended:
    it silently converts intended moves into shares (behavior + refcount
    changes) instead of making intent explicit.

---

## FIX SLICE COMPLETION (2026-07-13, option (a) executed)

### Trigger/semantics resolution (the blocking clarification)

The deliberate match exception is KEPT and now RECORDED — in
`doc/refactor_triggers.md` under the implicit-move entry (dated ruling) and
pinned in code. The corrected ruling: **considered — NOT fired — with one
recorded language exception and one per-site containment.** The trigger's
"pattern-match consume" bullet targets consuming positions ADDED without
source-level HMove; the scrutinee consume pre-dates the trigger and the
0.33.6 contract — it was an UNTRACKED position, not a new one, and is now
tracked (E_USE_AFTER_MOVE) + pinned. Sharpened fire condition recorded: a
second implicit-consume position, or another capture-slot mis-route,
fires it.

### NEW THIRD BUG found and fixed by the ruling probe (was live on certified 0.33.82)

Probing the trigger's exact worry — `match` on a MOVE-CAPTURED non-Copy
scrutinee inside a callback lambda — read a ZEROED payload (tag dispatch
correct: the HVar read is capture-aware; the arm consume in
`_ensure_arm_scrut_ptr` targeted the never-materialized LOCAL). Reproduced
on the certified toolchain; fixed by routing the arm consume through
`_move_from_callback_capture_slot` (env-slot load + zero-back + live-flag
clear; the source-local tombstone write-back is skipped on that path);
valgrind-clean probe; pinned
(`test_move_captured_scrutinee_match_reads_true_payload`).

### The 49-site stdlib sweep — classification table

Reviewed every site (context extracted per site). Result: **49/49 = single
terminal ownership transfer → spelled `move`**; 0 classification-predicate
shapes (the io.IoError family was already converted to `&IoError` — the
only predicate-style takers found); 0 double-uses (the double-uses were
FIXTURE-side — the std_io trio — and are fixed by the `&IoError`
signatures, under which their bare `e` args auto-borrow).

| module | sites | subjects | disposition |
|---|---|---|---|
| std.json | 20 | `e` (JsonErrorData) | move — all `return Err(e)` re-wraps |
| std.regex | 17 | `e` (RegexError) ×11, RegexNode ×6 (`node/first/only/atom×2/inner`) | move — Err re-wraps + Ok(node) terminal returns; `atom` sites are path-exclusive |
| std.concurrent | 6 | `e`/`err` (ConcurrencyError) | move — Err re-wraps |
| std.log | 2 | `next` (LoggerRuntimeState) | move — terminal into `_alloc_runtime_state` |
| std.parse | 1 | `value` (Token<K>) | move — terminal `Ok(value)` |
| std.cli / std.source / std.text | 3 | `e`/`err` | move — Err re-wraps |

### Verification

- Pins 7/7 (`test_match_consume_and_arm_call_gate.py`): arm-body call-gate
  restoration; consumed-scrutinee re-match rejection ×2 (Copy-able and
  String payloads); use-after-consuming-match rejection; by-ref IO
  predicate intent path; the bare-match exception (legal + consuming); the
  move-captured scrutinee payload pin. Regression-first: 4 verified failing
  pre-fix; the arm-body pin uses the two-module import-less-error shape
  (same-module errors legally const-share-wrap).
- Batteries: stage2 + borrow_checker + slice-1 guardrails 434/434; FULL
  memcheck 97 passed / 1 skipped.
- Corpus: rerun in progress vs the 4a reference — universe mismatch
  EXPECTED (stdlib + fixture sources changed); the new run becomes the
  phase reference. Expected C3 movement: shapes 1–2 events (6) disappear
  (fixture rewrite + auto-borrowing predicates); shape 3 (1 event) remains
  pending its reporter rule.

### Follow-ups recorded

- Projected-place match scrutinees (`match self.field`) deliberately
  excluded from consumption tracking (partial-move rule) — AUDIT follow-up.
- ConstShare synthesis qualifies fields against the DECLARING module's
  import-visible world — import-less modules silently cannot derive
  ConstShare for their types (probe-verified). Follow-up candidate slice.
- Spec §1.3/§4 wording: record the match-scrutinee exception in the spec
  (doc change, separate from this slice per spec-docs conventions).
