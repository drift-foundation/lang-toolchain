# zero-storage-safe drop-flag retirement — report-only checkpoint

Status: rev 2 — the four review amendments (2026-07-20 static
review) are folded in: §1.2a full-population identity closure, §6
emission-shape proof + structural acceptance additions, §2 corrected
admission formula, §5.3/§7 site-3 causality + implementation order.
Arm B is the reviewer-preferred direction; implementation GO
attaches on amendment closure. Implementation targets compiler
0.33.86 / ABI 21, then certification and release. Sequencing after this slice per direction: small
recorded follow-ups → full string_arc endgame/re-homing inventory →
B-repr(B5) entry audit.

Baseline: the post-review-closure 0.33.85 candidate tree
(`build/tmp/sweep-closure` reference; all gates green).

## 1. Inventory — every flag-managed zero-storage-safe local

Corpus measurement 2026-07-19 (`build/tmp/flaginv-measure`, exit 0,
universe identical 924/344/49; scratch instrument across
drop_flags/cleanup_authoring/string_arc/reporter, REVERTED
byte-identically — 4× cmp vs pristine, zero scratch refs, battery
51/51 on the restored tree).

### 1.1 Flag-managed locals by kind × zero-storage-safety

| class | locals | per fixture | bookkeeping stores (init+set+clear) |
|---|---|---|---|
| **ARRAY (zs)** | **15,711** | 17 | 49,906 |
| **VARIANT (zs)** | **4,683** | ~5 | 18,757 |
| SCALAR/String (zero-UNSAFE per predicate — KEEP) | 7,406 | ~8 | 27,762 |
| STRUCT (zero-unsafe — KEEP) | 4,691 | ~5 | 16,842 |

Zero-storage-safe total: **20,394 flag locals** and **68,663 flag
stores** (each paired with a ConstBool — ≈137k bookkeeping
instructions), plus the flag locals themselves and the guarded CFG
splits below.

### 1.2 Identity — standing stdlib populations (single-fixture drill-down)

Arrays (17/fixture): `std.cli::ArgParser::parse` ×4 accumulator
arrays; `std.codec` ×4 decode `out` buffers;
`std.fs::_read_all_capped acc`; `std.io::poll_many ready`;
`std.json::_parse_array values+items`, `_parse_object_throwing
occurrences`; `std.random::random_secure_bytes buf`; `std.regex` ×3
parse accumulators. Variants (5/fixture): `std.fs::read_to_bytes
cr`; `std.json::_parse_array child_sp`, `_parse_object_throwing
child_sp`, `_parse_literal node`, `parse_located sp` — the familiar
C3-population carriers.

### 1.2a Identity — full-population closure (review amendment 1)

The standing baselines account for 17×924 = 15,708 arrays and
5×924 = 4,620 variants; the aggregate residues are identified
EXACTLY (per-fixture aggregate mining + instrumented re-probe of
every deviant class):

| fn / local | type | fixtures | extra locals | extra stores | admission | emission |
|---|---|---|---|---|---|---|
| `std.concurrent::FutureGroup<T>::join_all` `out` (generic inst) | ARRAY | concurrent_future_group, concurrent_stress_join_all | 2 | 3 + 3 | 2a (user moveout + live-at-exit) | NO PD hook decisions — flag gates nothing |
| `m::exercise_direct_field_var_reassign` `names` | ARRAY | struct_ctor_heap_array_transfer_no_uaf | 1 | 4 | 2a | NO PD hook decisions — flag gates nothing |
| **ARRAY closure** | | 3 fixtures | **+3 → 15,711** | **+10 → 49,906** | | |
| fixture-local user Optional/Result locals — e.g. `main any/all` (FutureGroup join results), `main popped/popped2/popped3` (Array.pop results), `m::scenario_value_producing_match m`, `m::case_named_subset_bind r`, `m::scenario_reassignment_before_match r` | VARIANT | 53 fixtures | **+63 → 4,683** | **+277 → 18,757** | 2a | UNGUARDED (predicate-first) with dead flag-clear; multi-hook locals produce >1 decision |
| **unguarded VARIANT decision closure** | | 25 fixtures | | | | **+68 → 8,384** (e.g. `m` ×5 hooks, `popped*` ×6, `r` ×5, `any/all` ×2 — verified per-fn) |

