# Slice B — cleanup-authority migration (R6 + R2/R7): LIVING RECORD

Branch: `string-arc-endgame-cleanup-authority`. RELEASE-BOUNDARY
REFRAME (maintainer 2026-07-20T195625Z): 0.33.87 / ABI 21 is the
CONSOLIDATED endgame candidate (not per-slice) — one version bump,
ONE final certification when `string_arc.py` is deleted. B1 is a
chunk on that line, NOT its own release/cert boundary; commit for
recoverability is fine, no separate B1 certification. Development
gates (focused tests, memcheck, corpus deltas, structural gates)
after each chunk. Next chunk B2 (all remaining R6) requires a
report-only checkpoint (measured premise mismatch). See
STRING-ARC-ENDGAME-RESUME-CHECKPOINT.md §5.3 for the integration
line + definition of done.

Phase log at the bottom; measurement tables filled as they complete.
Continue to implementation directly if bijections close and ordering
is understood — STOP only on a listed condition (§0).

## 0. Stop conditions (from direction)

- any emission lacks a new-author location;
- destruction order changes unexpectedly;
- a self-alias/stake case cannot preserve retain-before-release;
- pass placement would consume a stale ledger;
- any runtime/ABI change is required;
- measured population differs from proposed scope.

## 1. Emission-site survey (string_arc.py, pre-measurement)

### R6 — destructible cleanup (three sub-sites)

- **site-3 (Return boundary)** — `_drop_all_destructibles(new_instrs,
  skip_locals=skip_cleanup_locals, only_locals=initialized_at_return)`
  at each Return (~1831). Per-local `_drop_destructible_local` =
  `LoadLocal + ZeroValue + StoreLocal(zero) + DropValue`. Universe:
  `destructible_locals ∩ initialized_at_return \ skip_cleanup_locals`.
  `initialized_at_return` = assigned_in ∪ store_defs(block) ∪
  store_defs(entry), PLUS the zero-storage widening (~1735-1748:
  PathDependent + `zero_storage_drop_safe` → added to
  initialized_at_return). NO audit note today (site-3 destructible
  drops are unmeasured — flagged in the R6 rev-1 checkpoint).
- **site-4 (drop-before-overwrite)** — StoreLocal into
  `destructible_locals` (~863-957). Pure Tier-1 ledger authority:
  `verdict_at((block,idx), local)` → MUST_DROP emits
  `_drop_destructible_local` + audit note
  SITE_CLASS_DROP_BEFORE_OVERWRITE_SITE4 (corpus 14). PathDependent
  → RuntimeError tripwire. Needs a FRESH post-drop_flags ledger
  aligned to the walked indices (build-timing invariant, pinned).
- **nullsafe-destructible StoreLocal** (~859-862) — unconditional
  `_drop_destructible_local` before the store for
  `nullsafe_destructible_locals`. (Sub-case of the overwrite family;
  no ledger consult — nullsafe types drop-safe on zero.)

### R2 — String overwrite releases (all instruction-local, ~1143-1218)

- **StoreLocal** String local → `_release_local` (LoadLocal old +
  ZeroValue + StoreLocal(zero) + note OVERWRITE_RELEASE +
  StringRelease). Subject = storage local.
