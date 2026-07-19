# string-arc-endgame-array-sweep — report-only checkpoint

Status: STOPPED for review. NO implementation; no GO given or assumed.
Bundled branch per maintainer: Sub-slice A (guard deletion, every
counter +0) + Sub-slice B (bijective migration of all 4,620 residual
Array drops, `_drop_all_arrays` deletion). Arm M provisionally
preferred (maintainer, 2026-07-18): avoids runtime flags, preserves
unconditional null-safe-drop semantics. Implementation acceptance =
compiler 0.33.85, ABI 21, then certification + release; Sub-slice A
carries its own independent every-counter-+0 contract.

## 1. Sub-slice A — consistency-guard deletion

Inventory: the ConstString re-add guard, the StringFrom*/Concat re-add
guard, and (decision item) the per-block `owned_values -=
recognized_released` subtraction — all marked consistency-only since
the tripwire-deletion slice.

Safety argument: the T4 output-equivalence proof, verbatim. A
re-owned recognized temp's state may propagate (AssignSSA copies
owned membership) and affect branch selection
(`_can_move_owned_once` reads it), but every affected branch is
output-equivalent — `_ensure_owned` is identity, the store paths are
unconditional, and `_note_use` only changes bookkeeping — so no
branch can author another instruction or release. The guards only
ever differentiate RECOGNIZED dests, so guard deletion perturbs
exactly the state the proof covers.

**A1 APPROVED (maintainer, 2026-07-18): both guards plus the
asymmetric subtraction delete together**, with the independent +0
gate below. (The rejected A2 fallback — guards only — would have
left a consistency mechanism suppressing prepass-only producers but
not re-add producers.)

Acceptance (independent of B): EVERY counter +0 vs the standing
reference (`build/tmp/cleanup-tripdel` table), universe identical,
gates zero, batteries + standalone memcheck unchanged. Any counter
movement = stop.

## 2. Sub-slice B — the residual population, measured

Corpus instrument (2026-07-18, `build/tmp/bij-measure`, exit 0,
universe identical 924/344/49; three scratch edits joining string_arc
sweep notes against cleanup_authoring's PATH_DEPENDENT array
`_KIND_SKIP` records via a per-fn consumable pool; ALL instrumentation
REVERTED byte-identically after — cmp-verified against pristine
copies, zero `SCRATCH-BIJ` refs in tree, reporter battery 51/51 on
the restored tree).

### 2.1 Bijective account — every drop, no remainder

| join class | count | identity |
|---|---|---|
| arraydrop_bij_matched_exact | 0 | — |
| arraydrop_bij_matched_fnlocal | 924 | `std.fs::read_to_bytes` / `__match_binder_4_bytes` ×1 per fixture |
| arraydrop_bij_swept_unmatched | 3,696 | `std.json::_parse_array` / `items` ×2 + `std.json::_parse_object_throwing` / `occurrences` ×2 per fixture |
| arraydrop_bij_skiprec_orphan | 0 | — |
| **total** | **4,620** | = 5 drops × 924 compiled fixtures — the ENTIRE population is THREE stdlib functions |

Per-fixture single-unit drill-down (algo_binary_search_basic probe,
`DRIFT_BIJ_DETAIL=1`, per-fn JSONL):

- `read_to_bytes`: 1 sweep note at block `match_join`; 1 PD-skip
  record at block `match_join2` (fn/local match, exact-block miss);
  fn also shows `c3_moveout_zero_safe: 1` (binder move-out,
  zero-backed).
- `_parse_array`: 2 sweep notes at `if_join3_cleanup_post_items` /
  `if_join9_cleanup_post_items`; ZERO skip records; fn shows
  `c3_moveout_flag_guarded: 2`.
- `_parse_object_throwing`: 2 sweep notes at
  `if_join{3,13}_cleanup_post_occurrences`; ZERO skip records; fn
  shows `c3_moveout_flag_guarded: 4`.

