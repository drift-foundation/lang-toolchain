# TLR-7 design checkpoint — the cross-block tail (report only)

Status: DESIGN/CHECKPOINT ONLY — no implementation.  Prerequisite
state: TLR-6 accepted (`build/tmp/cleanup-tlr6`: temp_lastuse_release
7,398 — the LAST population outside the materialization authority;
materialized 611,346; all nine hard gates zero).  This gate unlocks
the `_note_use` release-arm tripwire.

## 0. Framing (per review): what "no new lifetime analysis" means

TLR-7 reuses the EXISTING fn-wide `compute_string_temp_liveness`
fixpoint as-is and adds FN-WIDE PRODUCER RESOLUTION.  It is not "no
lifetime reasoning" — the liveness authority already reasons across
blocks (including backedges); TLR-7 simply stops discarding its answer
by resolving producers per block.  No new lifetime analysis beyond the
existing liveness authority.

## 1. Measured split (scratch remeasure on the TLR-6 tree)

Instrumentation: fn-wide producer map + CFG-shape classifier in the
shim's residual branch (`TM_xb_{producer}_{shape}`; shape = loop if the
drain block sits in a cycle with/through the source block, else linear
if a unique single-pred chain connects them, else join); scratch out
`build/tmp/xb-measure` (exit 0, universe identical 924/344/49, events
2,772,052 unchanged, materialized 611,346 unchanged, plain residue 0);
restoration via stored reverse edits (3 reversed, zero `TM_`/`_tm_`
refs, battery 50/50).

| fn-wide producer × CFG shape | count |
|---|---|
| **StringConcat × loop** | **7,392** |
| **StringConcat × linear** | **6** |
| join / no-producer / ANY non-family producer | 0 |
| **sum** | **7,398 — lossless** |

Every temp resolves to a FAMILY producer (StringConcat, member since
TLR-3).  The dominant shape: a Concat temp produced in one block of a
loop and drained in a LATER block of the same iteration (per-iteration
lifetime crossing an intra-loop block boundary — string-building loops
whose body spans several blocks: bounds checks, conditionals inside the
body).  The 6 linear cases are trivial straight-line chains.  There are
NO join-drain cases and NO Phi/AIL/MoveOut/LoadRef producers.