- **MoveFromRef** String → `_release_local` (release prior dest
  value; the transfer itself is codegen's).
- **StoreRef** String → LoadRef old + note OVERWRITE_RELEASE +
  StringRelease (release the pointee's prior value).
- **ArrayIndexStore** String elem → ArrayIndexLoad old + note
  OVERWRITE_RELEASE + StringRelease (release the prior element).
- Counter: `overwrite_release` = **233,519** (must stay stable).

### R7 — Array overwrite drop (~855-858)

- StoreLocal into `array_locals` → `_drop_array_local` = LoadLocal +
  ZeroValue + StoreLocal(zero) + ArrayDrop. NO audit note. Sole
  surviving caller of `_drop_array_local` (the Return sweep was
  deleted in B-U).

### Helpers in play

`_release_local` (R2; string_locals only — PRESERVE for R3 scope-exit,
per direction), `_drop_array_local` (R7 sole caller → delete after
migration), `_drop_destructible_local` / `_drop_all_destructibles`
(R6 site-3 + site-4 → delete when BOTH consumers gone).

## 2. Homing analysis (compare correct homes; do NOT force cleanup_authoring)

- **R6 site-3** is a SCOPE-EXIT emission → naturally cleanup_authoring
  under ledger authority AT the Return-block CleanupHook. KEY
  QUESTION the measurement answers: does cleanup_authoring ALREADY
  author these (making site-3 redundant), or does site-3 emit drops
  cleanup_authoring does not reach? Bijection per site-3 emission →
  {existing hook author | new hook candidate | NO author = STOP}.
- **R6 site-4** is INSTRUCTION-LOCAL overwrite cleanup — NOT a
  scope-exit; no CleanupHook there. Candidate homes: (a) a sibling
  overwrite-authoring pass, or (b) keep in string_arc if it cannot
  move without a stale ledger. Already pure ledger authority, so the
  move is about pass placement + a fresh ledger, not logic.
- **R2/R7** are INSTRUCTION-LOCAL overwrite cleanup → the sibling
  overwrite-authoring pass is the natural home (a release/drop-before-
  overwrite pass). Pipeline proof REQUIRED: must run AFTER upstream
  copy stakes are materialized (string_stakes) and with a
  fresh/aligned ledger; must not reopen the self-aliased-store UAF
  window (retain-before-release ordering) or add unaccounted
  ledger rebuilds.

## 3. Measurement tables (Phase M — corpus build/tmp/sliceb-measure,
924 fixtures, exit 0; instrument reverted byte-identically, 3× cmp,
battery 65/65)

### 3.1 Census

| population | count | notes |
|---|---|---|
| cleanup_authoring hook decisions | 1,832,187 | skip 884,357 / unguarded 942,285 / guarded 5,545 |
| R2 overwrite_release | **233,519** | StoreLocal 231,660 / MoveFromRef 926 / StoreRef 925 / ArrayIndexStore 8 — EXACTLY the overwrite_release counter |
| R7 array overwrite drop | 143,008 | all StoreLocal into array_locals |
| R6 site-4 drop-before-overwrite | 14 | all VARIANT/STRUCT user locals |
| R6 site-3 destructible drop | **1,088** | 1,087 ERROR-kind + 1 STRUCT; ALL must_drop, zero-storage-UNSAFE, NON-flag-managed |

### 3.2 R6 site-3 bijection vs cleanup_authoring (THE decisive finding)

Join of every site-3 (fn, local) against cleanup_authoring's hook
decisions for the same (fn, local):

| join class | count | meaning |
|---|---|---|
| **NO_HOOK_AT_ALL** | **1,068** | cleanup_authoring never sees this local — no CleanupHook covers it |
| CA_EMITS_ELSEWHERE | 20 | a ca hook-emit exists for the (fn,local) but at a different program point |
| CA_SKIP_ONLY | 0 | — |

Local-name breakdown: `e` 1,073 (+ `err`/`_e` a few) = the
try/catch/`throws` ERROR-BINDER locals; the rest `__discard.t*`
compiler discard temps for destructible-valued expression statements.
Precise result (matching the join table): 1,068 have NO CleanupHook
at the same program point (nor anywhere for that fn/local), and 20
have a cleanup_authoring hook decision for the (fn,local) only at a
DIFFERENT program point — so for none of the 1,088 does a hook author
the drop at the Return where site-3 emits it. site-3 is their sole
authority at that point today.

### 3.3 site-4 + R2/R7 bijections (these CLOSE cleanly)

- **site-4 (14)**: all user locals (`node`/`root`/`dst`/`r`) whose
  (fn,local) also has a cleanup_authoring hook decision (`ca_emit`
  14/14) — but the drop-before-overwrite fires at the REASSIGNMENT
  point, not the hook. Instruction-local → sibling overwrite pass.
  1:1 author.
- **R2 (233,519)** and **R7 (143,008)**: instruction-local
  release/drop-before-overwrite, one new-author per emission at the
  same instruction in a sibling authoring pass. Bijections close 1:1.

## 4. DISPOSITION — STOP AND REPORT (two stop conditions fired for R6 site-3)

The measure-first protocol caught a scope/premise mismatch. R6 site-3
does NOT close its bijection to cleanup_authoring:

1. **"any emission lacks a new-author location"** — 1,068 of 1,088
   site-3 drops have NO CleanupHook. cleanup_authoring (the proposed
   R6 home) cannot author them; homing them there requires a HIR→MIR
   lowering change to emit hooks for error binders / discard temps —
   a new authoring surface outside the stated scope.
2. **"the measured population differs from the proposed scope"** — the
   scope framed R6 site-3 as user-destructible scope-exit drops that
   "naturally belong at cleanup hooks under ledger authority." The
   measurement shows 98% are ERROR-BINDER (`throws`/try-catch) and
   discard-temp cleanup with no hook — a different mechanism than the
   user-scope destructibles cleanup_authoring models.

site-4 + R2 + R7 bijections DO close (instruction-local → sibling
overwrite-authoring pass, 1:1), and their homes are understood. But
Slice B is ONE unit with R6 in scope, and a listed stop condition
fired — so per direction the slice STOPS here for a scope decision,
rather than proceeding.

### Options for the maintainer (R6 site-3 only)

- **A — expand scope**: add HIR→MIR CleanupHook emission for error
  binders / discard temps so cleanup_authoring authors them under
  ledger authority (largest surface; touches lowering + likely the
  throws/try-catch machinery).
- **B — sibling-pass relocation**: move site-3's exact apparatus
  (`initialized_at_return` dataflow + zero-storage widening + skip
  set + `_drop_all_destructibles`) into the sibling
  overwrite/scope-cleanup pass at the Return boundary — a valid
  new-author location, but it RELOCATES the bespoke error-binder
  logic rather than consolidating it under hook/ledger authority
  (does not match the "cleanup hooks" premise).
- **C — split R6 site-3 OUT of Slice B**: proceed with site-4 +
  R2 + R7 (all close cleanly into the sibling overwrite pass; their
  homes and ordering are understood), and take R6 site-3 as its own
  later slice once its home is decided. Keeps Slice B behavior-
  changing but bounded to the overwrite family + site-4.

RECOMMENDATION: **C**. It lets the mechanically-clean, bijection-
closed populations (233,519 + 143,008 + 14) land now under a proper
sibling overwrite-authoring pass, and defers the genuinely different
error-binder-drop problem (site-3) to a slice scoped to it. A and B
both carry unmeasured surface (lowering change / bespoke-logic
relocation) that this branch's scope did not budget.

## 5. Slice B1 — NARROWED to R2 + R7 (review 2026-07-20T143144Z). Design CLOSED.

Scope: **376,527 instruction-local releases — R2 String overwrite
(233,519) + R7 Array overwrite (143,008).** DEFERRED to Slice B2:
ALL R6 — site-3 (1,088) AND site-4 (14). Rationale (review): site-4
shares the destructible-helper + ledger problem with site-3; moving
its 14 events now deletes no additional authority and would import
most of B1's complexity (an unproven post-string_arc ledger-
equivalence claim). B2 = "synthetic Return-epilogue cleanup" +
site-4.

