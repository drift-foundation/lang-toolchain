# TLR-3 design checkpoint — StringConcat family extension (report only)

Status: DESIGN/CHECKPOINT ONLY — no implementation.  Prerequisite state:
TLR-2b landed and committed (15a5122d); phase reference
`build/tmp/cleanup-tlr2b` (every counter +0 vs cleanup-tlr2a-r2;
materialized_lastuse_release 286,424; temp_lastuse_release 332,320; all
nine hard gates zero).

## 1. Confirmed remaining counts (scratch remeasure on the TLR-2b tree)

One instrumented corpus run (TEMP-MEASURE producer tagging in
`_note_use`'s release arm + temporary reporter enumeration entries;
both files restored byte-identically afterward — zero `TM_` references,
reporter battery 38/38; scratch out `build/tmp/tlr3-measure`, exit 0,
universe identical 924/344/49, events 2,772,052 unchanged, materialized
286,424 unchanged):

| producer of the released temp | count | share of 332,320 |
|---|---|---|
| **StringConcat (TLR-3 target)** | **192,523** | **57.9%** |
| Call / CallIndirect / CallIface | 114,780 | 34.5% |
| CopyValue (string_stakes stakes) | 11,095 | 3.3% |
| none (producer outside the block) | 7,398 | 2.2% |
| StringFromUint/Float/Int/Bool | 6,479 | 1.9% |
| ExcGetParamsJson / ExcGetContextJson | 45 | ~0% |
| other / plain residue | 0 | — |

Sum: **332,320 exactly — lossless**, and bucket-for-bucket IDENTICAL to
the original TLR measurement (as the +0 ladder predicts).  The
`_note_use` release arm's ConstString branch is confirmed dead in
production (0 notes — every ConstString-family release now comes from
the pass via the recognition arm).

## 2. Which StringConcat temps qualify under the EXISTING contract

`compute_lastuse_release_points` qualifies a temp iff: producer in the
family, String-typed, not live-out, ≥1 occurrence, every occurrence
USE, no Return-terminator use.  Everything in that ladder except the
family predicate is already producer-agnostic:

- Concat OPERANDS carry USE dispositions (generic fallthrough — there
  is no dedicated StringConcat arm in `string_operand_dispositions` or
  the rewrite loop beyond owned-registration), so a Concat temp's
  occurrence walk is identical in kind to a ConstString temp's.
- The dominant shape is the CHAIN (`a + b + c`): `%c1 = Concat(a, b);
  %c2 = Concat(%c1, c)` — `%c1`'s only use is an operand of the next
  Concat → all-USE → qualified; its release sits immediately after
  `%c2`'s Concat, exactly where string_arc's in-pass emission puts it
  today (the TLR-2b A/B pin already carries one Concat temp released as
  temp_lastuse at that position in BOTH configs).
- Multi-use Concat results: several USE occurrences → drains at the
  last → ONE release (multiplicity rule, already pinned at the
  occurrence level).
- Consumed Concat results (stored / returned / by-value call arg):
  CONSUME occurrence → disqualified → string_arc moves them — unchanged.