Every extra is the SAME anomaly class as the standing populations:
2a-admitted, zero-storage-safe, flags gating nothing at cleanup
(arrays: no PD decisions at all; variants: unguarded emission).
Sums close exactly: 15,708+3, 49,896+10 (3+3+4), 4,620+63,
18,480+277, 8,316+68.

### 1.3 What the flags currently do

PD hook decisions (corpus): guarded ARRAY:flagged 3,696 (the json
accumulators — the ONLY flagged arrays whose flags still gate
emission); guarded SCALAR:flagged 2,772 + STRUCT:flagged 2,773
(zero-unsafe, correctly guarded — out of scope);
skip SCALAR:unflagged 924 (the PD-skip tripwire residual);
unguarded ARRAY:unflagged 924 (B-M); **unguarded VARIANT:flagged
8,384** — flag-managed variants ALREADY take unguarded authoring
(predicate-first, preserved through B-M): their flags gate NOTHING
at cleanup; they exist only as 2a-admission overhead plus a
flag-clear store after each authored drop.

c3_moveout_flag_guarded event split (8,316): **ARRAY 3,696** +
SCALAR/String 2,772 + STRUCT 1,848. Most flagged arrays (15,711
locals vs 3,696 guarded decisions) never reach a PD hook at all —
their flags are pure bookkeeping overhead today.

## 2. Arm comparison

### Arm A — Array-only unguarded unification

Scope: drop_flags admission excludes ARRAY (both criteria);
cleanup_authoring's flag-managed-array exception (the B-M
counter-neutrality gate) is removed — PD arrays are always
unguarded; array flags retire.

- Counter migration (exact): `c3_moveout_flag_guarded` 8,316 →
  4,620 (−3,696 = the ARRAY event share); `c3_moveout_zero_safe`
  10,254 → 13,950 (+3,696); EVERY other counter +0.
- Removed: 15,711 flag locals; 49,906 flag stores + as many
  ConstBools; the guarded-split CFG blocks for the 3,696 array
  guards (the `{blk}_cleanup_post_{local}` pairs).
- Leaves the VARIANT anomaly in place: 4,683 flag locals + 18,757
  stores that gate nothing (§1.3), and the ladder keeps a
  type-kind special case.

### Arm B — generic retirement for ALL zero-storage-safe types (RECOMMENDED)

Scope: Arm A PLUS variants. The admission change is an ADDITIONAL
EXCLUSION — the existing criteria are preserved verbatim (review
amendment 3):

```
flag_for admits `name` iff
    needs_drop(ty)
AND NOT zero_storage_drop_safe(ty, type_table)   # NEW exclusion
AND _has_user_moveout(func, name)                # existing
AND (   _is_potentially_live_at_some_exit(...)   # existing (2a)
     OR _has_zero_storage_unsafe_path_dependent_at_cleanup_hook(...)
    )                                            # existing (2b —
                                                 # already predicate-
                                                 # aware post-B-M)
```

The cleanup ladder simplifies to `zs → UNGUARDED; flagged →
GUARDED; else SKIP` (the B-M array exception dies naturally —
flag-managed zs locals can no longer exist). Pins must cover BOTH
criteria in BOTH directions (§7.1).

- Counter migration: IDENTICAL to Arm A (±3,696 pair only).
  Variants contribute ZERO additional counter movement — their
  cleanup emission is already unguarded; retirement removes only
  the admission, the flag locals, the init/set/clear stores, and
  the post-drop flag-clears (none of which are audit events).
- Removed (total): **20,394 flag locals; 68,663 flag stores +
  68,663 ConstBools (≈137k instructions); 3,696 guarded splits.**
- Completes the doctrine the review-closure wording already states
  ("only zero-storage-UNSAFE PathDependent needs a runtime flag")
  — the code matches its own documentation with no residual
  special case.

Recommendation: **Arm B.** Strictly more dead state removed, zero
additional emission risk (variants change nothing at emission), one
uniform rule, and it retires the B-M ladder exception instead of
codifying it.

## 3. Ordering effects

