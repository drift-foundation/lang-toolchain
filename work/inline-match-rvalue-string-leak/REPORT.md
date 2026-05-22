# Patch: inline-match value-producing String-payload leak

**Filed**: 2026-05-22 (follow-up to 0.32.7→0.32.8 Arc/VT teardown fix)
**Repro source**: `work/bookkeeper-shutdown-hang/repro-bare-leak/` (PushCoin) + `lang/tests/memcheck/test_inline_match_rvalue_string_payload_leak.py` (in-tree)
**Status**: fix applied + 5-case regression suite green + DRIFT_SHUTDOWN_TRACE diagnostics from the previous fix stripped

## Trigger

```drift
val secret: String = match env.get("REPRO_VAR") {
    Optional::Some(v) => { move v },
    Optional::None    => { "default" }
};
return 0;
```

When `REPRO_VAR` is set, the heap-allocated `Some` payload leaks. When unset (`None` arm taken), no leak. Leak size scales with payload length. Reduced from the residual 32-byte bookkeeper leak after 0.32.8 closed the Arc/VT teardown class; bookkeeper sees the leak in `config.drift::load_from_argv`'s `application_secret` shape.

No struct / Arc / registry / pool / REST / shutdown machinery is required to reproduce.

## Root cause

`lang/driftc/stage2/hir_to_mir.py`, value-context match arm lowering.

When the match scrutinee is an **rvalue** (the SSA value returned by a function call, e.g. `env.get(...)`) and the match arm binds a payload field:

- Statement-context match (`match expr { … }`) ALWAYS materializes the scrutinee into a stack-slot via `_ensure_arm_scrut_ptr` (line 1714), so scope drop releases the variant after the match.
- Value-context match (`val x = match … { … }`) only materializes when **any** payload field is non-Copy (`not _should_copy_value(f_ty)`).

For Copy-classified types that still need runtime drop — `String` and any other refcounted scalar — `_should_copy_value` returns True, so `need_addr_binders` stays False, and the scrutinee remains as a bare SSA value. The arm body uses `VariantGetField` (LLVM `copy-semantic` transfer) which:

1. Loads the payload field via GEP.
2. Calls `drift_string_retain` on the loaded value → binder owns its own `+1`.
3. **Leaves the variant SSA value untouched** — the variant's payload-field `+1` is never released.

Net result: refcount stays at `> 0` for the lifetime of the process. Trace from `/tmp/sa` with `DRIFT_STR_TRACE=1`:

```
[str] retain ptr=0x…c00 prev=1 -> 2     # arm-0 retain
[str] retain ptr=0x…c00 prev=2 -> 3     # secret-store retain
[str] release ptr=0x…c00 prev=3 -> 2    # transient release
[str] release ptr=0x…c00 prev=2 -> 1    # secret end-of-scope release
                                         # ↑ no further release → leak
```

The "no further release" is the variant's payload-field release that should fire when the rvalue variant temp goes out of scope after the match.

## Fix

`lang/driftc/stage2/hir_to_mir.py`, +16 lines (one extra condition + comment):

```python
need_addr_binders = False
for fidx in field_indices:
    ...
    f_ty = arm_def.field_types[fidx]
    if not self._should_copy_value(f_ty):
        need_addr_binders = True
        break
    # NEW: Copy-classified payload that still has refcount (or other
    # runtime-drop) semantics — e.g. String, Array<X>, struct-with-drop
    # — must take the materialization path so the rvalue variant temp
    # gets a scope drop after the match.
    if self._needs_runtime_drop(f_ty):
        need_addr_binders = True
        break
```

This forces the materialization path (`_ensure_arm_scrut_ptr`) when any payload field has runtime-drop semantics (refcount, structural drop, Destructible, etc.), regardless of Copy classification. The materialization path:

- Moves the rvalue into a fresh stack-slot local (`__match_scrut_tmp…`).
- Registers it for scope cleanup.
- Uses `VariantGetFieldAddr` + `LoadRef` + `CopyValue` for Copy payloads — same retain semantics on the binder side, but now the variant's own `+1` is correctly released by scope drop.

No changes needed to LLVM codegen, drop-policy compute, or string_arc. The existing CleanupHook + chain-aware ledger walker handles the materialized variant correctly; we were just skipping into its territory for the wrong reason.

## Why this didn't surface earlier

The bug class is "value-producing inline match binding a Copy-droppable payload from an rvalue scrutinee" — not String-specific despite the surface symptom. Today the **only** Copy + runtime-drop type in the language is `String` (refcounted scalar: `Copy=True` because retain is cheap, `has_drop=True` because the refcount must be released). The moment another such type lands (refcounted Symbol, interned identifier, any future Copy-cheap-clone+drop scalar), it would leak through the same path without this fix.

Even within "String + this shape", the bug is uncommon in stdlib internals — the prevailing Optional-handling idiom is either (a) two-step (`val opt = …; match opt`) or (b) statement-form (`var s = default; match { Some(v) => { s = move v }, None => {} }`), both of which take the materialization path or never bind. `env.get` returning to an inline value-producing match is what bookkeeper's `application_secret` shape hits.

## Test coverage