RETAINED in string_arc through B1: site-3, site-4, `_release_local`
(R3), `_drop_destructible_local` / `_drop_all_destructibles` (B2
destructible helper). DELETED in B1: `_drop_array_local` once R7's
sole caller migrates.

### 5.1 Placement — overwrite_cleanup AFTER string_arc, NO ledger

Driver order UNCHANGED through rebuild A:
`author_cleanup → materialize_call_arg_stakes → materialize_lastuse_releases → build_and_attach_ledger(A) → string_arc [consumes A]`,
then a NEW bucket: `→ [overwrite_cleanup]`.

- rebuild A stays BEFORE string_arc, unchanged — string_arc consumes
  it for site-3 / zero-widening (both still resident, B2). No rebuild
  B, no stale-ledger question: R2/R7 need NO ledger (pure
  `_is_string_tid` / array-kind structural checks).
- string_arc runs FIRST → its recognition/occurrence-counting walk
  never sees the overwrite releases (avoids the fail-closed
  "unexpected input release" trip) → materialized_lastuse_release
  (618,744) and every string_arc counter provably unperturbed.
- `overwrite_cleanup` walks the post-string_arc MIR: for each String
  StoreLocal/MoveFromRef/StoreRef/ArrayIndexStore emit the release
  before the store (R2); for each StoreLocal into an array local emit
  the array drop before the store (R7). Old-value-before-new-store
  ORDER + spans preserved (release emitted immediately before the
  store; spans copied from the store instr).
