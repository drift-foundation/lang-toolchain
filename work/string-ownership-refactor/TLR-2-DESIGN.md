# TLR-2 design checkpoint — extracting the materialized-release authority

REVISION 3 (2026-07-14): records the THREE-WAY operand disposition
(CONSUME / USE / IGNORE) as the actual extracted contract — see §0a.
TLR-2a implementation surfaced that the binary consuming/non-consuming
framing of revisions 1–2 was incomplete; the disposition table below is
what TLR-2b consumes.

REVISION 2 (2026-07-14): incorporated the three review findings —
recognition-before-use-counting, the occurrence-level multiplicity rule
(§3a), and the second shared contract (the release-point calculator).

Status: DESIGN ONLY — no implementation. Prerequisite state: TLR-1 landed
(`cleanup-tlr1` reference: materialized_lastuse_release = 286,424,
temp_lastuse_release = 332,320); 4a′/4b′ deletion stays parked until a
clean cert cycle with zero tripwire firings.

## 0. The honest hard part, first

The pass must decide "this block-local ConstString temp's last use is
NON-consuming, so it needs a release" — but *consuming-ness is defined by
string_arc's consumer arms* (store/call/ctor/return/drop arms treat String
operands as moved; the generic fallthrough treats them as non-consuming).
A pass that re-implements that classification drifts. The design therefore
splits TLR-2 in two:

- **TLR-2a (pure refactor, corpus +0):** extract TWO shared contracts
  (review finding: the predicate alone is necessary but not sufficient):
  1. `consumes_string_operand(instr, operand, *, fn_infos, type_table)`
     — the per-operand consuming/non-consuming classification.  (As
     implemented, string_arc's arms are deliberately left UNCHANGED in
     2a — rewriting them to consult the predicate would risk behavior
     drift in the very slice meant to be a pure extraction; agreement
     is enforced by the conformance pins instead, and the
     two-implementation window closes when the last family migrates.)
  2. `compute_lastuse_release_points(block, *, fn_infos, type_table)`
     — the OCCURRENCE-LEVEL release-point calculator: for each qualified
     temp, the index of the instruction at which its occurrence count
     drains to zero (the multiplicity rule in §3a is part of this
     contract).  A PURE function over the input block, built on
     contract 1; it does not mutate anything.
  string_arc's incremental bookkeeping is NOT replaced in 2a (that
  two-implementation window is inherent to the migrate-by-family ladder
  and closes when the last family migrates and the release arm is
  tripwired); instead 2a adds a CONFORMANCE PIN asserting the
  calculator's output equals where string_arc actually emits, on
  synthetic MIR including the repeated-operand case.  Corpus acceptance:
  every counter +0 vs cleanup-tlr1 BEFORE any new author exists.
- **TLR-2b (the extraction):** the new pass consumes contract 2 (never
  its own reimplementation); the conformance pin from 2a becomes the
  standing agreement proof, and the corpus exact-delta at each family
  boundary is the corpus-scale version of it.

## 0a. The actual contract (rev 3): a THREE-WAY disposition, not a binary

Implementing 2a showed that `consumes_string_operand`'s yes/no answer is
NOT the whole classification: string_arc's rewrite arms give each String
operand occurrence one of THREE dispositions, and the calculator must
reproduce all three or it invents phantom release points. As extracted
(module-level in `string_arc.py`):