- Cross-block Concat temps (part of the 7,398 `none` bucket when used
  elsewhere): producer map is per-block → out of the family → untouched
  (stays temp_lastuse via string_arc's own bookkeeping).

So: the qualified StringConcat population is exactly the 192,523
measured releases — the same `_note_use` release-arm population the
tagging counted, by the same equivalence argued (and pinned) in TLR-2a:
release-arm firing ⇔ all-counted-occurrences-USE + owned + not-live-out.

## 3. Mechanically safe, or a separate producer-family rule?

**Mechanically safe — ONE parameterized family constant, no separate
rule.**  The analysis logic never inspects the producer beyond the
shape predicate.  Concretely, the extension is:

1. `MATERIALIZED_RELEASE_FAMILY = (M.ConstString,)` →
   `(M.ConstString, M.StringConcat)` — a NEW module-level constant in
   `string_arc.py`, consumed by BOTH the analysis shape predicate
   (`_is_conststring_temp` → `_is_family_temp`, isinstance against the
   constant) and the TLR-1 shim's classification split.  Single source;
   the shim and the pass cannot disagree by construction.  (The shim's
   Concat branch becomes dead-in-production exactly like its ConstString
   branch — it still classifies correctly in arc-only unit runs, which
   is what the A/B pins compare.)
2. `materialize_lastuse_releases` — NO code change beyond the constant
   (it consumes the calculator); docstring family wording updated.
3. string_arc suppression arms: the ConstString rewrite arm already
   guards on the recognized set; the sibling
   `elif isinstance(instr, (M.StringFrom*, M.StringConcat)):
   owned_values.add(instr.dest)` arm needs the SAME recognized guard
   (the fn-wide `owned_defs` subtraction already covers the seed half —
   that subtraction is set-driven and family-agnostic).
4. Recognition strictness extends automatically: in-contract shape
   becomes "producer in the family"; a StringRelease of a Call-produced
   temp still trips (that family arrives in TLR-4, not before).

Proof obligations that are NEW for Concat (all already handled by the
TLR-2b machinery, to be pinned):
- **Interleaved chain groups:** in `%c2 = Concat(%c1, d)`, the drain
  group at `%c2` contains `%c1` (Concat temp) AND `%d` (ConstString
  temp) — cross-FAMILY same-group releases.  The gap/placement
  validation is set-driven (recognized set is the family union), and
  the drain-order rule (last-occurrence position in the draining
  instruction's `iter_used_values` walk) is family-agnostic: order is
  `%c1` then `%d`, matching `_note_use`'s decrement sequence.
- **A qualified temp's release sitting between another qualified temp's
  PRODUCER and its first use** (chain shape: `Release(%a); Release(%b)`
  after `%c1`'s producing Concat) — already the TLR-2b A/B pin's
  layout; unchanged by the family extension.
- Liveness, ledger (StringRelease has no transfer arm), idempotence:
  family-independent arguments, unchanged.

What is NOT in TLR-3 (flagged ahead): the Call-result family (TLR-4)
introduces `can_throw` — a call's result temp only exists on the
non-throw path, and the throw edge changes the block topology the
points are computed over; that family needs its own design gate.  The
CopyValue family rides behind the stake-precision sub-investigation
(11,095 retain+release churn pairs that possibly shouldn't exist at
all).  The cross-block `none` tail needs the careful lifetime analysis
the measurement flagged.

## 4. Expected corpus delta (acceptance, vs cleanup-tlr2b)

- `temp_lastuse_release` 332,320 → **139,797** (−192,523);
- `materialized_lastuse_release` 286,424 → **478,947** (+192,523);
- **sum conserved exactly (618,744 lifetime total)**; the transfer must
  be EXACTLY the measured bucket — any deviation is a finding, not a
  tolerance;
- every other counter +0 (incl. `events` 2,772,052); universe identical
  924/344/49; all nine hard gates zero;
- FULL memcheck in gate (98 + 1 skip expected) — with the pass live,
  heap-string concat chains are the double-release (Invalid free) /
  missing-release (definitely-lost) detector;
- output MIR byte-identical for the migrated family (A/B pins); the
  corpus-scale check is `events` + the exact transfer.

## 5. Regression plan (pins)

A/B byte-identity pins (pass+arc vs arc-only), extending the TLR-2b
battery with Concat-specific shapes:
- **Chain:** `%c1 = Concat(%a, %b); %c2 = Concat(%c1, %d);
  StringEq(%e, %c2, %c2)` — releases: `%a`,`%b` after the first Concat;
  `%c1`,`%d` after the second (cross-family same-group order rule);
  `%c2` after the StringEq.  All materialized in config B; byte-identical
  to config A.
- **Multi-use Concat result:** used twice (two StringEq) → ONE release
  after the second use.
- **Repeated-operand:** `StringEq(%cc, %cc)` draining a Concat temp →
  one release (already present in the 2b pin — asserted classification
  flips from temp_lastuse to materialized).
- **Consumed Concat result:** `StoreLocal(x, %cc)` → no release from
  EITHER author (move path unchanged).
- **Double-release direction:** idempotence re-run with the extended
  family; strict recognition (a duplicate or misplaced Concat-temp
  release trips `unexpected input release`) — extend the rejection pin
  with a Concat carrier; memcheck heap rows.
- **Missing-release direction:** the A/B equality itself (config A
  emits, so config B must); corpus deficit fails the exact-delta gate;
  memcheck definitely-lost.
- **Cross-block Concat temp:** pass no-op; stays temp_lastuse; configs
  byte-identical.
- Conformance pin update: the existing calculator-vs-live-pass pin's
  Concat temp moves from the "not in the family" column to the
  qualified column — the pin's assertion flips accordingly (this is the
  contract-level record of the family extension).

Batteries: reporter suite; stage2 + ledger-cache guardrails; FULL
memcheck; corpus exact-delta signature above.

## 6. Stop triggers (implementation slice)

Transfer ≠ 192,523 in either direction; any non-TLR counter movement
(incl. `events`); any hard-gate movement; A/B pin divergence; memcheck
movement; the recognition tripwire firing on any corpus fixture; any
arm found consulting producer identity beyond the family constant
(that would mean the "one constant" claim is wrong — stop and report).

## 7. STOP

Awaiting review.  If accepted, implementation is the next reviewed
slice: the family constant + shim/arm updates + pins, single slice, no
intermediate shim step (the TLR-1-style shim's decoupling purpose is
spent now that the TLR-2b machinery is proven and the shim/pass share
the constant — recorded here as the deliberate ladder deviation).
