# Exception ^capture Unwind-Time Stacking — Plan

**Status:** planning / pre-branch. Implementation request.

**Working directory:** `work/exception-capture-unwind-stack/` (ephemeral).
The plan is the alignment artifact; the eventual implementation
lives in `lang/driftc/stage2/hir_to_mir.py` and the regression test
lands in `lang/tests/driver/`.

**Branch (when started):** suggested name
`feature/exception-capture-unwind-stack`.

**Target version:** 0.31.40.

---

## Goal

When an exception unwinds through a chain of frames that each declare
`^capture` locals, **every transited frame contributes its `^capture`
set** to the propagating Error. The Error's `e.captures` map ends up
shaped as a stack of per-frame captures — exactly what the existing
`e.captures["module::function"]["key"]` access shape was designed for.

Concrete example (the user's mental model):

```drift
fn outer() throws -> Int {
    ^var wo_id: Int = 42;       // outer's ^capture
    return middle();
}

fn middle() throws -> Int {
    ^var task_name: String = "submit";  // middle's ^capture
    return inner();
}

fn inner() throws -> Int {
    ^var step: String = "validate";    // inner's ^capture
    throw Bang();
}
```

After the unwind, the caught error contains:

```
e.captures["main::outer"]["wo_id"]      = DV.Int(42)
e.captures["main::middle"]["task_name"] = DV.String("submit")
e.captures["main::inner"]["step"]       = DV.String("validate")
```

Today (0.31.39): only `inner`'s captures are recorded. `middle`
and `outer` are silent transit frames.

---

## Current behavior (0.31.39)

`lang/driftc/stage2/hir_to_mir.py:_emit_captured_locals` is called
**only** at the `HThrow` site (`:7377`). It writes
`M.ErrorAddLocalDV(error, frame=current_fn_symbol, key, value)` for
every `^var` active at the lexical throw position in the throwing
function.

`_emit_captured_locals` is **NOT** called at:

- `_visit_stmt_HRethrow` (`:7417`) — rethrow inside a catch arm.
- `_propagate_error` (`:7390`) — the function-exit unwind path that
  constructs `FnResult.Err` and returns.
- The post-throws-call propagation paths: `:6303`, `:6309`, `:7388`,
  `:7432`, `:7546`, `:7553` (and any other site where a callee's
  `FnResult.Err` is propagated through the current frame without
  being caught).

Net effect: only the throwing frame's captures attach. Frames the
exception transits through are silent.

Doc behavior ("if an exception crosses this frame, the runtime
records: locals: { ... }") describes the **target**, not the
implementation.

---

## Target behavior

Captures attach at every transit point:

1. **Throw of a fresh exception** — throwing frame's `^vars` recorded.
   *(0.31.39: works.)*
2. **Rethrow inside a catch arm** — the rethrowing frame's `^vars`
   recorded onto the existing Error.
3. **Callee-thrown error propagating through this frame** (no catch
   in this frame, error returns via `FnResult.Err`) — this frame's
   `^vars` recorded onto the propagating Error.

The "stack of captures" emerges naturally: each transited frame
adds its layer; deeper frames are recorded earlier (innermost first).
The frame-keyed structure (`e.captures[frame_symbol][key]`) keeps
each frame's contributions distinct.

---

## Design — where to hook

### Approach (chosen): emit at the propagation site, not inside `_propagate_error`

`_propagate_error(err_val)` is called from two semantically distinct
positions:

- **(a) After `HThrow` builds a fresh Error** (line 7388). The
  throwing frame's `^vars` are *already* on the Error from
  `_emit_captured_locals(err_val)` at `:7377`. No further emit needed.
- **(b) Anywhere else** — `HRethrow`, post-callee-Err, etc. The
  Error in flight does NOT yet have the current frame's `^vars`.
  We emit before propagating.

A naive `_emit_captured_locals` inside `_propagate_error` itself
would **double-emit on the (a) path**. Wrong.

Instead, **emit at the propagation call site** — keeping the
"emit before propagate" pairing local to each site that needs it,
and leaving the HThrow path unchanged.

Concrete call-site changes (all in `hir_to_mir.py`):

| Line | Context | Change |
|---|---|---|
| `:7377` | `_visit_stmt_HThrow` | **No change.** Throwing frame already emits captures. |
| `:7388` | `_visit_stmt_HThrow`'s call to `_propagate_error` | **No change.** Already covered by the line-7377 emit. |
| `:7417`–`:7432` | `_visit_stmt_HRethrow` | **Add `_emit_captured_locals(err_val)`** before `_propagate_error(err_val)` at line 7432. |
| `:6303` / `:6309` | `_visit_stmt_HTry` (statement-form) — no-arm-matched fallthrough | **Add `_emit_captured_locals(err_tmp)`** before each `_propagate_error(err_tmp)`. |
| `:7546` / `:7553` | `_visit_stmt_HTry` (different overload, line ~7434+) — no-arm-matched fallthrough | **Same.** |
| `:3048` | (need to confirm context) | **Audit and add if applicable.** |
| Throws-call lowering (Err branch, multiple call sites) | When a callee returns `FnResult.Err` and current frame doesn't catch | **Add `_emit_captured_locals(err_val)`** before `_propagate_error(err_val)`. |

The throws-call lowering sites are the load-bearing additions —
that's the case where a callee's error transits through the current
frame without being caught.

### Alternative considered: emit inside `_propagate_error`'s escape branch (rejected)

The escape branch (line 7404+) handles "no outer try → return
`FnResult.Err`". Emitting there would double-emit on the HThrow
path. Could be salvaged by adding a flag parameter
(`originated_here: bool`), but that bleeds an awkward concern into
`_propagate_error`'s signature. Call-site emit is cleaner.

### Alternative considered: emit on FnResult.Err return only (rejected)

Only emitting at the function-exit point (the construction of
`FnResult.Err`) misses the case of a fresh error that's caught and
rethrown in the same function — the rethrowing frame's captures
should be added on rethrow. The call-site approach handles rethrow
naturally.

---

## Per-frame keying — semantic decision

`M.ErrorAddLocalDV(error, frame, key, value)` writes into
`e.captures[frame][key]`. Frame is the function symbol
(e.g., `"main::outer"`).

**What if the same key is written by two frames?**
Different frames → different outer dict keys. No collision.
`e.captures["outer"]["wo_id"]` and `e.captures["middle"]["wo_id"]`
are distinct. ✓

**What if the same frame is recorded twice (recursion / loop with
throw inside)?**
Today's implementation: `M.ErrorAddLocalDV` likely overwrites the
inner-key value. Need to verify and decide:

- (i) **Last-write wins** (current behavior, probably). For recursive
  unwinds, the outermost recursion frame's value wins. Cheap, simple,
  may surprise users.
- (ii) **First-write wins**. The innermost recursion frame (closest
  to the throw) wins. Matches "deeper frames recorded earlier" but
  requires a check at each emit.
- (iii) **Per-frame stack array** — `e.captures[frame][key]` is an
  array, both values appended. Memory-heavy; rarely useful.

**Decision (proposed for sign-off):** (i) **last-write wins**, with
the documentation note that recursive-frame ^captures are not
per-recursion-level. Matches the "frame symbol = function" keying
already in place. Users wanting per-recursion attribution should add
a level discriminator to the capture name (`^var step_lvl_0 = ...`).

---

## Regression test

**File:** new — `lang/tests/driver/test_exception_capture_unwind_stack.py`.

**Primary regression:**

```drift
exception Bang()

fn inner() throws -> Int {
    ^var step: String = "validate";
    throw Bang();
}

fn middle() throws -> Int {
    ^var task_name: String = "submit";
    return inner();
}

fn outer() throws -> Int {
    ^var wo_id: Int = 42;
    return middle();
}

fn main() nothrow -> Int {
    try {
        val x = outer();
        return 0;
    } catch Bang(e) {
        // Pre-fix: e.captures contains only main::inner.
        // Post-fix: e.captures contains main::inner + main::middle + main::outer.
        val step      = e.captures["main::inner"]["step"];
        val task_name = e.captures["main::middle"]["task_name"];
        val wo_id     = e.captures["main::outer"]["wo_id"];
        // Assertions in test fixture verify each DV unwraps to expected value.
        return 0;
    }
}
```

**Secondary regressions:**

| Test | Asserts |
|---|---|
| `test_throw_only_innermost_frame_captures_attached` | Baseline: HThrow's frame still attaches. (No regression of 0.31.39 behavior.) |
| `test_rethrow_attaches_rethrowing_frame_captures` | A frame catches, then rethrows; rethrowing frame's `^vars` attached on rethrow. |
| `test_caught_error_after_unwind_has_full_stack` | The chained example above; assert all three frames recorded. |
| `test_no_capture_decls_no_emit` | Frame with NO `^var` declarations transits an error: no extra entry, no crash. |
| `test_partial_chain_some_frames_have_captures` | `inner` has `^var`, `middle` has none, `outer` has `^var`. Caught error has `inner` + `outer` only. |
| `test_recursion_last_write_wins` | Recursive function with `^var step`. Recursive throw produces a single frame entry; per S(i) above, the outermost recursion-level value is observed. |

**Validation method:** test compiles the fixture, runs the binary,
checks via stdout/exit-code that the catch arm reads the expected
DiagnosticValues. The fixture pattern follows
`lang/tests/driver/test_match_by_ref_variant.py:_compile_and_run` —
spawn the driver as subprocess, link, run, assert exit code or
parse stdout output.

---

## Phases

### Phase 1 — audit hook points (~0.5 day)

**Deliverable:** complete list of `_propagate_error` call sites in
`hir_to_mir.py` with classification:

- (a) **Already-covered** (post-HThrow) — no change.
- (b) **Add emit** (rethrow, post-callee-Err, no-catch fallthrough).

Audit lives as a comment block at the top of the implementation
patch. No compiler change in this phase.

### Phase 2 — implement emit-at-propagation (~0.5 day)

For each (b) site identified in Phase 1, add
`_emit_captured_locals(err_val)` immediately before
`_propagate_error(err_val)`.

Single-commit patch. ~5–8 line edits. Each edit is mechanical
(insert one line); the audit table is the load-bearing thinking.

### Phase 3 — regression test (~0.5 day)

Land `test_exception_capture_unwind_stack.py` (the 6 tests above).
Compile + run + assert pattern. Pre-fix: primary regression fails
(only inner's captures recorded). Post-fix: passes.

### Phase 4 — verification (~0.5 day)

- Full driver / stage1 / checker / packages / memcheck suites.
- Spot-check: build a couple of representative downstream packages
  (web-rest stress-test if available) to confirm no perf regression
  or unexpected behavior shift.

**Total: ~2 days, single patch deliverable.**

---

## Acceptance criteria

The track wraps when **all** of these are true:

1. New regression test file `test_exception_capture_unwind_stack.py`
   exists; all 6 tests pass.
2. `_emit_captured_locals` is called at every (b) site identified
   in Phase 1 audit.
3. HThrow path's behavior is unchanged (no double-emit).
4. Full driver / stage / checker / packages / memcheck suites green.
5. Compiler version bumped 0.31.39 → 0.31.40.
6. `docs/history.md` entry naming the symptom, fix, regression
   coverage, and the per-frame keying rule (S(i) — last-write wins
   for same-frame recursion).
7. ABI unchanged (still 10) — no compiler/runtime boundary shift.
   Verified against `test_abi_version_stamp.py`.
8. App team's bookkeeper repro (`/home/sl/src/pushcoin/work/repro-bug-a/`
   or whatever follow-up they file) verifies the chained
   `^wo_id`/`^task_name`/`^task_id` behavior end-to-end.

---

## Stop-and-escalate triggers

Halt and consult before proceeding if any of these fire:

- **Phase 1 audit reveals more than 10 propagation sites.** Suggests
  the propagation surface is larger than expected; design may need
  to consolidate via a wrapper helper (e.g., `_propagate_with_capture
  _emit`) rather than duplicating the emit at each call site.
- **`_emit_captured_locals` is non-idempotent in some path** that
  the audit didn't anticipate (e.g., a code path that's reached
  twice for the same Error). Need to detect and guard, OR rework
  the emit logic to be idempotent (e.g., a per-Error/per-frame
  dedup at runtime — undesirable, would prefer to fix the path).
- **Memcheck regression in any test case.** Adding emits adds
  writes to the Error's captures map; if those writes happen in a
  cleanup path that wasn't accounting for the captures' refcount,
  could leak. Standard memcheck gate catches this.
- **`.dmp` byte-determinism breaks** for any package that uses
  `^capture`. The emit count changes per-frame which means MIR
  shape per-package shifts — but `.dmp` shouldn't be affected
  since it doesn't carry MIR-level `_emit_captured_locals` ops.
  Verify just to be sure.

---

## Out of scope (will not do in this track)

- **Q1: whole-`Error.attrs` enumeration** (separate design track —
  `Map<String, DiagnosticValue>`-shaped read-only view). Independent
  of this work; the `e.captures` access shape is already sufficient
  for the unwind-stack feature without iteration support.
- **Per-recursion-level captures** for recursive functions (S(iii)).
  Decision: last-write wins (S(i)); per-recursion attribution is
  user-facing concern (use distinct ^var names).
- **Capture filtering / sampling** at high transit depth. If the
  ^var count × frame depth becomes a perf concern in production,
  filtering can be added later. Not blocking for the feature.
- **Cross-package frame keying.** Frame symbol uses
  `function_symbol(self._current_fn_id)` which already includes
  module-id-qualified naming. Already cross-package-correct;
  unchanged.
- **Runtime-side changes.** All hooks are MIR-emission level.
  Runtime helpers (`drift_error_add_local_dv` or equivalent) already
  exist and handle the per-frame map storage. No runtime ABI
  changes.

---

## Open questions (to resolve before Phase 1 starts)

1. **Per-frame collision rule (S above).** Last-write-wins (S(i))
   is the proposed default. Sign off explicitly.
2. **Catch-and-rethrow attribution.** When a frame catches an error
   in arm A and rethrows from arm B, are arm A's `^vars` (active
   when the catch fired) ALSO attached, or just the rethrow site's
   `^vars`? Lean toward "rethrow site's `^vars` only" — the catch
   bound the error to a binder and the user has explicit access to
   `e.captures` if they want to record the catch context, plus they
   can mutate before rethrow if Q1 lands.