- Retain-before-release / self-alias (`x = x`): the store-VALUE copy
  stake (retain) is materialized upstream by
  `materialize_call_arg_stakes` (string_stakes.py:244-246 covers
  StoreLocal/StoreRef/ArrayIndexStore value operands) BEFORE
  string_arc, so the overwrite release (after string_arc) can never
  drop the shared refcount below the retain. Preserved by pipeline
  order.

### 5.2 Completeness — validated BY the overwrite pass (not prospectively)

`string_arc` cannot assert a release authored by a LATER pass
(review finding 3). `overwrite_cleanup` itself validates EXACT-ONE
authoring: it walks the eligible-store set, authors exactly one
release/drop per eligible store, and a post-pass check confirms every
eligible String store is immediately preceded by its authored release
(and every array-local store by its array drop) — an eligible store
without its authored cleanup is a fail-closed tripwire inside the
pass. No cross-pass assertion.

### 5.3 Counter preservation — dedicated strict counted-only recorder

`finalize(l_pre=None)` is REJECTED (review finding 2): that path
records `skipped_no_ledger=1` and bumps `fns`. Instead a dedicated
**strict supplemental recorder** on the reporter: it folds ONLY the
allow-listed counted-only classes into the process-global aggregate —
for narrowed B1 that allow-list is EXACTLY `{overwrite_release}` —
adding only `events` + `site_class:overwrite_release`; it runs NO
C1/C2/C3 aggregation, emits NO `skipped_no_ledger`, and does NOT
increment `fns`. A note with any class outside the allow-list is a
fail-closed error. R7 array drops carry NO counter (identical to
today — R7 had no audit note), so the recorder only ever sees
overwrite_release. Net: overwrite_release 233,519 exact, every other
counter +0.

### 5.4 Classification sharing — minimal (R2/R7 only)

Extract only what R2/R7 need: `string_locals` (R2 StoreLocal gate)
and `array_locals` (R7 gate) builders → shared helper both string_arc
and overwrite_cleanup call (single source of truth; a mismatch would
double-drop or leak). The destructible/nullsafe/error apparatus
(~150 lines) is NOT touched — it stays in string_arc for B2.

### 5.5 Predicted acceptance (Slice B1, narrowed)

- Bijection 1:1 exact: R2 233,519 (StoreLocal 231,660 / MoveFromRef
  926 / StoreRef 925 / ArrayIndexStore 8) + R7 143,008 — each old
  string_arc emission → its emission in `overwrite_cleanup`, same
  instruction / order / span.
- `overwrite_release` stays **233,519**; EVERY other counter +0
  (fns 1,107,693, materialized_lastuse_release 618,744,
  moveout_expansion, scope_exit_release 68,562, and the deferred
  site-4 drop_before_overwrite_site4 stays 14 in string_arc — B1
  does not touch it).
