# Inventory — ledger cache safety slice

Per-file enumeration of:
- **Mutation sites** that must get a `mark_ledger_dirty(...)` call.
- **Consumer sites** that must route through `require_fresh_ledger(...)`.

Generated 2026-05-16 against the current toolchain (post-0.31.90).

## Mutation sites

### `lang/driftc/stage2/drop_flags.py` (11 sites)

| Line | Pattern | Notes |
|---|---|---|
| 204 | `entry.instructions = init_instrs + entry.instructions` | entry-block prepend of flag inits |
| 226 | `blk.instructions = new_instrs` | per-block instruction replacement (flag set/clear inserts) |
| 510 | `cur_post.terminator = original_term` | terminator rewrite when splitting block |
| 511 | `func.blocks[cur_post.name] = cur_post` | block replacement |
| 521 | `drop_block.instructions.append(M.MoveOut(...))` | new drop-block contents |
| 522 | `drop_block.instructions.append(M.DropValue(...))` | new drop-block contents |
| 523 | `drop_block.terminator = M.Goto(...)` | new drop-block terminator |
| 524 | `func.blocks[drop_block.name] = drop_block` | new block insert |
| 532 | `func.blocks[guard_block.name] = guard_block` | new guard-block insert |
| 534 | `guard_block.instructions.append(M.LoadLocal(...))` | guard-block contents |
| 535 | `guard_block.terminator = M.IfTerminator(...)` | guard-block terminator |

drop_flags builds its own local ledger via `build_ledger(func, drop_policy=drop_policy)` at line 90 and does NOT read `func._ownership_ledger`.  No `require_fresh_ledger` calls needed here.  Reason convention: `"drop_flags.<action>"` (e.g. `"drop_flags.entry_prepend"`, `"drop_flags.insert_flag_store"`, `"drop_flags.insert_guard_block"`).

### `lang/driftc/stage2/cleanup_authoring.py` (12 sites)

| Line | Pattern | Notes |
|---|---|---|
| 409 | `func.blocks[nb.name] = nb` | new split-block insert |
| 477 | `blk.instructions = new_instrs` | per-block instruction replacement (hook removal + drop emission) |
| 523 | `drop_blk.instructions.append(M.MoveOut(...))` | guarded drop-block contents |
| 524 | `drop_blk.instructions.append(M.DropValue(...))` | guarded drop-block contents |
| 527 | `drop_blk.instructions.append(M.ConstBool(...))` | flag-clear const |
| 528 | `drop_blk.instructions.append(M.StoreLocal(...))` | flag-clear store |
| 531 | `drop_blk.terminator = M.Goto(...)` | drop-block goto |
| 533 | `current_blk.instructions = list(current_instrs)` | current-block tail replacement |
| 534 | `current_blk.terminator = M.IfTerminator(...)` | current-block terminator (flag-check branch) |
| 550 | `current_blk.instructions = current_instrs` | unguarded current-block instruction replacement |
| 551 | `current_blk.terminator = original_term` | current-block terminator (unguarded path) |
| 555 | `func.blocks[nb.name] = nb` | new block insert (edge-elaboration) |
| 723 | `edge_blk.terminator = M.Goto(...)` | edge-block terminator |

Reason convention: `"cleanup_authoring.<action>"` (e.g. `"cleanup_authoring.emit_unguarded_drop"`, `"cleanup_authoring.emit_guarded_drop"`, `"cleanup_authoring.edge_elaborate"`).

### `lang/driftc/stage2/match_cleanup_authoring.py` (1 site)

| Line | Pattern | Notes |
|---|---|---|
| 233 | `blk.instructions = new_instrs` | per-block instruction replacement |

Reason: `"match_cleanup_authoring.emit_arm_drops"`.

### `lang/driftc/stage2/string_arc.py` (2 sites)

| Line | Pattern | Notes |
|---|---|---|
| 1684 | `block.terminator = new_term` | terminator rewrite (e.g. return-value retain wrapping) |
| 1690 | `block.instructions = new_instrs` | per-block instruction replacement (retain/release inserts + MoveOut expansion) |

Reason: `"string_arc.rewrite_block"` (single site, single name — the per-block work loop).

## Consumer sites (reads of `func._ownership_ledger`)

### Attached-ledger consumers (must route through `require_fresh_ledger`)