- Arm A/B arrays (3,696): the drop moves from the flag-guarded
  `drop_blk` to the inline hook position (§6 proves the guarded
  population is fallback-shaped only) — the SAME scope boundary,
  same reverse-declaration candidate order; the flag branch
  disappears. On non-live paths
  the now-unconditional drop is a §-4 no-op; on live paths it runs
  at the same boundary as today. No user-observable reordering; the
  RAII ordering carrier (test_array_sweep_raii_order) keeps pinning
  the contract.
- Arm B variants: NO emission change whatsoever → no ordering
  effect (the 8,384 unguarded variant drops stay byte-positioned;
  only their trailing flag-clear stores vanish).

## 4. Safety proof

- **Arrays**: unguarded drop over non-live storage is a no-op via
  the B-M chain — string_arc's entry-block array zero-init
  (surviving responsibility) + MoveOut expansion zero-backs +
  len=0 element helper / `drift_free_array(NULL)`
  (array_runtime.c). Unchanged from the accepted B-M proof.
- **Variants**: tag-0 PHI-zero dispatch no-op (the original
  sub-step-3 doctrine); emission is ALREADY unguarded — retirement
  removes state, not behavior.
- **The `flag ≡ owns` invariant** remains intact for the surviving
  zero-unsafe population (String/struct); zero-storage-safe types
  never needed it: when the drop of zeroed storage is a no-op,
  ownership disambiguation at the drop point is unnecessary by
  construction.

## 5. Consumer audit (everything that reads flag state)

1. `drop_flags` (producer) — admission rule change is the slice.
2. `cleanup_authoring` — `flag_managed`/`flag_for` decision inputs;
   under Arm B the zs branches become unreachable-by-construction
   (ladder simplifies; the B-M exception and its
   counter-neutrality pin retire WITH a note).