- Emitted-MIR byte-identity where practical (spot probe) — identical
  release/drop sequences at identical points.
- Pins: leak / double-drop / order + self-alias for String
  StoreLocal, StoreRef, ArrayIndexStore, MoveFromRef, and Array
  overwrite.
- `_drop_array_local` DELETED after R7 migrates; destructible helper
  + site-3/site-4 RETAINED (B2); `_release_local` RETAINED (R3).
- Version **0.33.87 / ABI 21** — instruction-local re-home, no
  runtime/boundary change. SUPERSEDED by the consolidated endgame
  directive (2026-07-20T195625Z): B1 gets NO standalone certification;
  it is a commit-clear chunk on the single 0.33.87 endgame candidate,
  certified once at the end when `string_arc.py` is deleted.

## 6. Implementation log (Slice B1)

- 2026-07-20: IMPLEMENTED. New `overwrite_cleanup.py` runs AFTER
  string_arc (no ledger). string_arc stripped of R2 (4 arms) + R7
  emission (keeps `_note_use`); `_drop_array_local` DELETED. Shared
  `classify_string_array_locals` extracted to the R10 lib; string_arc
  routes through it. Strict `record_counted_only` recorder
  (allow-list {overwrite_release}; adds events + site_class only, NO
  fns/skipped_no_ledger). Driver `overwrite_cleanup` bucket after
  string_arc with boundary containment.
  BUG FOUND+FIXED: the pass must SKIP stores whose value is a
  ZeroValue dest — string_arc's synthetic entry-init / zero-back
  stores (String + Array) would else be treated as overwrites and
  load-before-store the entry-init (SSA violation).
  **SUPERSEDED (closure round 1):** the original broad `zero_dests`
  shape-skip below was UNSOUND — string_arc recognizes an *input*
  ZeroValue(String) as a valid owned empty value, so a user store of
  such into a live slot IS a real overwrite and must NOT be skipped
  by shape. Replaced by explicit provenance marking: string_arc marks
  its six synthetic zero-stores `synthetic_zero_back=True`;
  overwrite_cleanup skips ONLY marked stores. The `zero_dests`
  description here is retained for history only. Original claim:
  "`zero_dests` skip reproduces string_arc's original 'input user
  stores only' set exactly (user stores never store a bare
  ZeroValue)" — false for input ZeroValue(String); do not rely on it.
  Single-fixture diff (om_match_bind_string_heap_concat) vs pristine:
  EVERY counter identical (overwrite_release 262, events, fns). Pins:
  test_overwrite_cleanup.py 6/6; two string_arc unit tests updated to
  moved-authority; reporter battery 57/57; overwrite memcheck
  carriers green. Version 0.33.87 stamped.
- 2026-07-20: ACCEPTED. Corpus build/tmp/b1 vs flagret, exit 0 —
  EVERY counter +0 (overwrite_release 233,519 exact; fns 1,107,693;
  site-4 14; materialized_lastuse_release 618,744). Targeted stage2
  battery 85/85 (overwrite 6 + reporter 51 + extraction 2 +
  cleanup_authoring 14 + drop_flags 12); overwrite memcheck carriers
  green; history.md 0.33.87 entry written. Slice B1 COMPLETE —
  STOPPED for static delta review. NEXT: Slice B2 (site-3 synthetic
  Return-epilogue cleanup + site-4). Report:
  /tmp/drift-announce/2026-07-20T153917Z-sliceb1-report.md.

## 7. Static-review closure round (2026-07-20T191052Z)

Three release-blocking findings + closure amendments, all addressed:
- **#1 provenance**: shape-based ZeroValue skip REPLACED by explicit
  marking — string_arc marks all six synthetic zero-back stores
  `synthetic_zero_back=True`; overwrite_cleanup skips exactly the
  marked ones (never value shape). Pins BOTH directions: marked
  synthetic → skipped; UNMARKED input `ZeroValue(String)` /
  `ZeroValue(Array)` overwrite of a LIVE slot → still releases/drops.