3. **Inlined / generic-instantiated function frame keying.** A
   monomorphized `inner__inst__T_Int` — does the frame symbol use
   the canonical (`inner`) or instantiated (`inner__inst__T_Int`)
   name? Lean toward instantiated (matches the actual lowered
   symbol; user doing post-mortem can identify the exact instance).
4. **`HTryExpr` (expression-form try)** — separate from `HTry`.
   Phase 1 audit must include both forms.

---

## Why this is "soon," not "now"

- Q2 fix (0.31.39) closes the immediate user-reported gap (catch
  binder access in capture lambdas). The unwind stack is the
  next-level desirable behavior, not a hot blocker.
- Bookkeeper team can ship today's diagnostic logging using
  catch-arm-side attribution (their explicit `^wo_id` capture in the
  route closure is recorded at the route's own throw point if they
  rethrow, and post-Q2 they can read `e.attrs[...]` cleanly).
- The work is small (~2 days), well-scoped, and independently
  reviewable. Worth landing as 0.31.40 within the next compile-cycle
  windowing.

---

## Notes on what this plan is NOT

- **Not** a green light to start Phase 1 — needs user approval of
  the design (call-site emit vs. wrapper) and S(i) per-frame
  collision rule.
- **Not** a workaround for any one bug. The user reported it as a
  follow-on Q3 question after Q2 was diagnosed; today's behavior
  doesn't crash, just under-records.
- **Not** related to lambda-body-as-fresh-CallableContext (separate
  track at `work/unified-lambda-as-fn/plan.md`). Independent change.
- **Not** under source/stdlib/tests/tooling. `work/` is ephemeral;
  durable artifacts (regression test, history entry) move to their
  proper homes as deliverables of Phase 2/3.
