# temp_lastuse_release measurement checkpoint (report-only)

Status: STOP/REPORT — no implementation. Baseline: the committed
`build/tmp/cleanup-4b` reference (618,744 temp_lastuse_release). Method:
one scratch corpus run with a temporary producer-tagging instrumentation
(both files restored byte-identically afterward — `git diff` empty,
reporter battery 26/26); the scratch run itself: universe identical
924/344/49, exit 0, all nine hard gates zero, and the tagged buckets sum
to EXACTLY 618,744 (lossless).

## 1. Split by emission path — settled BY CONSTRUCTION

**100% `_note_use`, 0% `_ensure_owned`.** Post-4b, any proven-String value
reaching `_ensure_owned` unconditionally trips the dead-stake wire after
the (possible) release — a firing fails the compile and would have changed
the corpus partition. The green 4b acceptance therefore proves zero
`_ensure_owned` releases corpus-wide.

Corollary for 4b′: `_ensure_owned`'s release arm is DEAD-IN-EFFECT (it can
only execute en route to a tripwire) and joins the deletion inventory with
the retain arms. (The 4b in-code comment calls that bookkeeping "live" —
true pre-4b via this path; the arm's only remaining executions are doomed.
Correct the comment when 4b′ lands.)

## 2. Which `_note_use` sites can release — 2 of 37

A CONSUMING use of an owned temp discards it from `owned_values` and
returns before the release check — so the last-use release can only fire
at `consume=False` sites:
- the **generic instruction fallthrough** (every instruction without a
  dedicated string-consuming arm notes its String operands non-consuming)
  — the dominant site;
- **non-Return terminator operands** (rare; String terminator operands
  barely exist).

Semantics of the class, precisely: OWNED String temps (creator results)
whose LAST use is NON-consuming — concat/compare/inspect-style operands.
The dominant stdlib pattern: `a + b + c` chains (each intermediate concat
result is a fallthrough operand of the next concat) and ConstString
operands of concats/comparisons.

## 3. Producer histogram (scratch run; sums to 618,744)

| producer of the released temp | count | share |
|---|---|---|
| ConstString | 286,424 | 46.3% |
| StringConcat | 192,523 | 31.1% |
| Call (String-returning results) | 114,780 | 18.6% |
| CopyValue (string_stakes stakes!) | 11,095 | 1.8% |
| none (producer outside the block — per-block producer map) | 7,398 | 1.2% |
| StringFromUint/Float/Int/Bool | 6,479 | 1.0% |
| ExcGetParamsJson / ExcGetContextJson | 45 | ~0% |

Sub-finding worth its own look during the migration: the 11,095
**CopyValue** rows are string_stakes' own `.stake` temps whose last use
was NON-consuming — i.e. a stake was materialized for a consumer that
never moved it. Not a leak (the release balances it) but a stake-precision
signal: those stakes are churn (retain+release pairs) that a tighter
`_is_string_value_view`/consumer analysis could avoid emitting at all.

## 4. Ledger representation — NOT represented; a new authority is needed

The ledger tracks NAMED locals only (`tracked_locals = params + locals`);
SSA temp lifetimes exist nowhere but string_arc's private per-block
use-count bookkeeping (`use_counts`/`owned_values`/`live_out`). Neither
cleanup_authoring nor drop_flags models temps. Migration therefore cannot
"move" this class into an existing authority — it needs a **dedicated
materialization authority** (the release sibling of `string_stakes`),
computed from the same last-use analysis, so string_arc's bookkeeping
goes corpus-zero → tripwire → delete (the established 4a/4b ladder).
PLACEMENT of that authority is decided by the TLR-2 design gate — the
"pre-ledger" placement this section originally named was REJECTED in
review (it changes the ledger snapshot and downstream inputs); the
current plan is a LATE pass immediately before string_arc (see the
"TLR-2 preview" in the revised design section). Note the per-block
`none` bucket (7,398): the current producer map is per-block, and
cross-block temp lifetimes are exactly where a standalone pass must be
more careful than the current in-pass bookkeeping.