- **#2 completeness**: independent PRE-rewrite eligible-site
  inventory (by object identity) + STRUCTURAL post-rewrite validation
  (`_validate`/`_cleanup_precedes`: exactly one canonical cleanup
  immediately before each inventoried store, correct
  local/ptr/array/index/type). TEETH pin monkeypatches the emitter to
  suppress one authoring → validator raises.
- **#3 mutation guardrail**: overwrite_cleanup.py added to
  test_ledger_cache_safety_mutation_audit SCOPED_FILES (audit 6/6);
  ledger_cache prose + audit docstring updated; inline allow marker
  at the `block.instructions =` site (dirty mark at pass end).
- Amendments: self-alias pin asserts CopyValue < StringRelease <
  user StoreLocal; R7 pin asserts full canonical sequence + order;
  recorder pin snapshots/restores the aggregate and proves the exact
  delta is only events + site_class:overwrite_release (fns/
  skipped_no_ledger/C1-C3 unchanged) + positive-int validation;
  driver-boundary containment pin (phase overwrite_cleanup, empty
  IR, no traceback). Wording retargeted to present-tense
  overwrite_cleanup authority (string_arc:1664, ledger_cache,
  mir_nodes StoreRef/MoveFromRef, the self-concat memcheck carrier;
  historical explanations preserved). Living-record site-3 sentence
  corrected to the precise same-program-point result (1,068 no-hook +
  20 hook-elsewhere).
  Pins: test_overwrite_cleanup.py 11/11; reporter + extraction +
  mutation-audit 59; single-fixture overwrite_release 262 preserved.
  Corpus re-run (marked provenance changes emission logic) RUNNING.

## 8. Closure round 2 (review 2026-07-20T200715Z) — two guards completed

- **Validator → true BIJECTION**: each authored cleanup is tagged
  `ow_authored_for=id(store)`; `_validate` now proves a bijection
  between the pre-rewrite inventory and the tagged cleanups —
  rejects ORPHAN (tag → no site), MISSING (site → no tag),
  DUPLICATE (>1 tag per site), and validates FULL operand/type
  linkage (LoadLocal.local, ZeroValue.ty==String, zstore.value==
  ZeroValue.dest, StringRelease.value==LoadLocal.dest, LoadRef/
  ArrayIndexLoad inner_ty/elem_ty match, ArrayDrop.elem_ty==array
  element). Fail-closed on an id-collision (same store object twice
  in the stream). Teeth pins: missing / duplicate / orphan / broken
  ZeroValue-ty / broken release-operand / StoreRef wrong-inner-ty —
  each proven to raise (6 new teeth pins; battery 16/16).
- **Mutation guard → real teeth**: the inline allow marker REMOVED;
  a real `mark_ledger_dirty(func, "overwrite_cleanup.block_rewrite")`
  now sits within the audit proximity window of each changed block's
  `block.instructions =` mutation. Proven: removing the mark makes
  the audit flag overwrite_cleanup.py:247; restored → 6/6.
- `ledger_cache` opening sentence corrected: overwrite_cleanup (and
  string_stakes) do NOT consult the ledger — they mutate MIR the
  ledger is cached against, following the dirty-bit discipline.
- Authority sweep FINISHED: retargeted present-tense StoreRef/array-
  overwrite authority to overwrite_cleanup in hir_to_mir.py (2
  comments), test_array_nested_scope_drop / test_mem_replace_string_
  uaf / test_maybe_uninit_local_uaf / test_hash_map_maybe_uninit_
  alignment / test_assign_store_ref_drop_bearing_lowering /
  test_mut_struct_string_field_self_concat (historical explanations
  preserved).
- Emission-neutrality: the closure changes are a post-validator +
  instruction ATTRIBUTES (not MIR fields) + a relocated dirty mark +
  comments — production authoring/counters UNCHANGED from the
  marked-provenance corpus (build/tmp/b1v2), which stands as the
  acceptance (no re-run per review). Touched battery 75/75.