**Consequence for acceptance: temp_lastuse_release → 0 exactly** — no
residuals to itemize (review's residual-itemization branch is moot).

## 2. Mechanism: fn-wide producer resolution, same drain points

Every one of the 7,398 is a release string_arc emits TODAY at a
specific drain: per-block occurrence counting + the fn-wide `live_out`
guard.  TLR-7 reproduces EXACTLY those points; the only machinery
change is where the qualification/recognition looks up the producer:

- `_analyze_lastuse_block` gains a fn-wide producers map (built once
  per fn, shared by the PASS and string_arc's recognition — one lookup
  authority; SSA single-assignment makes it unique by construction,
  and a DUPLICATE dest in the map build fails closed as an SSA-contract
  violation).  `_is_family_temp` consults it instead of the per-block
  map.
- The per-block occurrence counting, drain-point computation
  (multiplicity rule), terminator handling, and the fn-wide liveness
  guard are all UNCHANGED.
- The pass iterates blocks exactly as today with the fn-wide map;
  per-block points now include temps produced elsewhere; insertion
  logic unchanged (release after the drain instruction IN THE DRAIN
  BLOCK).

## 3. Safety proofs (per review requirements)

### 3a. Double-release: path-exclusivity via the liveness fixpoint

A temp can have release points in MULTIPLE blocks only on CFG-disjoint
paths.  Proof: suppose points P1∈B2 and P2∈B4 with B4 reachable from
B2.  B4 contains a use (its drain), so the temp is live-in at B4 and
therefore live-out of B2 by the fixpoint — but a block whose live_out
contains the temp gets NO point (the guard).  Contradiction.  The
fixpoint propagates liveness along ALL edges INCLUDING BACKEDGES, so
the argument covers loop shapes: a temp used again next iteration is
live-out through the backedge and never drains inside the loop.  (The
7,392 loop-shaped cases drain INSIDE one iteration precisely because
the temp does NOT cross the backedge — fresh Concat per iteration.)
Pinned with a dedicated loop/backedge pin (§5).

### 3b. Cross-block suppression is already structural

`owned_values` is re-seeded FRESH per block from the fn-wide
`owned_defs` and the recognition subtraction
(`owned_values -= recognized_released`) runs per block: in the DRAIN
block the recognized temp is subtracted before any `_note_use` can
fire; in the PRODUCER block the temp never drains (its uses are
elsewhere), so ownership there is inert.  The family-arm guards
(ConstString/StringFrom*/Concat/CopyValue) cover only the
producer-and-release-share-a-block case they were built for — no new
suppression machinery is needed, and the teeth pins keep their bite.

### 3c. Bypass-path caveat (review requirement 1) — behavior
equivalence, NOT a leak proof

The path-exclusivity proof prevents double-release; it does NOT prove
every producer→exit path passes a drain block.  If one branch uses the
temp and another bypasses all uses, TODAY's string_arc also emits no
release on the bypass path — TLR-7 MIRRORS today's drain points and
explicitly does NOT attempt to fix such hypothetical bypass leaks
(that would be a semantics change with its own gate, and would break
emission identity).  Empirical bound from the measurement: all 7,398
drains are loop-internal or linear — there are ZERO join-shaped drains,
and a bypass shape requires a branch structure around the uses; the
shape data is consistent with the bypass population being empty, but
the claim of record is EQUIVALENCE, not absence.  Corpus events +0 and
memcheck-clean are the operative guarantees.

## 4. Contract updates (review requirements 2 and 3)

- `_analyze_lastuse_block` / `compute_lastuse_release_points` /
  `recognize_materialized_releases` docstrings: "block-local family
  temp" → **"family temp with a FN-WIDE unique producer; the release
  is placed in the DRAIN block"** (qualification: every occurrence USE,
  not live-out of the drain block, producer anywhere in the fn).
- The fail-closed recognition message drops "block-local":
  "operand is not a family-producer String temp (fn-wide producer
  resolution)"; its tests update accordingly.
- Producer-map contract: duplicate dest during the fn-wide build →
  fail closed (AssertionError, SSA-contract violation); a temp with NO
  producer entry stays out of the family (unqualified), exactly like
  any non-family producer.
- `compute_string_temp_liveness` docstring (string_arc.py ~847, review
  item 2): the liveness-invariance argument goes stale — it currently
  says in-contract releases cannot change the result because "their
  temps are defined earlier in the same block."  Under cross-block
  releases the REFINED argument is: every in-contract release site is
  DOMINATED BY A USE of the same temp within the drain block (the
  release sits after the drain — after the last instruction occurrence,
  or at end-of-instructions for a terminator-drained temp whose
  terminator use the liveness walk also sees), so `block_use` already
  contains the temp before the release occurrence is reached and defs
  are untouched — live-in/out sets stay identical between pre- and
  post-materialization MIR.  The invariance CONCLUSION survives; the
  implementation must rewrite the argument.
- `is_materialized_release_family_producer` docstring: the caller-side
  condition note and membership prose lose their block-local framing
  (the producer may be in any block; dest String-typed-ness and the
  fn-wide lookup stay the caller's conditions).
- `string_releases.materialize_lastuse_releases` FUNCTION doc (not
  only the module doc): "block-local family temp" → fn-wide wording.
- `string_releases.py` module doc: family wording gains "fn-wide
  producer (TLR-7); release placed in the drain block"; the
  "cross-block excluded" sentences retire.
- The cross-block A/B pins REPLACE the four `*_cross_block_*_untouched`
  pins' expectations (they currently assert the pass is a NO-OP for
  cross-block temps — after TLR-7 the pass materializes them; those
  pins flip to asserting materialization, preserving their carriers).

