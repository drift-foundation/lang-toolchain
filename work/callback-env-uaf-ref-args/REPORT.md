# Boxed-callback closure env UAF — investigation report (branch `fix/callback-env-uaf-ref-args`)

Status: root cause found, fixed (conservative blanket rejection + defense-in-depth MIR assert), regression-tested (unit + 3 e2e, 14/14 + earlier 41/41 pass). Copy-typed projected captures investigated, found to need real lowering work, explicitly deferred to a separate follow-up doc — not folded into this fix. Version bumped (`0.33.68` → `0.33.69`, no ABI change) and `doc/history.md` entry added. Full repo test suite run still outstanding.

## 1. The original report

Bookkeeper (PushCoin) filed against certified `driftc 0.33.68/abi19`: a move-capturing closure boxed via `core.callback_throw1` gets miscompiled — captured owned fields freed before a worker invokes it (UAF) — when the closure is built inside another boxed-callback's body AND later moved across `conc.spawn`. Same shape built in a real `fn` frame is sound. Secondary finding: reference-typed callback generic args (`CallbackThrow2<&T, &String, R>`) compile but looked unsound at the boxed-call boundary.

## 2. Root cause (confirmed, fixed)

**Not** what the bookkeeper pseudocode literally shows (`move p.execute` as a source expression) — `move` is real grammar, but this specific form does not compile: partial moves of a struct field are already rejected everywhere (`E-AUTO-28a41106`, "move of a projected place is not supported in v1"; "No partial moves" is documented project policy). The real trigger is narrower and easy to hit by accident:

> A struct field is passed **by value** to a callee **inside a boxed-callback lambda body**, with **no explicit `captures(...)` clause** naming it.

Example (confirmed-crashing, `variant_m` in scratch dir):
```drift
var vt = conc.spawn<type String>(core.callback0(| | => {
    return try run_execute(p.execute) catch { "spawn-err" };   // p.execute passed by value, no captures(...) clause
}));
```

