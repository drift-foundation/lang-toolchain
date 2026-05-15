# PR2 handoff — `std.concurrent.Condvar`

PR1 (`vt_is_cancelled` + `MutexGuard` unlock/relock state) landed
cleanly.  PR2 (the Condvar itself) is **blocked on two pinned
Drift toolchain bugs**.  Each blocker has a regression test in
`lang/tests/driver/` that flips green when the underlying bug is
fixed — so unblock work is fully traceable.

## Blockers (both pinned)

### Bug 1: `Array<T>.len()` (method-call syntax) does not resolve
**Pin:** `lang/tests/driver/test_array_len_method_call_resolution_blocker.py`
**Diagnostic:**
```
no matching method 'len' for receiver Array<Int>          — E-AUTO-7b9868f6
no matching method 'len' for receiver Ref<Array<Int>>     — E-AUTO-40773e95
no matching method 'len' for receiver RefMut<Array<Int>>  — E-AUTO-b9bf78ff
```
**Root cause hypothesis:** `arr.len` is field-access magic in
`type_checker.py` at the `HField` handler (~line 8957: `expr.name
in ("len", "cap", "capacity", "gen")`).  The **method-call** form
`arr.len()` has no parallel routing — no `len()` method is
registered on `Array<T>` in any of the three receiver shapes.

**Workaround:** use `arr.len` (no parens) — the field-access form
works on owned receivers.  Composition through `&Array<T>` /
`&mut Array<T>` inside larger compilation units is verified by
the next-bug repro (which uses `arr.len` and compiles past
type-check).

### Bug 2: Ownership ledger PathDependent on conditional-move from `Array.remove()` loop
**Toolchain pin:** `lang/tests/driver/test_array_remove_conditional_move_path_dependent_blocker.py`
(asserts the ICE message verbatim; flips red when fix lands.)

**Architecture pins (post-fix, red today):**
- `lang/tests/stage2/test_cleanup_authoring_flag_guarded.py` (2 strict
  `xfail` cases — non-variant PathDependent + flag-managed → guarded
  emit; MUST_DROP + flag-managed → uniform flag clear).
- `lang/tests/codegen/e2e/conditional_move_loop_destructor_order/`
  (skip-marked; un-skip when fix lands; pins user-observable
  end-of-iteration destructor timing via stdout discriminator on `I`
  iteration-top markers).

**Stage2 ICE today:**
```
RuntimeError: drop_before_overwrite: ledger returned
PathDependent at (fn=drain, block=array_remove_exit, idx=4,
local=w).  Tier-1 promotion retired the
`initialized_destructibles` fallback — if PathDependent is now
reachable, either tighten the lattice or restore a flag-guarded
path here before re-landing.
```

**Repro shape:**
```drift
while arr.len > 0 {
    var w = arr.remove(0);
    if w.raw > 0 { out.push(move w); }
    // else: w dropped implicitly
}
```

**Why it matters:** canonical iterate-and-claim pattern used by
Condvar's `_claim_one`, `_drain_active`, `_prune_inactive`.

**Locked architecture (reviewed with K):**

```
build ledger
drop_flags PLANNING ONLY  (entry init, set/clear, metadata; no emit)
rebuild ledger            ← REQUIRED — set/clear shifted (block, idx)
cleanup_authoring         ← sole drop emitter; guarded branch for
                            non-variant PathDependent + flag-managed;
                            uniform flag-clear on every flag-managed drop
rebuild ledger
string_arc                ← unchanged; tripwire stays
```

Key invariants:
- **No flag consult at `drop_before_overwrite`** — ledger is the single
  source of truth at site 4.
- **Flag bit ≡ "currently owns destructible storage."** Every
  cleanup-authored MoveOut on a flag-managed local clears the flag,
  guarded or not.
- **Explicit `func._drop_flag_for_local: dict[str, str]`** metadata
  (not name-derivation) — needed because `_allocate_flag_name` collision
  suffixing makes `flag_local_name_for(L)` unsafe.