- `string_operand_dispositions(instr, *, local_types, fn_infos,
  type_table)` → `[(operand, disposition)]` per occurrence, where
  disposition ∈ {`DISPOSITION_CONSUME`, `DISPOSITION_USE`,
  `DISPOSITION_IGNORE`}:
  - **CONSUME** — the arm moves the operand (store/call-by-value with
    String param/ctor field/Exc selected slot/Return value/DropValue…):
    ownership transfers; the temp is discarded from bookkeeping; no
    release ever.
  - **USE** — the arm notes the operand non-consumingly (the generic
    fallthrough — StringEq/Concat-source/inspect-style — and non-Return
    terminator operands): the occurrence counts toward the last-use
    drain; the release fires when the count reaches zero.
  - **IGNORE** — handled arms that neither consume nor note the operand:
    ref-position (`&String`) call args, args at non-String by-value
    params, info-less indirect calls, ctor/Exc operands at non-selected
    slots, ErrorRaise.  Call-param classification (all three call arms)
    uses the SEMANTIC String test — `TypeKind.SCALAR && name ==
    "String"`, mirroring the rewrite arms' `_param_is_string` — NOT raw
    TypeId equality: String param TypeIds are not canonical across the
    package/type-table boundary (the string_stakes lesson), and a
    raw-equality table would classify a semantically-String by-value
    arg IGNORE while the live arm consumes it.  IGNORE still
    disqualifies the temp from release-point output, so this is not a
    phantom-release path today — the real risk is CONTRACT DRIFT:
    `consumes_string_operand` would lie relative to the live arm, and
    future users of the predicate (the 2b pass, family migrations)
    would decide wrongly (pinned with a `new_scalar("String")` carrier
    across Call/CallIndirect/CallIface). Counted by the PRESCAN (it walks
    `iter_used_values`, which sees every occurrence) but never drained
    by the rewrite → the count never reaches zero → NO release. A
    calculator that treats IGNORE as USE emits a release string_arc
    never emits (phantom); one that skips IGNORE in prescan mis-drains
    other occurrences.
- `consumes_string_operand(...)` remains as the CONSUME projection of
  the disposition table (kept for arm-site readability).
- `compute_lastuse_release_points(...)` (contract 2) is built on the
  full three-way table: a temp qualifies only if EVERY occurrence is
  USE; any CONSUME or IGNORE occurrence disqualifies it.

A third shared piece rounds out the contract set (review finding on
2a): `seed_string_dest_types(blocks_in_order, local_types, *, fn_infos,
type_table)` — the type-seeding rules for String-producing dests
(ConstString/Concat/Retain/StringFrom* → String; AssignSSA/Phi
propagation; Call via fn_infos or the drift_string_* symbol list;
CallIndirect/CallIface via `user_ret_type`), extracted from the private
`_seed_dest_types` closure (which now delegates). Production MIR does
NOT carry types for every temp, so the 2b pass MUST run this seeder on
its own copy of `local_types` before calling the calculator — pinned
with a production-like un-seeded func (bare calculator sees nothing;
seeder+calculator agrees with the live pass).

Reachability note, recorded so 2b doesn't re-litigate it: the
ctor/Exc-non-selected-slot and ErrorRaise IGNORE rows are unreachable
for String operands in WELL-TYPED MIR (a String value cannot occupy a
non-String slot); they exist for totality. The constructible IGNOREs —
ref-position args and non-String by-value params via instruction-carried
`param_types` — are pinned, including the mixed
"IGNORE occurrence + later USE" case (no release from either author).

## 1. Pass placement (exact)

`materialize_lastuse_releases(func, ...)` (new module,
`lang/driftc/stage2/string_releases.py`) runs in `compile_stubbed_funcs`'s
per-fn pipeline:

```
drop_flags → cleanup_authoring → materialize_call_arg_stakes
    → materialize_lastuse_releases   (NEW)
    → _ol_build_and_attach           (the existing per-fn ledger build)
    → insert_string_arc
```

i.e. a LATE pass immediately before string_arc, and — the key placement
decision — **before the existing per-fn ledger build**, so no EXTRA
rebuild is added: the one ledger string_arc consumes is built on the
post-materialization MIR. The pass calls `mark_ledger_dirty` and carries
the ledger-cache-safety audit markers like every mutating pass.

## 2. Ledger rebuild & why index shifts are safe

No rebuild AFTER the pass is needed because the build already happens
after it (§1). Safety argument for the shifted snapshot:
- `StringRelease(%temp)` has no `local` operand and no transfer-function
  arm in the ledger walker — it cannot change any TRACKED local's state.
  Every `block_in`/`post_instr` STATE at corresponding program points is
  therefore identical to today's; only the `(block, idx)` keys shift by
  the number of releases inserted earlier in the block.