## 5. ~~Proposed smallest first migration slice~~ — SUPERSEDED (2026-07-14)

**This section's pre-ledger-pass sketch is SUPERSEDED by the reviewed
design in "TLR-1 REVISED DESIGN" below (option B: in-string_arc shim).**
It is retained only as the record of what review rejected — the
"pre-ledger pass + byte-identical" combination was contradictory. Do not
implement from this section.

**Slice TLR-1: materialize last-use releases for BLOCK-LOCAL ConstString
temps only** (the largest and simplest-lifetime family, 286,424 minus its
cross-block tail):
- a pre-ledger pass emits `StringRelease(temp)` immediately after the
  temp's last use, tagged with a NEW site class
  (`materialized_lastuse_release`) — same instruction, same point, new
  author;
- string_arc's `_note_use` bookkeeping stays as-is and naturally emits
  NOTHING for those temps (their counts are consumed by the pass's
  explicit release — the mechanism must decrement/skip exactly like the
  in-pass release did);
- scope strictly: producer is ConstString AND every use in the producing
  block (the pass skips anything else — cross-block, other producers —
  leaving them to the existing bookkeeping).

Expected acceptance:
- `temp_lastuse_release` 618,744 → 618,744 − N; NEW
  `materialized_lastuse_release` = +N; **sum conserved exactly** (N ≈
  280k, measured at implementation time);
- every OTHER counter +0; universe identical; all nine gates zero;
- runtime byte-equivalence goal: the release lands at the same
  instruction position — the emitted MIR sequence per block should be
  IDENTICAL (assert via an output-MIR pin), making memcheck a formality
  (still in gate).

Regression plan: output-MIR identity pin for a representative concat
chain; conservation pin (audit-level: per-fn sum of the two classes
unchanged); memcheck full suite; corpus exact-delta signature above.

Stop triggers: any non-conserved delta; any movement in a non-TLR
counter; any memcheck movement; cross-block or non-ConstString temps
touched by the pass (out of scope by definition); implementation
discovers the fallthrough-note interplay cannot be made byte-identical.

Follow-on ladder (post TLR-1, each measured): StringConcat family →
Call-result family → StringFrom*/Exc* → cross-block tail (needs the
careful lifetime analysis) → tripwire `_note_use`'s release arm → delete
with 4a′/4b′.

## 6. NOT in scope, reaffirmed

4a′/4b′ tripwire-branch deletion still waits for a clean cert cycle with
zero firings (per instruction).

## 7. STOP

Awaiting approval of TLR-1 before any implementation.

---

## TLR-1 REVISED DESIGN (2026-07-14, per review — supersedes §5's sketch)

The review blocker was real: §5 said "pre-ledger pass" while promising
byte-identical placement — a pre-ledger pass changes the ledger snapshot
and every downstream input. The revision picks **option B**: an
IN-string_arc materialization SHIM first, proving the classification and
counter split with zero pipeline impact; extraction to a real pass is a
LATER slice (TLR-2) with its own design gate.

### What TLR-1 actually is (option B, precisely)

A classification split at the single `_note_use` release point — NOT a new
author, NOT a new pass, NOT a moved instruction:

- at the moment the last-use release fires, compute the QUALIFICATION
  PREDICATE the future pass will own: `producers.get(val)` is a
  `ConstString` (the per-block producer map already guarantees the
  producer is co-block with the release, and the existing `live_out`
  guard already guarantees the temp is dead after this block — so
  "block-local ConstString" is exactly `isinstance(producers.get(val),
  M.ConstString)` at this point, no new analysis);
- qualified → the audit note is tagged with the NEW
  `SITE_CLASS_MATERIALIZED_LASTUSE_RELEASE`; unqualified → today's
  `SITE_CLASS_TEMP_LASTUSE_RELEASE`;
