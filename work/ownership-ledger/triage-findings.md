# Phase 3A Task #5 Findings

Companion to the auto-generated `triage.md`.  This file holds the hand-analysis conclusions for each bucket — the auto-generated counts cannot reason about WHY a record is in a bucket, only HOW the rules placed it.

## Run summary

- **Cases compiled with the ledger flag:** 1031 (one per e2e test directory with `module X;` declared in `main.drift`).
- **Cases producing ledger records:** 442 (compiled successfully through the full pipeline including string_arc).
- **Cases that exit-non-zero:** 590 (the e2e suite includes many diagnostic-cases that intentionally fail compilation; expected).
- **Total records (with cross-case duplication):** 518,176.
- **Unique decisions (deduped by `site + fn_name + program_point + local`):** 9,757.
- **Agreement rate:** 89.9% (8,767 / 9,757).

## Bucket dispositions

### Bucket 1 — `per_field_gap` (360 records)

Confirms the design's known limitation: site 2 records the scrutinee-level decision under partial-move cleanup, but the ledger does not model per-field state.  Records are concentrated in `std.cli` and `std.json` — both of these libraries do destructure-and-move-fields heavily.  **Disposition: defer to 3B per-field tracking, do not act on individual entries.**

### Bucket 2 — `droppolicy_approximation` (540 records)

The Copy-trait short-circuit / DV-bearing divergence between `TypeTable.has_drop` and `DropPolicy.needs_drop`.  Heavily concentrated in `lang.atomic::_order_code` (a Copy-typed variant on which the site correctly skips drop but the ledger over-claims).  **Disposition: stays quarantined.**  The driver's `_needs_drop` callable in `driftc.py` already carries the explicit "DO NOT REUSE IN 3B" comment; the call site on the canonical pin (`test_ownership_ledger_three_quadrant_pin.py`) uses the real `DropPolicy.needs_drop`, so the gate's correctness is not vulnerable.  3B replaces this with a per-function DropPolicy accessor.

### Bucket 3 — `path_dependent` (29 records)

Direct input to the 3C design.  All 29 records concentrate in `main` functions on user-declared locals named `popped`, `popped2`, `popped3` at if/match join points.  Pattern: a local declared outside an if/match, conditionally assigned on some arms, scope-dropped at function end.  **None of the 29 are inside loop bodies** — the back-edge fixed-point case (3C "loop case 2") does not appear in the e2e set.  3C may ship with case-1-only handling and a hard fail-stop for case 2 if it ever surfaces; the implementation PR can decide based on this evidence.

### Bucket 4 — `semantic_equivalent` (41 records)

All on `__try_err*` synthesized try-error temps at try/catch join points.  These temps are zero-stored after error consumption (Tombstoned) but the site still emits a drop because its dataflow doesn't see the zero-store.  Drop on Tombstoned bytes is a runtime no-op.  **Disposition: tolerated.**  Phase 4 tombstone fusion will make these emissions explicit no-ops in MIR.

### Bucket 5 — `implicit_return_move_gap` (15 records)

Concentrated in `std.json::_parse_array` (10 on local `values`) and `std.json::_parse_object_throwing` (5 on local `fields`).  Both functions return their owned local via `Result::Ok(JsonNode::Array(move values))` / `Object(move fields)`.  HIR's `_moved_locals` knows about the move because the AST has an explicit `move` keyword; MIR represents it as `LoadLocal+Return` with no `MoveOut` instruction, so the ledger's transfer functions cannot infer the ownership transfer.  **Disposition: 3B/3C input.**  Two equally clean fixes:

1. **Widen the ledger's transfer-function table** — recognize `Return(value)` where `value` is the dest of a `LoadLocal(local)` AND `local` has owning type, transition `local → MovedOut`.  Cheap, narrow.
2. **Make MIR's return ownership explicit** — emit `MoveOut + DropValue?` shape at returns where the source local is drop-needing.  More invasive but cleaner long-term.

Recommend (1) for 3A polish (shrinks the bucket to 0 without MIR shape changes) and (2) as a 3B/Phase-4 thread.

### Bucket 6 — `real_disagreement` (5 records) — **GATE-BLOCKING**

All 5 records concentrate in **a single function and a single local: `std.json::_parse_object_throwing` / `fields`**, at five distinct error-return blocks inside the parser's `while true` loop body.  Site verdict: `MustNotDrop`.  Ledger verdict: `MustDrop`.  Site reason: `not_drop_needing` at site 3 (`string_arc_return`).

