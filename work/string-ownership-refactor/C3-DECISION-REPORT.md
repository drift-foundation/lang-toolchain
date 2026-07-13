# Slice 2 Part 1 — C3 decision checkpoint (report-only)

Status: INVESTIGATION REPORT — no implementation. Stops for the user's arm
selection per CLEANUP-EXECUTION-PLAN.md Slice 2. All evidence gathered against
the recorded Slice 1 reference baseline (tool v1.4.0, manifest
`8dceddd5…`, `c3_moveout_not_owned = 19,504`) on the merged
0.33.81/ABI 21 tree. Investigation artifacts (MIR spies, valgrind probes)
lived in the session scratchpad; nothing in-tree changed.

---

## 0. Headline: the plan's premise was wrong — C3 is FIVE populations, not one

The plan assumed C3 ≈ the flag-guarded `*_cleanup_drop_<local>` shape and
projected a "pure reclassification" of the whole counter. The baseline's
detail records (19,504 — exactly equal to the counter, so the analysis is
complete, not sampled; no fn hit the 50/class detail cap) decompose into:

| pop. | events | sites | raw_state | shape |
|---|---|---|---|---|
| A | 8,316 | 6 (all stdlib) | maybe_uninit | GUARDED cleanup drops in `*_cleanup_drop_<L>` blocks (the plan's shape) |
| B | 8,384 | ~70 (9 stdlib carry 8,316) | maybe_uninit | UNGUARDED inline cleanup drops of zero-tag-drop-safe variants at PATH_DEPENDENT points |
| C | 1,852 | 2 stdlib + 4 fixture events | uninit | cleanup moves inside CFG-UNREACHABLE (dead) catch blocks |
| D | 945 | 1 stdlib + ~7 fixture | tombstoned | zero-init-as-empty-value immediately moved (`ZeroValue→StoreLocal→MoveOut`) |
| E | 7 | 5 fixture sites | moved_out | re-moves of already-moved locals/binders — NOT understood, must stay divergent |

Sum = 19,504 exactly. 18 stdlib sites carry 19,416 of the volume (each
stdlib fn recompiles per fixture, ×924); the true source-site universe is 91.
The plan's stale count (11,441) was a smaller corpus generation of the same
populations.

Consequence: **the model-vs-permanent-allowlist decision as framed only
covers population A (43%).** B–E need their own classification decisions
either way. Each is characterized below with verified root cause.

## 1. Population A — flag-guarded cleanup drops (the original question)

Shape (verified in post-cleanup MIR of `std.json::_parse_array` etc.):
`cleanup_authoring` splits the block at a CleanupHook when the local's
verdict is PATH_DEPENDENT, the type is not zero-tag-drop-safe, and the local
is flag-managed (`cleanup_authoring.py:365-369`):

```
current:   … LoadLocal(t, __drop_flag_L) ; IfTerminator(t ? drop_blk : post_blk)
drop_blk:  MoveOut(tmp, L) ; DropValue(tmp) ; __drop_flag_L ← false ; Goto post_blk
```

The runtime flag is set exactly on the owning paths (`drop_flags.py` step 4:
set after StoreLocal, cleared after MoveOut), so `drop_blk`'s MoveOut only
executes when L is initialized. The flag-blind lattice joins the paths to
MAYBE_UNINIT and C3 (which asks "is L LIVE?") fires. The 6 sites: `items` /
`occurrences` / `vspans` (std.json parsers, Array/Map types), `inline_value`
×3 hooks (std.cli::ArgParser::parse).

### Can the event model represent this? — YES (sketch)

Conditional ownership = **edge-refined dataflow keyed on drop-flag
branches**, not a new lattice element:

- `build_ledger` currently propagates one `out_state` to every successor.
  Extend the successor propagation with an *edge transfer*: when a block's
  terminator is `IfTerminator(cond=t)` and `t` traces (within the block) to
  `LoadLocal(t, F)` where `F = _drop_flag_for_local[L]`, propagate
  `L → LIVE` on the then-edge and `L → MOVED_OUT` on the else-edge, leaving
  every other local untouched. The `func._drop_flag_for_local` map already
  exists (attached by `insert_drop_flags`); flag loads are always
  block-local at the split (authored by `cleanup_authoring` itself).
- Inside `drop_blk`, `state_pre(L)` becomes LIVE → the 8,316 events land in
  the existing `c3_moveout_owned` agree-class. No new event kinds needed.
- Join rules unchanged; the refinement happens before the join.

Complexity: moderate — ~100–150 lines in `ownership_ledger.py`
(per-edge out-states + flag-load tracing) plus unit pins. The mechanics are
not the problem. The problem is:

### The model arm's REAL cost: shared-authority emission feedback

The ledger is not just the audit's oracle — it is emission authority for
`drop_flags` (criteria), `cleanup_authoring` (drop decisions), and
`string_arc` (release elision at MUST_NOT_DROP, site-4 drop-before-
overwrite, variant zero-tag widening). Refinement makes downstream states
*more precise*: e.g. at `cleanup_post` blocks the join of
{drop-edge: MOVED_OUT, else-edge: MOVED_OUT} collapses today's MAYBE_UNINIT
to MOVED_OUT, and release-elision / cleanup decisions at points dominated by
the split would newly fire. Those changes are *correct and beneficial*
(fewer dead releases), but they mean the Part 2A acceptance "every other
counter byte-identical" is NOT achievable with a genuinely shared refined
ledger. The alternatives are both bad for THIS slice:
  - audit-scoped refinement (two ledger truths) — directly against the
    single-authority direction of the whole campaign;
  - accepting unpredicted emission deltas inside a bookkeeping slice —
    widens the blast radius (memcheck gates, cert) far beyond the slice's
    stated intent.

## 2. Population B — unguarded zero-safe cleanup drops (NOT in the plan)

Same CleanupHook classifier, different arm (`cleanup_authoring.py:366-367`):
PATH_DEPENDENT + `variant_zero_tag_drop_safe(ty)` → UNGUARDED inline
`MoveOut+DropValue` in the ORIGINAL block (hence the `if_thenN`/`if_joinN`/
`match_joinN` anchors that looked like user moves in the aggregate). Runtime
safety argument is the compiler's own: on non-owning paths the storage holds
zeroed bytes (the MoveOut expansion zero-stores; match scrutinee moves zero;
`ZeroValue` stores are tombstones) and a zero-tag variant drop is a no-op.
Verified in MIR: every one of these is a `__cleanup_t*` temp — compiler-
authored cleanup, zero user moves in this population. Dominant sites:
`child_sp` (Optional<_SpanTree>, 8 hooks in the std.json parsers, 7,392),
`cr` (Result close-status in `std.fs::read_to_bytes`, 924), plus ~68 events
across ~60 fixture-local Optional/Result locals.

C3 asks the wrong question here. The cleanup MoveOut does not claim the
local is owned; it claims *moving-then-dropping is safe*, and for these the
lattice itself already proves it: every non-LIVE component of the join is a
zeroed state. The principled fix is a **reporter-side comparison extension**
(no lattice change, no emission impact): a MoveOut whose subject's pre-state
is MOVED_OUT / TOMBSTONED / MAYBE_UNINIT *and* whose type is
zero-safe-droppable classifies as a new agree-class (`c3_moveout_zero_safe`)
instead of a divergence.

## 3. Population C — moves in dead catch blocks

`std.json::JsonNode::get` / `JsonObject::get` wrap a **nothrow**
Optional-returning `fields.get(key)` in `try … catch`. The lowering still
fabricates dispatch/catch blocks, but with no throwing call inside the
attempt there is no `call_err → try_dispatch` edge — the catch machinery is
statically dead. Verified: `tryexpr_dispatch` has `preds=[]` in the MIR CFG;
the ledger (correctly) never reaches it; `state_pre` falls back to UNINIT
and C3 fires on the catch's `__try_err` cleanup move.

Two important side-results (both probe-verified, scratchpad valgrind runs,
0 errors / 0 leaks):
- **No live-leak concern behind this.** For genuinely-throwing calls the
  lowering emits explicit `call_err → try_dispatch` MIR edges; reachable
  catch blocks get correct ledger state, and a heap-string-in-catch-arm
  probe (both expression-arm and return-inside-catch shapes) is clean under
  valgrind. My initial alarm that release-elision might leak in catch
  blocks is retired.
- **Stdlib hygiene flag (separate thread, not this campaign):** the two
  dead `try <nothrow-expr> catch` sites in std.json deserve a cleanup
  and/or a future "useless try over nothrow expression" diagnostic.

Classification fix: reporter-side — an event whose `pre_point` block has no
ledger state (`block_in` absent ⇔ CFG-unreachable) classifies as a distinct
observational class (`unreachable_block_event`), never C3. Trivial (~10
lines + pin).

## 4. Population D — zero-init-then-move

`std.log::log_context`: `ZeroValue → StoreLocal(attrs) → MoveOut(attrs)` —
empty-container literals lower as zero-init, which the ledger deliberately
records as TOMBSTONED (drop-safe bytes, suppresses drop-before-overwrite),
then the value is immediately moved as a legitimate empty value. Same
zero-safe-move argument as B, same reporter rule absorbs it (the
TOMBSTONED pre-state case). 945 events (924 stdlib + ~21 fixture
`__maplit.tN`).

## 5. Population E — 7 events that must STAY divergent

- `match_stmt_nested_match_last_stmt`: local `r` moved at three
  `match_arm_N1` entries with pre-state moved_out;
- `std_io_file_builder_chunked_large` / `std_io_stdin_read_line_eof_helper`
  / `std_io_pipe_reverse_stdout`: `__match_binder_*_e` re-moved at
  `tern_else`/`if_join` entry;
- `catch_binder_visible_in_arm`: catch binder `e` moved at `try_catch_0[9]`
  with pre-state moved_out.

All are in passing fixtures, so nothing is actively crashing, but I did not
root-cause them. Hypotheses (unverified): zero-safe cleanup re-moves that
the B rule would legitimately absorb after per-site verification; ledger
build-time skew vs cleanup_authoring's decision ledger; or a genuine
checker/lowering gap (re-move of a moved local — adjacent to the resolved
0.33.39 family). Part 2 must triage these 5 sites individually; blanket
normalization is exactly what the retired-C4 precedent forbids.

## 6. Expected verdict movement (supersedes the plan's 11,441 claim)

Target end-state for `c3_moveout_not_owned`: **19,504 → 7** (population E
residue, pending its triage), with exact-balance reappearance:

| population | events | new class |
|---|---|---|
| A | 8,316 | `c3_moveout_flag_guarded` (2B structural) or `c3_moveout_owned` (2A refinement) |
| B | 8,384 | `c3_moveout_zero_safe` |
| C | 1,852 | `unreachable_block_event` |
| D | 945 | `c3_moveout_zero_safe` |
| E | 7 | stays `c3_moveout_not_owned` until triaged |

Under 2B + reporter rules, every other counter stays byte-identical (all
changes are reporter-side classification). Under 2A (shared refined
ledger), site_class released/skip counters WILL move (see §1) — the corpus
acceptance would need to predict those deltas explicitly.

## 7. Recommendation

Decide per population, not once:

1. **A → 2B-style STRUCTURAL recognition now** (my recommendation): the
   reporter recognizes exactly the authored shape — MoveOut at index 0 of a
   `*_cleanup_drop_<L>` block whose sole predecessor terminates in
   `IfTerminator` whose cond traces to `LoadLocal(__drop_flag_L)` — as
   `c3_moveout_flag_guarded`; any C3-shaped MoveOut outside the shape stays
   divergent (retired-C4 discipline: structural match, never a count). No
   emission risk, loud on drift. The reporter needs the MirFunc at
   `finalize` time (string_arc has it in hand) — small plumbing.
   **Record the 2A flag-refinement as a future emission-improvement slice
   in its own right** — its payoff is real (PATH_DEPENDENT release elision,
   more precise site-4) but it belongs behind its own corpus acceptance
   with predicted deltas, not inside audit bookkeeping.
2. **B + D → zero-safe-move agree-class** (reporter comparison fix; this is
   a model correction of the C3 question itself, not an allowlist).
3. **C → unreachable-block filter** (+ separate stdlib-hygiene note for the
   two dead try/catch sites).
4. **E → individual triage first**; keep divergent until then.

If you prefer the full 2A model arm for A despite §1, the sketch above is
implementable; I would then split it: reporter slices (B/C/D) first with
byte-identical acceptance, refinement slice after, with its own predicted-
delta corpus acceptance and memcheck gate.

## 8. STOP

Awaiting arm selection (per population) before any Part 2 implementation.

---

## OUTCOME (2026-07-13): hybrid arm selected and implemented — acceptance exact

User selection: A → structural recognition now (no edge-refined ledger in this
slice); B+D → zero-safe agree-class; C → unreachable filter; E → stays divergent
pending triage; flag-refined modeling recorded as a future emission slice
(CLEANUP-EXECUTION-PLAN.md addendum).

Implemented reporter-side only (see PROGRESS 2026-07-13). One naming change vs
§6's placeholder: the population-C class landed as `c3_moveout_unreachable_block`
(scoped to the C3 comparison) rather than `unreachable_block_event`.

Corpus acceptance vs the pre-change reference (0.33.82 merge first confirmed
corpus-neutral: all 14 counters +0): movement EXACTLY as §6 predicted —
19,504 → 7 residual; +8,316 flag_guarded; +9,329 zero_safe; +1,852 unreachable;
all other counters byte-identical; hard gates zero. The residual 7 verified
identical to §5's population-E list. No new population surfaced.
