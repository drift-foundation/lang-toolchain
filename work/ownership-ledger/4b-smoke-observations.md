# 4b Smoke-Test Observations

Source: compiling a minimal `module main { fn pick(b: Bool) -> String; fn main() }` program with `DRIFT_COMPILER_DEBUG='{"ownership_ledger":true}'`.  Pulls in stdlib for the String type, so the run covers a meaningful slice of `std.cli`, `std.atomic`, etc.

## Verification status

- 1148 stderr lines total; 1147 records (one is the lane header).
- All 1147 lines parse as valid JSON after the `[drift:ownership_ledger] ` prefix.
- All required fields present on every record (`fn_name`, `site`, `program_point`, `local`, `site_verdict`, `site_reason`, `ledger_verdict`, `raw_state`, `classification`).

## Site × classification breakdown

| site | agree | ledger_stricter | site_stricter | semantic_equiv | total | agree % |
|---|---|---|---|---|---|---|
| `drop_before_overwrite` | 49 | 0 | 0 | 0 | 49 | 100 |
| `scope_drop` | 525 | 9 | 11 | 0 | 545 | 96 |
| `string_arc_return` | 316 | 5 | 6 | 1 | 328 | 96 |
| `match_cleanup` | 37 | 38 | 150 | 0 | 225 | 16 |

## Noise classes already visible

1. **`match_cleanup` is the dominant noise source** (84 % non-agree).  Site 2 records the scrutinee-level decision in the partial-move-cleanup branch, but the ledger sees the scrutinee as still `Live` — partial-move semantics are conceptual, not represented in MIR.  This is the per-field-state limitation called out in the design's "Open questions" section.  Not a real leak; not a real ledger bug.  3B/3C will add per-field tracking; for 3A this site's records are largely uninterpretable and the triage should bucket them as a known gap rather than chase individual entries.
2. **`scope_drop` and `string_arc_return` carry a small noise class** (~20 records combined).  Likely from the driver-side `needs_drop` approximation that bypasses `DropPolicy.needs_drop` (the Copy-trait / DV-bearing short-circuit).  This is the "needs_drop approximation" called out in the driver-side comment in `driftc.py`.  Triage should confirm by spot-checking whether the underlying types have `copy_status=True`.
3. **`drop_before_overwrite` is a clean signal**.  Site 4 is fully expressible at the ledger's current granularity; 100 % agreement on this smoke run, so disagreements that show up at e2e scale are the high-signal ones.

## Triage buckets for Task #5

The triage MUST classify every record into exactly one of these buckets before the 3A→3B gate is evaluated.  Buckets in evaluation order:

1. **`per_field_gap` (defer to 3B).**  Site 2 (`match_cleanup`) records on the scrutinee local during partial-move cleanup.  The ledger sees the scrutinee as `Live` because partial-move semantics are conceptual, not represented in MIR; the per-field drop temps that take ownership are accounted for elsewhere.  Filter: `site == "match_cleanup"` AND `site_reason in {"field_moved", "field_needs_drop", "field_not_drop_needing"}`.  These do NOT block the gate; 3B's per-field tracking will subsume them.
2. **`droppolicy_approximation` (defer to 3B, do not silence).**  Driver-side `_needs_drop` callable in `driftc.py` uses raw `TypeTable.has_drop` rather than the canonical `DropPolicy.needs_drop`.  Two divergence cases produce records in this bucket:
   - `copy_status(ty) is True` types — DropPolicy says `needs_drop=False`, has_drop says True.  Surfaces as `site_stricter` (site correctly skipped drop on a Copy type; ledger says drop required).
   - DV-bearing types — DropPolicy says `needs_drop=True` via DV transitive walk, has_drop may say False.  Surfaces as `ledger_stricter`.
   Filter for triage: spot-check sample records of each `site_stricter` / `ledger_stricter` against the local's `copy_status` and `_contains_dv_transitive`.  If 90%+ match these two shapes, bucket as approximation noise.  The three-quadrant pin in `test_ownership_ledger_three_quadrant_pin.py` uses the real `DropPolicy.needs_drop`, so the gate's correctness is not vulnerable to this approximation.  3B MUST attach a per-function DropPolicy accessor in place of the raw `has_drop` query before any consumer is swapped onto the ledger.
3. **`path_dependent` (input to 3C).**  `classification == "path_dependent"`.  These are conditional-move sites the ledger flags as 3C queue material.  Count + summarise per site for the 3C design note.
4. **`semantic_equivalent` (silent agreement).**  `classification == "semantic_equivalent"`.  Site emits drop on a `Tombstoned` local; runtime no-op.  Acknowledge but do not act.
5. **`real_disagreement` (gate-blocking).**  Anything not absorbed by buckets 1–4.  These are real `ledger_stricter` (potential leak) or `site_stricter` (real ledger bug) cases.  Gate criterion: bucket 5 must be empty before 3B begins.

## Implications

- **`drop_before_overwrite` disagreements at e2e scale** are the highest-signal candidates for bucket 5.  Smoke produced 100% agreement on this site; any e2e disagreements warrant individual audit rather than bucketing.
- **`scope_drop` / `string_arc_return` ledger_stricter records** are mostly bucket-2 noise but a small tail will be bucket 5.  Audit the tail.
- **Bucket 1 (`match_cleanup`) is loud but uninterpretable in 3A** — it should not be the basis for any decision until 3B adds per-field tracking.

## Sample records

```json
{"classification": "site_stricter", "fn_name": "lang.atomic::_order_code", "ledger_verdict": "must_not_drop", "local": "__match_scrut_tmpt14", "program_point": ["match_arm_0", 2], "raw_state": "live", "site": "match_cleanup", "site_reason": "needs_drop", "site_verdict": "must_drop"}

{"classification": "ledger_stricter", "fn_name": "std.cli::_slice_string", "ledger_verdict": "must_drop", "local": "__match_scrut_tmpt77", "program_point": [...], "raw_state": "live", "site": "match_cleanup", "site_reason": "field_moved", "site_verdict": "must_not_drop"}
```