- HANDOFF policy: B1 does NOT trigger a standalone full suite /
  certification / deploy (endgame directive 2026-07-20T195625Z);
  after static clearance, commit for recoverability and continue
  directly into B2 under the single 0.33.87/ABI-21 endgame boundary.

## 9. Corpus acceptance — final (924/924, CLEAN +0)

The fresh marked-provenance corpus run (build/tmp/b1v2) landed with
`fixtures_compiled: 903` — 21 short of the 924 baseline. Investigation:
the 21 missing were a CONTIGUOUS alphabetical block of the
`concurrent_*` family (`concurrent_is_complete_cancel_pending` …
`concurrent_spawn_coerce_callback_captures`), with 40 concurrent
fixtures compiling BEFORE the gap and 13 AFTER it — the signature of
an interrupted/killed corpus shard on the busy machine, NOT a
systematic B1 regression. Confirmed by recompiling all 21 directly
under the current tree: 21/21 exit 0, each emitting its audit
aggregate. Merged the 21 fresh audit files with b1v2's 903 and
re-aggregated the full 924-fixture universe against flagret
(0.33.85 baseline):

    every counter delta = +0 (14/14 counters identical)
    site_class:overwrite_release = 233519 (exact)
    events = 2772976, fns = 1107693 (both exact)

=> B1 is emission-neutral and counter-exact. The overwrite_release
authority moved from string_arc to overwrite_cleanup with the
`record_counted_only` recorder holding events + site_class:
overwrite_release steady. FINAL corpus gate: PASS.

Process note (correction): the closure-round changes (bijection
_validate, ow_authored_for tags, per-block dirty mark) were claimed
"emission-neutral, no re-run needed." That reasoning was unsound as
stated — a _validate that RAISES is caught by the driver boundary
containment and becomes a compile failure, so it genuinely required
the corpus re-run to confirm. The re-run confirmed +0; but the
"no re-run needed" shortcut was wrong and is retracted. Any future
change touching the validator or authoring MUST re-run the corpus.

## 10. B2 / final-cleanup debt (from final review 2026-07-20T222129Z)

Non-release-blocking; MANDATORY before the single final 0.33.87
certification; fold into the next coherent B2/endgame work — do NOT
spin another isolated B1 gate for these.

1. **Harden `_validate`'s rewritten-site lookup.** `pos[id(store)]`
   assumes each inventoried store survives exactly once in the output.
   A future authoring regression that DROPS a store → uncaught
   `KeyError`; DUPLICATING the same object → silent position collapse.
   Fix: track output positions per identity, raise a CONTAINED
   `AssertionError` unless each inventoried store occurs exactly once.
   Add vanished-store + duplicated-store teeth pins when touched.
2. **Remove validation-only dynamic MIR metadata after use.**
   `ow_authored_for` (host-process `id()`s) and `synthetic_zero_back`
   (migration provenance) must NOT survive the completed endgame —
   strip them post-validation (once each attribute's sole consumer
   has run), or let them die naturally with the final string_arc
   handoff in D. No object-ids / migration provenance in output MIR.
3. **Documentation cleanup before final cert:**
   - `test_mut_struct_string_field_self_concat.py` ~lines 92, 244,
     298 still carry present-tense `string_arc` StoreRef authority →
     retarget to `overwrite_cleanup`.
   - SLICE-B §5.5 "own certification" + §6 broad `zero_dests` claim —
     DONE (superseded markers added this round).
   - RESUME-CHECKPOINT trailing "each step is its own checkpoint" /
     report-only STOP language — DONE (consolidated-directive block
     added this round).
4. **Remove unused local `mutated`** in `overwrite_cleanup.py`
   (lines 185/252) when the pass is next edited.

Items 1, 2, 4 and the test-doc bullet of 3 remain OPEN for B2/D.