## 5. Pin plan (review requirement 4 included)

A/B byte-identity (pass+arc ≡ arc-only), one pin per required shape:
- **straight-line cross-block**: producer block → single-pred chain →
  drain block;
- **branch join**: producer before a diamond, temp used ONLY at the
  join block → single release at the join drain (no per-branch
  releases — liveness keeps it live through both arms);
- **path-exclusive dual drains**: temp used in BOTH arms of a diamond,
  dead at the join → one release point PER ARM, no path executes two
  (the §3a proof's teeth);
- **bypass path (BLOCKING review addition — the §3c contract's
  teeth)**: producer before a diamond, temp used/drained in ONE arm
  only, the OTHER arm bypasses all uses, dead at the join → release
  ONLY in the use arm; NO release in the bypass arm or at the join;
  pass+arc output equals arc-only byte-for-byte.  This pins that TLR-7
  MIRRORS today's drain points and does not "fix" bypass paths
  accidentally (the release stays path-local; the bypass path's
  behavior — no release — is preserved as-is);
- **loop/backedge**: (a) per-iteration shape — Concat produced in a
  loop-body block, drained in a later body block, fresh each iteration
  → release inside the iteration (the 7,392 shape); (b) loop-carried
  NEGATIVE control — temp used again NEXT iteration (live through the
  backedge) → NO release anywhere inside the loop from either author;
- **consumed-before-exit**: temp produced in B1, CONSUMED (stored) in
  B2 → no release from either author;
- **live-out-to-terminator**: temp produced in B1, last-used by a
  non-Return terminator operand of B2 → point at len(instructions) of
  B2, release at the end of B2's instruction list;
- **flipped legacy pins**: the four existing cross-block-untouched
  pins (%x1 in the TLR-1 shim pin, xb/xc/t5x/t6x) flip from
  no-op/temp_lastuse expectations to materialized expectations;
- **misplaced/duplicated cross-block release** trips (placement
  validation in the drain block);
- heap memcheck row: a multi-block string-building LOOP (conditionals
  inside the body forcing the intra-loop block crossing) with
  runtime-built elements — missing release reads definitely-lost,
  double release reads Invalid free.

## 6. Expected corpus delta (acceptance, vs cleanup-tlr6)

- `temp_lastuse_release` 7,398 → **0** (per-shape sub-check: 7,392
  loop + 6 linear);
- `materialized_lastuse_release` 611,346 → **618,744** — the ENTIRE
  lifetime population under the dedicated authority;
- sum conserved; every other counter +0 (incl. events); universe
  identical; all nine hard gates zero; FULL memcheck STANDALONE.
- Stop triggers: any deviation; any non-TLR counter movement; A/B pin
  divergence; memcheck movement; recognition tripwire on any corpus
  fixture; duplicate-producer tripwire firing (would mean the SSA
  assumption is wrong — stop and report).

## 7. After TLR-7 (not in this slice)

With temp_lastuse_release corpus-zero, `_note_use`'s release arm joins
the 4a/4b tripwire ladder: fail-closed arm → clean cert cycle → delete
with 4a'/4b'.  The TLR-1 shim's classification split dies with the arm.
Path B (element-read churn elimination) remains an independent
optimization proposal.

## 8. Scratch hygiene (review requirement 5)

The TEMP-MEASURE edits used for §1 were restored via stored reverse
edits before this report was written: 3 edits reversed, zero
`TM_`/`_tm_` references in string_arc.py and
ownership_ledger_reporter.py, reporter battery 50/50.  The tree's only
uncommitted content is work/ documentation.

## 9. STOP

Awaiting review.  If accepted, TLR-7 implementation is the next slice:
fn-wide producer resolution + contract/wording updates + the §5 pin
ladder, exact −7,398/+7,398 acceptance, closing the temp_lastuse
population entirely.