`lang/tests/memcheck/test_inline_match_rvalue_string_payload_leak.py` — 8 cases under valgrind `--leak-check=full --show-leak-kinds=definite,indirect --error-exitcode=97`:

| Shape | Description | Pre-fix | Post-fix |
|---|---|---|---|
| A (Some arm) | `val s = match env.get(K) { Some(v) => move v, None => lit }`, env set | LEAK 28B+ | clean |
| A (None arm) | same shape, env unset (None arm taken) | clean | clean |
| B | two-step `val opt = env.get(K); match opt { … }` | clean | clean |
| C | statement-form `var s = lit; match … { Some(v) => { s = move v }, None => {} }` | clean | clean |
| D | inline match on user fn `Optional<String>::Some(fmt.format_int(N))` (allocated payload, NOT env.get) | LEAK | clean |
| E | inline match on user fn returning `Result<String, E>::Ok(fmt.format_int(N))` | LEAK | clean |
| F | multi-field ctor `Filled(label: String, count: Int)` — binds both String and Int | LEAK | clean |
| G | discarded result `val _ = match … { Some(v) => move v, None => lit }` | LEAK | clean |

Shapes A (None arm), B, C act as positive controls — already clean before the fix and must stay clean. Shapes A (Some arm), D, E, F, G all failed before the fix and pass after.

**Note on an earlier false-positive control.** An initial version of Shape D used `"literal".clone()` for the payload, but Drift's static-string optimization makes the literal-clone STATIC (no refcount ops, no leak surface). That made the case clean even on the buggy compiler — not because the fix worked, but because the code path the fix touches was never exercised. Replaced with `fmt.format_int(N)` which produces a real heap-allocated refcounted String. Caught by reviewing the bug class definition: "Copy + runtime-drop payload" is the trigger, not "any payload in an inline match".

Shape coverage spans:
- Two variant families (Optional, Result) → fix is not variant-name-specific.
- Single-field and multi-field ctors → fix's per-field loop break-on-first-hit works.
- Named result (`val x = match …`) and discarded result (`val _ = match …`) → drop chain works in both.
- env.get and user fn scrutinees → fix is not env.get-specific.
- Some/Ok arm taken (heap allocated) AND None/Err arm taken (no allocation) → both arms get correct scope cleanup.

## Drive-by cleanup (in same patch)

`lang/language_runtime/posix/thread_runtime.c`: stripped the 11 `DRIFT_SHUTDOWN_TRACE`-gated `fprintf` prints added during the 0.32.7 Arc/VT shutdown-hang investigation. Stage validation cleared 2026-05-21 (398 ms clean shutdown on bookkeeper) and the team confirmed they should come out. -11 lines.

## Diff summary

```
 lang/driftc/stage2/hir_to_mir.py             | 16 ++++++++++++++++
 lang/language_runtime/posix/thread_runtime.c | 11 -----------
 lang/tests/memcheck/test_inline_match_rvalue_string_payload_leak.py | (new)
 2 files changed, 16 insertions(+), 11 deletions(-)
 1 file added (regression test, ~210 lines)
```

## Risk surface for reviewer

1. **Compilation-time impact of forced materialization**: trivial. Materialization is one extra alloca + AddrOfLocal per Copy-droppable-payload match arm. No effect on hot paths.
2. **Runtime overhead**: the materialized path emits one extra `StoreLocal` + the same CopyValue retain that the non-materialized path was already doing. The net retain/release count goes from `(retains) > (releases)` to balanced. No extra retains.
3. **Behavioral change scope**: ONLY value-producing inline match (`val x = match …`) over an rvalue scrutinee whose ctor binds a Copy + runtime-drop field (String today; in principle any future Copy+drop type). Statement-form match, ref-scrutinee match, and rvalue match with non-Copy payloads are all unchanged (they were already on the materialization path).
4. **Adjacent invariants**: the `arm_scrut_payload_moved` partial-move tracking and `match_cleanup_authoring`'s `field_verdict_at` query remain authoritative. For Copy+drop payloads with this fix, the field stays LIVE (CopyValue, not MoveOut), so the variant scope drop releases all payload fields — same shape as the working materialization-because-of-non-Copy-binder case.
5. **Test coverage**: the 5-case suite hits Some/None arms × env/static-literal payload × inline/two-step/statement-form. The fix would also clear the bookkeeper residual 32B leak end-to-end.

## What this does NOT change

- LLVM codegen of `VariantGetField` / `VariantGetFieldAddr`: untouched.
- `_classify_payload_extract_transfer` (copy-semantic / copy-bitcopy / move): untouched.
- string_arc.py refcount tracking: untouched.
- Ownership ledger / drop policy: untouched.
- Any non-match codepath: untouched.

The fix is the smallest possible: a single extra `OR` clause in one materialization gate. All other infrastructure that needed to be present was already in place.

## Files

- `lang/driftc/stage2/hir_to_mir.py:1735-1755` — the fix.
- `lang/language_runtime/posix/thread_runtime.c` — trace strip (11 lines deleted across 6 sites).
- `lang/tests/memcheck/test_inline_match_rvalue_string_payload_leak.py` — new, regression suite.
- `work/bookkeeper-shutdown-hang/repro-bare-leak/` (PushCoin) — original team-side repro this fix unblocks.