- **Two ledger rebuilds.** Pre-cleanup_authoring rebuild required;
  planning shifts `(block, idx)` keys.

**Drift implementation files to touch:**
1. `lang/driftc/stage2/drop_flags.py` — strip Step-5 emission; broaden
   trigger to include non-variant PathDependent at any `CleanupHook`;
   attach `_drop_flag_for_local`.
2. `lang/driftc/stage2/cleanup_authoring.py` — new guarded-emit branch
   (lift the existing `_insert_flag_guarded_drops` helper from
   drop_flags); uniform flag-clear after every cleanup MoveOut on
   flag-managed locals; new telemetry tags
   (`path_dependent_flag_guarded_emit`, `must_drop_flag_clear`).
3. `lang/driftc/driftc.py:7053-7196` — swap pass order; add pre-cleanup
   ledger rebuild; remove post-drop_flags rebuild (redundant once
   drop_flags doesn't emit).

## What PR1 landed (clean, verified)

```
lang/codegen/llvm/llvm_codegen.py
lang/language_runtime/posix/thread_runtime.c
lang/tests/codegen/e2e/mutex_guard_condvar_unlock_relock/
lang/tests/codegen/e2e/vt_is_cancelled_basic/
stdlib/lang/thread.drift                   (vt_is_cancelled export + docstring)
stdlib/std/concurrent/concurrent.drift     (MutexGuard.locked + unlock/relock pair)
work/stdlib-condvar/plan.md
```

## What PR2 has in flight (does not compile)

`stdlib/std/concurrent/concurrent.drift` contains the full
Condvar API + impl skeleton — review-clean shape, all 6 review
findings from the previous session addressed in source:

1. ✅ `MutexGuard.get_mut`/`borrow_mut` + `mutex_guard_get_mut`
   gated by `assert(self.locked, ...)`.
2. ✅ Dead-waiter pruning via `_prune_inactive` called after
   self-claimed wake (only when a waiter actually returned to
   the active list).
3. ✅ `wait_until(0)` infinite-wait fixed via `has_deadline: Bool`
   flag in `_wait_inner` (NOT `deadline_ms = 0` sentinel).
4. ✅ `wait_timeout` negative-duration validation via
   `_check_duration(d)`.
5. ✅ `vt_is_cancelled` docstring reframed as compute-loop
   cooperative-cancellation only (parking primitives can't
   surface CANCELLED because the scheduler reaps cancelled VTs
   at re-dispatch — `thread_runtime.c:858-872`).
6. ✅ `mutex_guard_get_mut` test docstring fix queued.

Compile fails at the two pinned bugs above.

## Unblock path

When **either** bug is fixed, the corresponding pin test flips
to failing — that's the signal to come back here and finish.

**Bug 1 fix → unblocks length checks.** Sweep
`stdlib/std/concurrent/concurrent.drift` for any `.len()` calls
re-added; current code uses field-access `arr.len`.

**Bug 2 fix → unblocks waiter-list drains.** All three helpers
(`_claim_one`, `_drain_active`, `_prune_inactive`) currently use
the conditional-move-from-remove pattern.

Once both compile, the remaining work is:
- Smoke-test stdlib compile + existing test suite green.
- Add the 15 e2e tests from `work/stdlib-condvar/plan.md`'s
  test plan section (we dropped `condvar_cancel_during_wait` per
  the PR1 scheduler finding).
- Sign off and merge.

## Notes for the unblocker

- Don't switch data structures (e.g., Deque<T>) to work around
  Bug 2.  User feedback was explicit: pause and pin, don't
  swerve.  The bugs are real toolchain gaps that other users
  will hit.
- Both pin tests are designed to flip cleanly: when assertions
  start failing because the compile now succeeds, edit the test
  to assert clean compile + correct runtime behavior.
- Memcheck must be in the verification gate for any site-3
  authority work (per MEMORY.md, 0.31.9 follow-up).

— continued 2026-05-15