| File:Line | Read | Migration |
|---|---|---|
| `cleanup_authoring.py:237` | `ledger: Optional[LiveStateMap] = getattr(func, "_ownership_ledger", None)` | `ledger = require_fresh_ledger(func, "cleanup_authoring")` — the `is None` early-return becomes a precondition: if no ledger attached, the pass cannot run.  *But* the existing code permits `ledger is None` and early-returns.  Decision: keep the soft-form `maybe_fresh_ledger(func, "cleanup_authoring")` here — pre-existing optional-pass semantics.  Inline justification comment required. |
| `cleanup_authoring.py:325-326` | `ledger = _build_ledger(...); setattr(func, "_ownership_ledger", ledger)` | Migrate to `ledger = build_and_attach_ledger(func, drop_policy=..., reason="cleanup_authoring.in_pass_rebuild")` |
| `match_cleanup_authoring.py:78` | `ledger: Optional[LiveStateMap] = getattr(func, "_ownership_ledger", None)` | Same pattern as cleanup_authoring:237 — same optional-pass semantics, same `maybe_fresh_ledger` migration with justification. |
| `string_arc.py:68` | `_ledger = getattr(func, "_ownership_ledger", None)` | Same pattern.  `maybe_fresh_ledger(func, "string_arc")`. |
| `driftc.py:7146` | `ledger = getattr(func, "_ownership_ledger")` (observe/reporter debug block) | `ledger = require_fresh_ledger(func, "driftc.observe_reporter")` — closes K's debug-mode blind spot.  This one is hard-assert because it always runs inside the debug block; if the bit is dirty here, real damage has happened. |

### Driver-side ledger rebuilds (migrate to `build_and_attach_ledger`)

| File:Line | Current | Migration |
|---|---|---|
| `driftc.py:7032` | `ledger = build_ledger(func, drop_policy=...); setattr(func, "_ownership_ledger", ledger)` | `ledger = build_and_attach_ledger(func, drop_policy=..., reason="driftc.initial_build")` |
| `driftc.py:7056` | same | `reason="driftc.rebuild_after_drop_flags"` |
| `driftc.py:7101` | same | `reason="driftc.rebuild_after_cleanup_authoring"` |
| `driftc.py:7129` | same | `reason="driftc.rebuild_after_match_cleanup_authoring"` |

(Reason strings are illustrative; resolve exact ordering at edit time by reading the surrounding context.)

## Totals

- **26 mutation sites** across 4 files → 26 `mark_ledger_dirty` calls.
- **6 consumer sites** (4 attached-ledger + 1 in-pass rebuild + 1 observe) → 4 `maybe_fresh_ledger` (preserving optional-pass semantics) + 1 `build_and_attach_ledger` (in-pass) + 1 `require_fresh_ledger` (observe).
- **4 driver-side rebuilds** → 4 `build_and_attach_ledger`.

Effort estimate from plan stands: ~280 LOC including helper module + audit test.

## Discovered nuance: soft-form (`maybe_fresh_ledger`) usage

The three pass-entry reads (`cleanup_authoring.py:237`, `match_cleanup_authoring.py:78`, `string_arc.py:68`) all currently early-return when no ledger is attached.  This is intentional optional-pass semantics — the pass may run in modes where no ledger exists.  These are exactly the cases the plan's `maybe_fresh_ledger` helper is for; we'll use it with an inline justification at each call site.

`require_fresh_ledger` (hard-assert) is used only at `driftc.py:7146` (observe/reporter, no optional semantics).

## Discovered nuance: test-side ledger attaches

The dirty bit lives on the `MirFunc` and is only cleared by `build_and_attach_ledger` / `attach_ledger`.  A raw `setattr(func, "_ownership_ledger", ledger)` replaces the ledger object but leaves `_ledger_dirty_reason` set — the next consumer's `maybe_fresh_ledger` / `require_fresh_ledger` then fires on a ledger that is, in practice, fresh.  Surfaced by `test_move_from_ref_string_arc_contract.py`'s `_run_authoring_then_string_arc` helper, which chained `match_cleanup_authoring` → raw rebuild → `string_arc`.

Rule for both production and tests:

- **Production driver rebuilds** use `build_and_attach_ledger`.
- **Test helpers that chain two or more ownership/cleanup/string passes** on the same `MirFunc` must also use `build_and_attach_ledger` or `attach_ledger`, not raw `setattr`.
- **Single-pass tests** may keep raw attach only if they never mutate before consuming, but prefer the helper for new tests.

The other ten test files identified by `grep -rln "setattr.*_ownership_ledger" lang/tests/` (`test_has_drop_cache_clear_before_mir_lowering.py`, `test_hir_to_mir_path_insensitive_moved_locals.py`, `test_match_cleanup_full_candidate_set.py`, `test_match_cleanup_authoring.py`, `test_cleanup_authoring.py`, `test_drop_before_overwrite_swap.py`, `test_scope_drop_swap.py`, `test_string_arc_return_swap.py`, `test_cleanup_authoring_flag_guarded.py`, `test_hir_to_mir_match_copy_payload_drop_once.py`) are single-pass against a freshly-built ledger and do not need migration today; new chained tests must use the helper.