3. `string_arc` site-3 `_flag_managed_at_return` — arrays never
   reach it (not `destructible_locals`); flag-managed VARIANTS do.
   Retiring variant flags shrinks that skip set, and coverage is
   STATICALLY SUBSUMED by the generic ledger consultation (review
   amendment 4, corrected causality): cleanup_authoring's existing
   unguarded `MoveOut` transitions the REBUILT ledger's state to
   MOVED_OUT before the Return, so the destructibles consultation
   (string_arc's Phase 4 sub-step 1 loop) returns MUST_NOT_DROP
   and adds the local to `skip_cleanup_locals` BEFORE
   `_flag_managed_at_return` is even formed.  The runtime
   zero-backing proves the paired drop is SAFE — it is NOT what
   causes the ledger verdict; the verdict comes from the ledger's
   MOVED_OUT transition.  REQUIRED PIN (§7.3), and implementation
   ORDER: the site-3 regression pin lands BEFORE the admission
   change; the now-stale site-3 flag-authority comments update in
   the same slice.
4. Reporter `_is_flag_guarded_cleanup_moveout` (C3 A-rule) — reads
   `_drop_flag_for_local`; post-retirement the rule matches only
   the surviving zero-unsafe guards; the 3,696 array events migrate
   to the zero-safe leg (they become paired unguarded cleanups).
   No reporter code change needed; fail-closed as-is.
5. Tests — the B-M flag-managed-array pin
   (`test_authoring_keeps_guarded_branch_for_flag_managed_array`)
   retires (subject: the exception being deleted); drop_flags
   admission pins update; everything else consumes flags only for
   zero-unsafe carriers.

Observation recorded, OUT of scope: `drift_string_release` on
zeroed bytes is also a runtime no-op — admitting SCALAR-String to
the predicate could retire 7,406 more flags and convert the 2,772
guarded String cleanups, but it interacts with string_arc's string
machinery mid-endgame; a candidate slice AFTER the string_arc
re-homing inventory, not before.

## 6. Predicted acceptance (Arm B, vs the 0.33.85 reference)

| counter | delta |
|---|---|
| c3_moveout_flag_guarded | 8,316 → 4,620 (−3,696) |
| c3_moveout_zero_safe | 10,254 → 13,950 (+3,696) |
| every other counter (events, moveout_expansion, drift, ... ) | +0 |

**Emission-shape PROOF (review amendment 2 — the table is
DEFINITIVE, not an assumption):** the guarded array split is
fallback = 3,696, edge-elaborated = 0, derived from two
independently-measured equalities. `pddec:guarded:ARRAY:zs:flag =
3,696` counts guarded array DECISIONS at classification time
(before any per-arm demotion); `c3fg_kind:ARRAY = 3,696` counts
MoveOut EVENTS the reporter's A-rule classified via its STRUCTURAL
fallback-shape match (MoveOut at index 0 of a block entered through
an `IfTerminator` loading the subject's own flag — the `drop_blk`
shape).  An edge-elaborated demotion produces edge-end MoveOuts
with NO flag-load guard block, which the A-rule cannot match (they
classify `c3_moveout_owned`: the edge is LIVE).  Decisions = A-rule
events therefore forces demotions = 0 and exactly ONE fallback
MoveOut per guarded decision — the ±3,696 pair is exact.  Any
deviation at acceptance remains a STOP (§8).

**Structural acceptance additions (review):** alongside the counter
pair, the acceptance re-runs the measurement instrument (scratch,
reverted with the standard proof chain) on the implemented tree and
requires EXACT structural deltas:
- zero-safe flag locals 20,394 → 0 (`flagmgd:ARRAY:zs` and
  `flagmgd:VARIANT:zs` keys ABSENT);
- zero-safe flag stores 68,663 → 0 (and their paired ConstBools);
- the 3,696 guarded array block pairs removed (block-count /
  `_cleanup_post_` spot probe);
- zero-UNSAFE String/Struct populations BYTE-IDENTICAL
  (`flagmgd:SCALAR:unsafe` 7,406 / 27,762 stores;
  `flagmgd:STRUCT:unsafe` 4,691 / 16,842 stores).

## 7. Pins (implementation)

1. Admission — BOTH criteria, BOTH directions (review amendment 3):
   2a-positive (zero-unsafe struct, user moveout + live-at-exit →
   admitted); 2a-NEGATIVE (array and variant with the identical
   shape → NOT admitted); 2b-positive (zero-unsafe struct PD at a
   mid-fn hook → admitted); 2b-NEGATIVE (array/variant PD at a
   mid-fn hook → not admitted); plus user-moveout precondition
   control (no user move → never admitted regardless of type).
2. Ladder: PD zs → unguarded for BOTH kinds regardless of any stale
   flag metadata (fail-closed against attribute leftovers); the B-M
   flag-managed-array guarded pin retires with a note.
3. Site-3 variant coverage — LANDS BEFORE THE ADMISSION CHANGE
   (review amendment 4): PD variant with authored unguarded cleanup
   at the hook → exactly-once drop through the driver,
   valgrind-clean on both path outcomes, covering BOTH the standing
   `parse_located sp` shape AND at least one §1.2a fixture-specific
   shape (e.g. the `Array.pop` Optional `popped` or the
   value-producing-match `m` carrier).  The pin's contract wording
   states the §5.3 causality: ledger MOVED_OUT drives the skip;
   zero-backing only proves drop safety.
4. Valgrind rows: existing json-accumulator + read_to_bytes rows
   stay; NEW variant-flag-retirement row (the `parse_located sp`
   shape: conditionally-consumed variant through a throwing parse,
   both outcomes).
5. RAII ordering carrier unchanged (already mechanism-agnostic).
6. Stale site-3 flag-authority comments updated in-slice (review
   amendment 4).

## 8. Stop conditions

- Any counter outside the exact ±3,696 pair (incl. any
  events/moveout_expansion movement — the multiplicity note).
- The site-3 variant pin shows a double-drop or a missed drop
  (subsumption claim wrong) → stop and report.
- Reporter A-rule misclassifies any surviving zero-unsafe guard
  after retirement.
- drop_flags admission change disturbs any zero-UNSAFE carrier
  (String/struct flag counts must be byte-identical).

## 9. STOP

Report-only; no implementation. On approval: implement per §2 Arm
B (or Arm A if directed), acceptance per §6, then 0.33.86 / ABI 21
→ certification → release. Then: small recorded follow-ups → full
string_arc endgame/re-homing inventory (entry zero-init, overwrite
`_drop_array_local`, `_release_all_locals`/String authority, the
String-predicate observation above) → B-repr(B5) entry audit.