Mechanism, in `lang/driftc/stage1/capture_discovery.py`:
- Implicit-capture inference classifies `p.execute`'s usage as a plain **read** (no `move` keyword used) → `use.move = False`.
- But the lambda is a **boxed callback** (`capture_as_move = True`, set by `call_resolver.py` when wrapping as `core.callbackN`/`callback_throwN`), and the `elif use.read: kind = MOVE if capture_as_move else REF` branch **defaults plain reads to MOVE** for boxed callbacks (since an escaping closure can't safely borrow).
- The existing "move captures of projections are not supported yet" rejection only checked `use.move` (True **only** for an explicit `move` expression) — never the branch above. So this MOVE-kind capture of a **projected place** (`p.execute`, not a whole local) sailed through unrejected.
- MIR lowering (`hir_to_mir.py`, the `cap.key.proj` branch inside `_lower_lambda_callback`/`_lower_lambda_immediate_call`) has **no real handling** for a projected MOVE capture — it just does `env_val = self.lower_expr(expr)`, a plain copy-read. It never zeroes the source field.
- Result: the source struct (`p`) still looks like it owns `execute` when `p`'s own scope-cleanup/drop runs later → **double-drop** of the same String/env buffer → UAF.

Confirmed live with `--sanitize=address,undefined`:
```
==...==ERROR: AddressSanitizer: heap-use-after-free ... READ of size 8
#0 drift_string_release string_runtime.c:256
#1 __drift_cb_drop_... (inner closure's env-drop thunk)
#2 __drift_iface_drop_helper
#3 __drift_cb_drop_... (outer struct's own drop, re-dropping the same field)
...
freed by thread T2 here: ... free ...
previously allocated by thread T2 here: ... malloc ...
```
This exactly matches the report: freed before/independent of the worker's own use, via a second drop chain.

### Fix

`lang/driftc/stage1/capture_discovery.py`: moved the "MOVE of a projected place is rejected" check to run **after** `kind` is finally decided (covers both the explicit-`move`-keyword path and the `capture_as_move`-defaulted-read path), instead of only checking `use.move`. Diff is ~15 lines, comment explains why (see file). This turns the silent miscompile into a compile-time diagnostic — consistent with the project's existing "no partial moves" stance (the explicit form of this exact mistake, `move p.execute`, was already rejected the same way; this closes the implicit-capture loophole for the identical unsafe operation).

**Scope of the fix**: rejects the unsafe shape at compile time. Does **not** attempt to make the shape "work" (i.e., does not implement proper move+zero-back lowering for projected captures) — that would be a materially bigger change (new MIR lowering path) for a shape the language doesn't otherwise support (no partial moves). The already-documented-safe workaround (extract the field via `std.mem.replace` into a standalone local first, then `captures(move <local>)`) continues to compile and run correctly — verified.

**Defense-in-depth**: `hir_to_mir.py`'s two `cap.key.proj` MOVE-capture lowering branches now `raise AssertionError` instead of silently copy-reading, in case this ever reaches lowering unrejected in the future (checker/lowering boundary contract) — fails loud at compile time instead of reintroducing the UAF silently.

### Copy-typed projected captures — investigated, explicitly deferred (not folded into this fix)

Per review discussion, I investigated a type-aware refinement: downgrade a MOVE+projected capture to a plain COPY capture when the field's type is Copy (e.g. `p.count: Int`), rejecting only the non-Copy case. Prototyped in `borrow_checker_pass.py::_check_lambda_captures` (post-type-check, has real types via `self._type_of_place`/`self._is_copy`).

**It doesn't work as a checker-only change.** Env construction becomes correct (stores just the `Int` value), but `hir_to_mir.py`'s `_emit_lambda_capture_prologue` binds captures purely by **root local id/name** — it ignores `key.proj` entirely. For a `p.count` capture, the prologue creates a body-visible local literally named `p`, typed as the outer `Prepared` struct, and stores the lone `Int` into it. The lambda body's own `p.count` expression then tries to project `.count` off a value that's already a bare `Int` — LLVM codegen crash: `integer binop requires matching Int/Uint operands (have %Struct_main_Prepared_..., drift.int)`. Confirmed via a driver test (now renamed/repurposed to `test_copy_typed_projected_field_also_currently_rejected`, asserting the current rejected behavior instead — see §3).

**Decision (per review): split the work.** Land the conservative blanket rejection now (reverted the `borrow_checker_pass.py` prototype — clean no-op diff, confirmed via `git diff --stat`). Copy-projected-capture support is a real lowering feature — teaching the prologue to bind a projected capture key as a distinct body-visible binding, addressed by the exact key rather than root binding id — tracked as a separate follow-up: `work/callback-env-uaf-ref-args/projected-copy-captures-followup.md` (written by the user, records the split, the check-in gate, and required follow-up regressions).

## 3. Regressions added

- **Unit test** (`lang/tests/stage1/test_lambda_capture_discovery.py::test_capture_discovery_rejects_implicit_projected_move_for_boxed_callback`): directly drives `discover_captures()` with `capture_as_move=True` + a bare field-read body, asserts the rejection diagnostic. Confirmed fails on pre-fix code, passes post-fix (regression-first, verified by temporarily reverting the source file and re-running).
- **Driver e2e test** (`lang/tests/driver/test_boxed_callback_projected_move_capture_rejected.py`, new file): three cases —
  1. `test_implicit_projected_move_capture_into_boxed_callback_rejected` — full real-source compile of the confirmed-bad (non-Copy `execute`) shape, asserts compile failure with the exact diagnostic text.
  2. `test_projected_move_capture_via_mem_replace_still_compiles` — the safe `mem.replace`-based control, asserts it still compiles **and runs** (exit 0) — no regression to the legitimate pattern.
  3. `test_copy_typed_projected_field_also_currently_rejected` — a Copy-typed field (`p.count: Int`) read the same implicit way is confirmed to ALSO be rejected right now, locking in the conservative scope decision so a future change can't silently narrow it without deliberately updating this test.
- Ran the fix against the broader existing suite for adjacent risk: `lang/tests/stage1/test_lambda_capture_discovery.py` + `lang/tests/driver/test_lambda_catch_binder_capture_discovery.py` + `lang/tests/driver/test_implicit_callback_wrap.py` — **41/41 passed**, no regressions.
- All 3 driver e2e tests + the unit test re-run after reverting the Copy-downgrade prototype — passing (see §"Copy-typed projected captures" above for why the prototype was reverted).

## 4. K's alternative hypothesis — tested, did not reproduce

K's notes proposed a **different** root cause: `ownership_ledger.py::_identify_return_consumed_loads` traces return-sources through `ConstructStruct`/`ConstructVariant`/`ConstructResultOk`/`ConstructIfaceValue` but **not** `ConstructIface` — so a boxed callback **returned directly** (or wrapped in a struct) from another boxed callback's body might get a wrong `MUST_DROP` verdict instead of `MOVED_OUT`.

K staged 5 repro files at `/tmp/drift-callback-uaf-repros/` to test this (all using **explicit** `captures(move p)`/`captures(move execute)` of a **whole local**, not a projection — structurally different from what I found). The files as staged had unrelated syntax bugs (`return` inside expression-form lambda bodies/catch-arms, invalid bare-value match arms) that made all 5 fail to compile for reasons unconnected to the bug; I fixed the syntax (preserving the intended structural shape) and re-ran:

- 2 of 5 (`*_callback_direct_bad`, `*_callback_direct_good`) hit an **unrelated, pre-existing** codegen gap: `NotImplementedError: LLVM codegen v1: FnResult ok type INTERFACE is not supported yet` — a `throws -> core.CallbackThrow1<...>` function (interface as a can-throw fn's Ok payload) isn't supported at all yet, independent of this bug. Not fixable in this scope; noted as a pre-existing limitation.
- The other 3 (`Prepared`-wrapped bad/good, plus the ref-args variant) **compiled and ran clean**, both plain and under `--sanitize=address,undefined`. No UAF.

**Conclusion: K's `ConstructIface` return-tracing hypothesis does not appear to be a real, independently-triggerable bug** — at least not via explicit whole-value `captures(move ...)`, which is the pattern it would need to go through. The bug is specifically the **implicit/projected**-capture path described in §2. (It's possible a `_identify_return_consumed_loads` gap for `ConstructIface` exists in the abstract, but nothing I could construct made it observable — plausibly because a whole-value capture's ownership is tracked correctly via a different code path than the one degraded here.)

## 5. Secondary finding — reference-typed callback args

Original report: `CallbackThrow2<&T, &String, R>` compiles/type-checks but looked unsound across the boxed-call boundary. K's notes caution against a blanket rejection (repo has real positive coverage for `Callback2<&Req, &mut Ctx, R>` web/rest dispatch) and suggest the real contract is narrower: refs escaping via a **returned, spawned** callback, not every ref callback param.

K's `boxed_callback_ref_args_returns_move_capture_spawn.drift` repro (fixed syntax, same treatment as above) **compiled and ran clean**, plain and under ASAN — no crash observed. I have **not** found a concrete failing case for this secondary finding. Two possibilities: (a) it's actually fine as tested and the original report's instability was really just the primary UAF bug manifesting in a ref-arg-shaped program, or (b) the real trigger needs a detail neither repro captured (e.g. the ref genuinely escaping past the boxed call's own stack frame in a way my test's immediate `.call()`+return doesn't exercise). **Not treating this as fixed or ruled out** — flagging as open, would want either a more specific repro from the reporter or to treat it as likely-non-issue pending one.

## 6. Outstanding / not yet done

- Version bump: done. `DRIFTC_VERSION` 0.33.68 → 0.33.69 in `lang/versions.py`, no ABI change (`DRIFT_RT_ABI_VERSION` stays 19). `doc/history.md` entry added and updated to reflect the conservative split.
- Driver e2e test file: 3/3 passed (including the new Copy-still-rejected lock-in test).
- 44/44 across capture-discovery unit + driver e2e (3 new tests) + `test_implicit_callback_wrap.py` (all arities/sites) + lambda-catch-binder-capture-discovery suites, on the final state (post-revert of the Copy-downgrade prototype, with the `hir_to_mir.py` defense-in-depth asserts in place). No regressions.
- Repo-wide full test suite run — still not run; only the targeted suites above (44 tests spanning the adjacent-risk surface). Recommend a full `just test`-equivalent pass before landing.
- `/tmp/drift-callback-uaf-repros/*.drift` — I edited 5 of these files in place (fixing unrelated syntax so they'd actually compile) to test K's hypothesis. They're scratch/tmp files, not part of the repo, so no repo diff from this — flagging in case K wants to know their staged files were modified.
- Haven't cleaned up `/tmp/claude-1000/.../scratchpad/callback_uaf_repro/` (my own scratch dir) — harmless, session-scoped.
- No git add/commit/staging performed — patch is in the working tree on `fix/callback-env-uaf-ref-args`, ready for review.
- Copy-projected-capture support: deferred, tracked in `work/callback-env-uaf-ref-args/projected-copy-captures-followup.md`.

## 7. Files changed

- `lang/driftc/stage1/capture_discovery.py` — the fix (moved rejection check to run after `kind` is decided; blanket, not Copy-aware).
- `lang/driftc/stage2/hir_to_mir.py` — defense-in-depth: both `cap.key.proj` MOVE-capture branches now assert instead of silently copy-reading.
- `lang/driftc/borrow_checker_pass.py` — Copy-downgrade prototype added then reverted; clean no-op diff (confirmed via `git diff --stat`).
- `lang/tests/stage1/test_lambda_capture_discovery.py` — new unit regression (rejection).
- `lang/tests/driver/test_boxed_callback_projected_move_capture_rejected.py` — new driver e2e regression (3 tests: non-Copy rejected, mem.replace control still works, Copy-also-currently-rejected lock-in).
- `lang/versions.py` — `DRIFTC_VERSION` 0.33.68 → 0.33.69.
- `work/callback-env-uaf-ref-args/projected-copy-captures-followup.md` — follow-up scope doc (written by the user).
- `doc/history.md` — new entry.