- All consumers of those keys (string_arc's boundary verdicts, site-4,
  the audit's pre/post anchors, L_post) read the SAME MIR the ledger was
  built on — internally consistent by the standing ledger-cache rule.
- The corpus counters are position-independent, so shifted anchors cannot
  move any counter.

## 3. The string_arc handshake (recognition + suppression)

The pass emits `StringRelease(%t)` immediately after `%t`'s last use, for
temps satisfying: producer is a ConstString in the same block, every use
non-consuming per the SHARED predicate, temp not in `live_out`.

string_arc recognizes and suppresses as follows — with the ORDERING
rule the review flagged as the load-bearing detail:

- **Recognition happens BEFORE use counting (review finding 1).**
  `StringRelease(%t)` is a use per `_iter_used_values`, so an
  unrecognized materialized release would inflate `%t`'s prescan count
  and MOVE the last-use point string_arc computes.  The prescan
  therefore classifies each instruction FIRST: an in-contract
  `StringRelease(%t)` marks `%t` EXTERNALLY-RELEASED and is EXCLUDED
  from use counting entirely — its operand contributes no count.
  **In-contract requires BOTH halves (review-hardened):** SHAPE —
  `%t`'s producer is a block-local ConstString — AND **PLACEMENT** —
  it is the UNIQUE `StringRelease(%t)` in the block, `%t`'s remaining
  occurrences are all USE, and the release sits after the draining
  instruction `compute_lastuse_release_points` computes, separated
  only by in-contract releases of temps draining at the SAME
  instruction (2b implementation finding, caught by the A/B pin:
  same-drain-group temps release CONSECUTIVELY — `StringEq(%p, %r)`
  yields `Release(%p); Release(%r)` — so strict `drain+1` rejects the
  k-th member; a gap containing ANY non-release instruction still
  rejects).  Shape is also enforced strictly: ANY input StringRelease
  whose operand is not a block-local ConstString raises (no author
  other than the 2b pass may emit pre-string_arc releases).
  Shape alone is too broad: a release placed BEFORE a later use would
  still be excluded from counting and would suppress string_arc's own
  release — turning a TLR-2b emission bug into a silent
  use-after-release (or, misplaced late, a missing release).  A
  shape-matching release that fails placement is fail-closed with the
  distinct `unexpected input release` tag (implemented and pinned in
  2a: mis-placed-before-later-use and duplicate-release cases both
  raise) — the handshake is a verified contract, not trust.
- **The rewrite loop skips bookkeeping for recognized releases
  symmetrically:** the instruction is copied through verbatim with NO
  `_note_use` on its operand (not the fallthrough note the original
  design hand-waved — the counts never included this occurrence, so no
  decrement may happen either).  Prescan and rewrite see the same
  numbers by construction.
- **§3a — multiplicity rule (review finding 2, part of contract 2):** a
  temp may occur MORE THAN ONCE in one non-consuming instruction
  (`StringEq(%c, %c)`).  string_arc's draining semantics emit ONE
  release after the instruction at which the occurrence count reaches
  zero.  The calculator and the pass MUST reproduce exactly that:
  exactly one release per temp, placed AFTER the draining instruction,
  never one per operand occurrence and never before the instruction.
  The conformance pin includes the repeated-operand case.
- **Suppression:** externally-released temps are never added to
  `owned_values` at their ConstString producer. `_note_use`'s release
  requires ownership, so it structurally CANNOT emit a second release —
  double-release prevention by construction. (Qualified temps have zero
  consuming uses by definition, so the owned-exclusion cannot change any
  move decision.)  2b implementation finding (caught by the A/B pin):
  the ConstString rewrite arm is only HALF the ownership registration —
  `owned_values` is seeded per block from the fn-wide `owned_defs`
  prepass, which registers every ConstString dest, so recognition must
  also subtract the recognized set from that seed
  (`owned_values -= recognized_released`); without it, a second release
  at the drain.
- **Audit accounting:** the recognition arm notes the event
  (RELEASE-kind, `materialized_lastuse_release`, anchored at the
  release's position) as it copies it through — the counter keeps its
  author-independent meaning and `events` stays constant.

## 4. Expected corpus delta (TLR-2b acceptance)

**Everything +0 vs cleanup-tlr1**, exactly:
- materialized_lastuse_release stays 286,424 (same events, new author —
  noted at the recognition arm instead of the emission arm);
- temp_lastuse_release stays 332,320;
- events unchanged; every other counter +0; universe identical; all nine
  hard gates zero.
TLR-2a has the identical all-+0 signature (pure refactor). Any deviation
in either sub-slice is a stop trigger, not a tolerance.

## 5. Output MIR: behavior-equivalent, with a byte-identical stretch check

Committed claim: BEHAVIOR-EQUIVALENT. The materialized release occupies
the same position string_arc's inline emission used (immediately after
the last-use instruction), and fallthrough instructions are copied
verbatim, so byte-identity is PLAUSIBLE — but string_arc's other
rewrites interleave in the same blocks and the design does not stake the
slice on instruction-stream equality. Stretch verification at
implementation time: a scratch A/B harness compiling a fixture sample
with the pass on/off and diffing emitted IR; if it comes back identical,
record it — if not, itemize the interleaving differences in the report
(they must all be reorderings of independent instructions, never a
different instruction SET).

## 6. Regression plan

- **Double-release direction:** by-construction suppression (§3) + the
  contract tripwire + full memcheck (an actual double release reads as
  Invalid free on heap strings) + the corpus surplus check
  (materialized > 286,424 fails the exact-delta gate).
- **Missing-release direction:** conformance pin over contract 2
  (calculator-vs-string_arc agreement on one synthetic MIR containing:
  qualified temps, a REPEATED-OPERAND case `StringEq(%c, %c)` — exactly
  one release, after the instruction — a consumed single-use ConstString
  which must NOT be released by either author, a Concat-produced temp,
  and a cross-block temp) + memcheck heap-string rows (a missed release
  reads as definitely-lost) + the corpus deficit check
  (materialized < 286,424 or temp_lastuse movement fails the gate).
- **Prescan-exclusion pin (finding 1):** a block containing an
  in-contract materialized release followed by NO other use of the temp
  must leave every OTHER temp's release point unchanged versus the same
  block without the materialized release — proving recognized releases
  contribute nothing to use counts.
- **Handshake pins:** externally-released temp → string_arc emits
  nothing for it and notes the materialized class at the recognition
  arm; out-of-contract input StringRelease → tripwire with the
  structured message.
- Batteries: reporter suite; stage2 + guardrails; FULL memcheck in gate
  (both sub-slices — 2b is an emission-author change even though the
  instruction stream is equivalent).

## 7. Stop triggers

- TLR-2a: ANY counter delta vs cleanup-tlr1; any string_arc arm whose
  rewrite to the shared predicate is not behavior-preserving by
  inspection (i.e. the predicate needs a per-arm special case — that's a
  finding about the classification, report before proceeding).
- TLR-2b: any counter delta (incl. materialized ≠ 286,424 in either
  direction); conformance pin disagreement; any memcheck movement; the
  contract tripwire firing on any corpus fixture; the A/B stretch check
  revealing a different instruction SET (reorderings are reportable,
  set-differences are stoppers).

## 8. Out of scope, reaffirmed

- TLR-2 does NOT extend the qualified family (ConstString/block-local
  only — Concat/Call/StringFrom families are TLR-3+ per the measured
  ladder).
- 4a′/4b′ tripwire-branch deletion stays parked until a clean cert cycle
  with zero firings.
- The `_note_use` release arm itself is NOT tripwired in TLR-2 (that
  comes when the LAST family migrates and the arm goes corpus-zero).

## 9. STOP

Awaiting approval of the TLR-2a / TLR-2b split and this design before
any code.