- the emitted `M.StringRelease` is THE SAME instruction on THE SAME code
  path at THE SAME position, unconditionally.

### The five review questions, answered for option B

1. **Pass order / ledger rebuilds:** UNCHANGED — no new pass exists; no
   MIR mutation beyond what string_arc already does; no ledger is built,
   rebuilt, or consulted differently. The snapshot string_arc sees is
   bit-for-bit today's.
2. **Double-release avoidance:** vacuous by design — there is exactly ONE
   author (string_arc's existing release point); the shim only renames the
   audit tag. The double-release question becomes real only in TLR-2 and
   is part of its design gate (string_arc must recognize pre-materialized
   releases and suppress its own bookkeeping for those temps).
3. **Output MIR:** BYTE-IDENTICAL, by identity — the instruction-emitting
   code is untouched; only the audit-tag expression (env-gated, audit-only)
   differs. Pinned by an output-MIR sequence assertion, and corpus-proven
   (`events` and every non-TLR counter +0).
4. **Recording the new event without moving unrelated counters:**
   `SITE_CLASS_MATERIALIZED_LASTUSE_RELEASE` joins the closed
   STRING_ARC_SITE_CLASSES enumeration (so it is not UNTAGGED) and the
   finalize `_counted_only` set (so the UNCLASSIFIED sweep ignores it,
   exactly like temp_lastuse_release). It is RELEASE-kind, so C2/C3 never
   see it; C1 filters on scope_exit_release only. The `events` total is
   unchanged (same number of notes, different tag).
5. **Smallest non-vacuous handshake pin:** one synthetic fn, three temps,
   asserting the SPLIT in both directions plus emission identity:
   - `%c1`, `%c2` — ConstString temps whose only use is a `StringEq`
     (a generic-fallthrough, NON-consuming consumer) → both releases tagged
     `materialized_lastuse_release`;
   - `%cc` — a StringConcat result used the same way → its release stays
     `temp_lastuse_release` (producer not ConstString);
   - a second-block ConstString last-used there → stays
     `temp_lastuse_release` (producer map is per-block: cross-block
     producers resolve to none);
   - output-MIR assertion: each `StringRelease` sits immediately after its
     temp's last-use instruction — the same positions as pre-shim.

### Expected corpus acceptance (vs cleanup-4b)

- `temp_lastuse_release` 618,744 → 332,320 expected
  (618,744 − 286,424); `materialized_lastuse_release` +286,424 — the
  measured ConstString bucket should transfer EXACTLY (its co-block
  properties are already implied by the mechanics; any deviation is a
  finding, not a tolerance);
- **sum conserved exactly**; every other counter +0; `events` +0;
  universe identical; all nine hard gates zero;
- memcheck full suite in gate (formality — emission untouched — but the
  standing rule applies).

### Stop triggers

Non-conservation of the sum; any non-TLR counter movement (incl.
`events`); the ConstString transfer ≠ 286,424 without an explained,
itemized cause; any output-MIR difference in the identity pin; memcheck
movement.

### TLR-2 preview (extraction — SEPARATE design gate before code)

Placement A: a LATE pass immediately before string_arc (AFTER drop_flags /
cleanup_authoring / stakes and the post-cleanup ledger rebuild), emitting
the qualified releases as explicit MIR; ledger rebuilt AFTER it (per the
ledger-cache rule) — equivalence argument required (temp releases don't
change tracked-local states; boundary points shift indices only);
string_arc handshake: recognize a pre-materialized `StringRelease(temp)`
and mark the temp consumed in its bookkeeping so `_note_use` cannot
re-release (the double-release question lives HERE). Output MIR for TLR-2
is BEHAVIOR-equivalent, not byte-identical (insertion interleaving with
string_arc's other rewrites) — which is exactly why TLR-1's byte-identical
shim must land first and hold the counters steady.