#### Why the disagreement matters

`_parse_object_throwing` (stdlib/std/json/json.drift:1131) declares:

```drift
var fields = containers.hash_map<...>();
```

then has multiple early-error-returns inside the loop body:

```drift
match _parse_string(text, idx) { core.Result::Err(e) => { return core.Result::Err(e); }, ... }
match _parse_value(text, idx)  { core.Result::Err(e) => { return core.Result::Err(e); }, ... }
if *idx >= n { return core.Result::Err(_err_parse(...)); }
if b ≠ ',' AND b ≠ '}' { return core.Result::Err(_err_parse(...)); }
```

None of these error returns consume `fields` — they return a freshly-constructed `Err(...)`.  The function also has *success-path* returns that DO consume `fields` via explicit `move`:

```drift
return core.Result::Ok(JsonNode::Object(move fields));   // empty-obj early return
return core.Result::Ok(JsonNode::Object(move fields));   // close-brace return
```

HIR's `_moved_locals` set is **not path-sensitive**: once `move fields` is lowered anywhere, the set contains `fields` for every subsequent scope-drop emission in the same function.  Site 1's records confirm: at the error-return blocks `_moved_locals` already contains `fields` (`reason="moved"` with site_verdict=`MustNotDrop`), even though the runtime control-flow path to those blocks did NOT execute either of the two `move fields` statements.

The same shape on site 3 (`string_arc_return`): site reports `not_drop_needing` because the string-arc dataflow's `initialized_at_return` set treats `fields` as not-initialized at those error-return points — likely because the move-tracking and the assigned-set computations interact in a way that excludes `fields` from `initialized_at_return` once it has been moved on any path.

The ledger, which IS path-sensitive, reports `Live` at these program points — and `MustDrop` therefore.

#### Verdict — likely a real leak in `_parse_object_throwing`

Strongly suggestive of an **actual leak of one HashMap allocation per error-return path** inside the parser loop.  The two passes' (`hir_to_mir`'s `_moved_locals` + `string_arc`'s `initialized_at_return`) joint decision is "no drop required" at error-return points; the ledger's path-sensitive view is the one that matches the source semantics.

This is precisely the kind of structural-weakness-in-per-pass-ownership-analysis the Phase 3 ledger rollout exists to retire.  It is also a surface that the existing memcheck / TLS-team validation may have missed because:
- The leak only fires on parse-failure paths (uncommon in healthy traffic).
- The leak is *one HashMap per malformed input*, not a recurring per-call leak.
- HashMap allocation is small compared to the surrounding String allocations the parser drops correctly.

#### Recommended actions before 3B

1. **Audit `_parse_object_throwing` directly.**  Either:
   - Confirm a real leak by writing a memcheck regression: feed a stream of malformed JSON inputs to the parser, observe HashMap allocation accumulation.  If confirmed, fix is a regular stdlib bug separate from the ledger rollout.
   - OR confirm a ledger gap by tracing why the ledger reports `Live` at points the per-pass analyses agree are post-move.  If gap, name the missing transfer function.
2. **Either way, bucket 6 must be cleared before 3B.**  If real leak: fix in stdlib, bucket 6 → 0 on re-run.  If ledger gap: extend transfer functions, bucket 6 → 0 on re-run.
3. **Do not silence by adjusting the bucket rules.**  Whichever side is wrong, the disagreement is a real signal — bucketing it away would defeat the purpose of the gate.

## Gate verdict

❌ **3A→3B gate is not yet satisfied.**  Bucket 6 has 5 records, all from one suspect site.  Resolution path is concrete (audit one function, one local) and not blocking other 3A work.

3C design is reviewed-and-pending; can begin implementation once the bucket 6 audit lands and the gate clears.

## Distribution highlights

- 442 / 1031 (43%) of cases compiled cleanly to the post-string-arc point and produced records.
- The other 590 cases failed at earlier stages (parser/checker), as expected — the e2e suite includes diagnostic-only cases.
- Records concentrated in `std.cli`, `std.json`, `std.atomic`, and various `main` functions — broadly representative.
- Total record volume (518K) compresses 53× under dedup (9.7K unique decisions), confirming the dedup key choice was right.