### 2.2 Explanations (required for every fallback/orphan)

**Unmatched class (3,696 — the json parsers).** These locals never
produce `_KIND_SKIP` records because cleanup_authoring classified
them `_KIND_GUARDED` and AUTHORED complete flag-guarded cleanup (the
co-located `c3_moveout_flag_guarded` counts). The sweep notes sit in
`{blk}_cleanup_post_{local}` blocks — the continuation blocks the
flag-guarded emission itself creates (`cleanup_authoring.py:563`).
The swept drop is therefore the RESIDUAL after a complete guarded
cleanup: on the flag-taken path the storage was zero-backed by the
authored MoveOut expansion; on the flag-not-taken path the storage
holds the entry-block zero-init (§3). Under the documented flag
invariant ("flag bit ≡ currently owns destructible storage",
cleanup_authoring module doc), the storage is ZERO on EVERY path at
the swept point — today's sweep emission there is a provable no-op.

**Fn/local-fallback class (924 — read_to_bytes) — RESOLVED
STATICALLY (K, 2026-07-18, stdlib/std/fs/fs.drift:285).** A genuine
`_KIND_SKIP` (PD, not flag-managed) exists for the local at the
`match_join2` CleanupHook. The close match resolves the PD join:
close SUCCESS moves `bytes` into `Ok` (zero-backed); close FAILURE
returns `Err(ce)` WITHOUT consuming `bytes` — **the Array is
genuinely LIVE on the close-error arm.** This class is NOT a no-op:
today the sweep's unconditional drop at `match_join` is what frees
the live array on the error path. B-M must therefore AUTHOR exactly
924 unguarded drops at the EXISTING `match_join2` CleanupHook (not
at the swept `match_join` exit); the authored MoveOut zero-backs the
storage on every path, making the later sweep dead.

**Orphans: zero** — no cleanup_authoring PD-array skip record went
unswept. Exact-block matches: zero — no hook shares a block with a
sweep note anywhere in the corpus.

### 2.3 What the bijection changes about Arm M

Arm M as originally framed ("flip the `_KIND_SKIP` branch to
unguarded authoring") covers ONLY the 924 class — and even there the
skip's hook is at a different exit than the swept Return. The 3,696
class has no skip to flip; its guarded cleanup is already complete
and the residual drop is dead. The refined Arm M is therefore
TWO-PART:

- **B-U (3,696, json parsers): retire as proven no-ops.** No new
  emission anywhere. Justification chain: guarded-cleanup
  completeness (flag invariant) + entry zero-init (§3) + zero-storage
  drop no-op (§3). Bijective account: 3,696 → 0, each justified by
  the same three-line proof, itemized per (fn, local, block) — 4
  rows total, ×924. SOUND per maintainer review.
- **B-M (924, read_to_bytes): author exactly 924 unguarded drops at
  the EXISTING `match_join2` CleanupHook** via the extracted
  predicate (§4) — the close-error arm holds a genuinely LIVE array
  (§2.2, resolved statically at fs.drift:285), so this class is a
  REAL drop migration, not a no-op retirement. The authored
  MoveOut+DropValue zero-backs the storage on all paths; the sweep's
  drop at `match_join` becomes dead and deletes with the sweep.

### 2.4 Arm F (flag-modeling), reframed honestly

Arm F would extend drop_flags to the 924 remaining sites ONLY — the
3,696 are already flag-managed with complete guarded cleanup, so F
adds nothing there (it does NOT double-guard them; there is nothing
left for it to guard). For the 924, F would add runtime flag state
(flag local, init/set/clear stores, a branch at the drop point) to
distinguish the two close arms — state the unguarded null-safe
authoring makes unnecessary: the success arm's storage is already
zero-backed by the `move` into Ok, so one unconditional authored
drop is correct on both arms with no flag. Rejected for the runtime
state and MIR growth against zero benefit, consistent with the
strings precedent (PATH_DEPENDENT keeps unconditional null-safe
handling for zero-storage-drop-safe types).

## 3. Zero-safety proof (the actual chain)

Array allocas are NOT automatically zeroed. The chain is:

1. **Entry initialization** — string_arc's entry-block init loop
   authors `ZeroValue + StoreLocal` for EVERY non-param array local
   (string_arc.py, the array loop immediately following the
   string-local init at ~1578). This is the sole reason
   never-initialized array storage holds zero bytes.
2. **Zero-backed consumes** — the MoveOut expansion zero-stores the
   local after every move (authored guarded cleanup included).
3. **Zero-storage drop is a no-op** — `_lower_array_drop` /
   `_emit_drop_value`'s ARRAY arm extract `len` and `data` from the
   header: the element-drop helper iterates `len = 0` times and
   `drift_free_array(NULL)` returns without effect
   (`array_runtime.c:75`; `free(NULL)` is defined no-op).

**Load-bearing consequence:** the entry init is a string_arc
responsibility that SURVIVES sweep retirement. It joins the endgame
inventory as its own migration item — deleting string_arc without
re-homing it turns every uninit-path array drop (and B-U's no-op
proof) into UB. Recorded as endgame-inventory line, out of this
slice.

## 4. Predicate extraction + migration rule (maintainer, verbatim)

New `zero_storage_drop_safe(ty, type_table)` homed in
`drop_policy_compute.py`. True for VARIANT (tag-0 no-op dispatch,
absorbing today's variant_zero_tag_drop_safe semantics) and ARRAY
(zeroed header no-op, §3); everything else fails closed.

**Migration rule: `zero_storage_drop_safe` must replace EVERY
production consumer, including drop_flags. The legacy
`variant_zero_tag_drop_safe` wrapper may remain temporarily for
compatibility/tests, but no production decision may continue through
the misleading variant-only name.** Production consumers, with
today's import lines:

- `cleanup_authoring.py:121` (import) → PD resolution branch.
- `drop_flags.py:276/308` (imports FROM string_arc; gates candidate
  admission) — the widened predicate also excludes arrays from flag
  management by the same rule that exempts zero-tag variants,
  aligning drop_flags with Arm M's contract from both sides.
- `ownership_ledger_reporter.py` C3 ladder + string_arc.py:2832's
  production `zero_safe_ty` lambda.
- string_arc's DIRECT site-3 call (~string_arc.py:2691, the Phase 4
  site-3 sub-step 3 `initialized_at_return` variant zero-tag
  widening): migrates with the rest. Semantics preserved — arrays
  are excluded from `destructible_locals` and cannot reach this
  site, but the rule stands: no production decision through the
  variant-only name.
- NOT a consumer: `match_cleanup_authoring.py` — its single
  reference (line 40) is a docstring mention, no import or call;
  wording touch only, no migration.

`variant_zero_tag_drop_safe` stays as a delegating wrapper for
tests/compat only and dies with string_arc.

## 5. Destruction order

Today: MUST_DROP arrays drop in cleanup_authoring's
reverse-declaration RAII order at hooks; PD arrays drop in the
sweep's `sorted(name)` order at Return, after all hook drops. Array
element destructors are observable, so this split ordering is
user-visible in principle.

- **B-U changes NO observable order**: the retired drops are proven
  no-ops — nothing runs today on any path, nothing runs after.
- **B-M is a REAL RAII-order change for 924 live drops**: on the
  close-error arm, the live `bytes` array today drops at the
  `match_join` sweep (after every hook-authored drop, in the sweep's
  sorted-name order); after B-M it drops at the `match_join2`
  CleanupHook — earlier, in reverse-declaration RAII order,
  interleaved with that hook's other candidates. This normalizes PD
  arrays onto the same order contract live arrays already follow;
  recorded as a behavior note for the 0.33.85 history entry.
  (Element type here is Byte — no element destructors — so the
  fs.drift instance itself has no observable destructor reordering;
  the ordering carrier pin proves the general contract.)
- The ordering carrier pin (§6, maintainer spec) proves the final
  order either way.

## 6. Required pins (maintainer list, verbatim + additions)

1. Predicate contract: Variant and Array → True; unrelated types
   fail closed.
2. PATH_DEPENDENT Array cleanup chooses Arm M's unguarded authoring
   and is NOT flag-managed.
3. A paired MAYBE_UNINIT Array MoveOut classifies
   `c3_moveout_zero_safe`.
4. An unpaired Array MoveOut remains divergent / hard-gated.
5. The ordering carrier exercises BOTH condition outcomes and places
   the PD Array correctly among live Arrays and the interleaved
   destructible.

Additions from the measurement:

6. B-U retirement pin: the `_cleanup_post_` residual shape (guarded
   cleanup + Return) compiles with ZERO post-cleanup array drops in
   output MIR, valgrind-clean on both flag outcomes (heap
   Array<String> carrier).
7. B-M authored-drop pin (read_to_bytes shape): PD array candidate
   at the `match_join2`-style hook → exactly one authored unguarded
   MoveOut+DropValue at the HOOK position, no sweep drop at the
   Return; BOTH match outcomes valgrind-clean (moved-into-Ok arm =
   authored drop is a no-op over zero-backed storage; error arm =
   the authored drop frees the live array — the leak-direction
   guard, since pre-B-M that free came from the sweep).
8. Memcheck: `test_array_release_elision.py` rows stay green; NEW
   row for the json-parser shape (conditionally-consumed array
   through a throwing loop, both exits) and the read_to_bytes shape
   (both close arms).

## 7. Predicted acceptance (vs the standing reference table) — DEFINITIVE

Refined Arm M = B-U retirement (3,696 proven no-ops deleted) + B-M
authoring (924 unguarded drops at the `match_join2` hook):

| counter | delta |
|---|---|
| events | **+924** (the authored MoveOut expansions; sweep notes were never events) |
| site_class:moveout_expansion | **+924** (all `moveout_feeds_drop=True`) |
| c3_moveout_zero_safe | **+924** (via the extracted predicate — without the array leg, +924 `c3_moveout_not_owned` = HARD-GATE FAILURE) |
| site_class:scope_exit_arraydrop | 4,620 → 0 (key vanishes) |
| arraydrop_state:maybe_uninit | 4,620 → 0 (key vanishes) |
| arraydrop_verdict:path_dependent | 4,620 → 0 (key vanishes) |
| every other counter | +0 |

Universe identical; all hard gates zero; memcheck standalone in gate
from the start (authority-work gate rule).

`_drop_all_arrays` deletion safety: single call site (the Return
branch); `_drop_array_local` SURVIVES via its drop-before-overwrite
caller (out of scope); entry-init loop NOT deleted (§3); bare-caller
test inventory = the reporter battery's arraydrop pins, reworked in
this slice (they pin the sweep they retire).

## 8. Version & sequencing

Sub-slice A first (independent +0 acceptance, no bump), then B.
B is behavior-changing → compiler 0.33.85, ABI 21, then
certification + release, with a history.md entry covering the sweep
retirement, the predicate extraction/migration rule, and the REAL
RAII-order change for the 924 B-M sites: the close-error-arm live
drop moves from the Return sweep's sorted-name position to the
`match_join2` CleanupHook's reverse-declaration RAII position,
normalizing PD arrays onto the live-array order contract (§5).

## 9. Stop conditions

- The authored B-M drop at `match_join2` fails to make the
  `match_join` sweep note vanish (dataflow gap between hook and
  Return) → stop and report.
- Any B-U local's all-paths-zero proof fails structurally.
- Any counter outside the predicted table (§7 is exact: ±924/±4,620
  only); any hard gate nonzero.
- The predicate migration cannot be made fail-closed for any
  consumer (esp. drop_flags admission semantics).

## 10. STOP

Report-only. Awaiting review; no implementation GO.
