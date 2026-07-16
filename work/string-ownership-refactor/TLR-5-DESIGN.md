# TLR-5 design checkpoint — StringFrom* + Exc* families (report only)

Status: DESIGN/CHECKPOINT ONLY — no implementation.  Prerequisite
state: TLR-4 accepted (`build/tmp/cleanup-tlr4`: temp_lastuse_release
25,017; materialized_lastuse_release 593,727; exact ±114,780 transfer,
all nine hard gates zero, standalone memcheck 99+1).

## 1. Confirmed residual counts (scratch remeasure on the TLR-4 tree)

Fine-grained TEMP-MEASURE run (`build/tmp/tlr5-measure`, exit 0,
universe identical 924/344/49, events 2,772,052 unchanged, materialized
593,727 unchanged, plain residue 0; restoration via stored reverse
edits, zero `TM_` refs, battery 45/45):

| producer | count |
|---|---|
| CopyValue (out of scope — stake precision) | 11,095 |
| none / cross-block (out of scope — lifetime analysis) | 7,398 |
| **StringFromUint** | **1,853** |
| **StringFromFloat** | **1,850** |
| **StringFromInt** | **1,850** |
| **StringFromBool** | **926** |
| **ExcGetParamsJson** | **40** |
| **ExcGetContextJson** | **5** |
| **sum** | **25,017 — lossless** |

StringFrom* total 6,479 and Exc* total 45, exactly as carried by every
+0 slice since the first measurement.

## 2. Both families are unconditional owned producers — confirmed

- **StringFrom{Int,Bool,Uint,Float}**: plain single-dest MIR
  instructions `(dest, value)` with a SCALAR operand (Int/Bool/Uint/
  Float — never a String, so the operand side has no ownership
  interaction).  Emitted exclusively by f-string interpolation-hole
  lowering (`hir_to_mir.py` `_const_part`/holes walk) and the
  throw-envelope event-code formatter.  No `can_throw`, no control
  flow, no hidden edges.  Codegen lowers to `drift_string_from_*`,
  returning a fresh +1 — the `owned_defs` prepass already registers
  the dests unconditionally (same elif as StringConcat).
- **ExcGetParamsJson / ExcGetContextJson**: plain single-dest
  instructions `(dest, error)`; the operand is the error value, not a
  String.  Emitted by `<error>.params` / `<error>.context`
  field-access lowering.  Codegen calls
  `drift_error_get_{params,context}_json`, which return RETAINED
  canonical JSON Strings — "caller owns and releases" per ABI spec
  §2.3 (the owned_defs prepass comments cite exactly this).  No
  `can_throw`, no topology.

So: no call/throw analysis, no conditional admission — both families
are unconditional isinstance members, exactly like ConstString and
StringConcat.

## 3. One predicate extension, one slice (no 5a/5b split)

Both families extend the UNCONDITIONAL tuple inside
`is_materialized_release_family_producer`:

```python
	if isinstance(prod, (
		M.ConstString, M.StringConcat,                       # TLR-1..3
		M.StringFromInt, M.StringFromBool,                   # TLR-5
		M.StringFromUint, M.StringFromFloat,                 # TLR-5
		M.ExcGetParamsJson, M.ExcGetContextJson,             # TLR-5
	)):
		return True
```

Recommendation: ONE slice.  The mechanics are identical for both
families (unconditional producers, family-agnostic machinery past the
predicate), the Exc* population is 45, and a 5a/5b split would add a
review cycle without new information.  The measurement splits above
keep the two families separately auditable inside the single slice's
exact-delta check regardless.

Suppression coverage (verified in code):
- StringFrom*: the rewrite-loop owned arm ALREADY carries the
  `recognized_released` guard (added in TLR-3 for StringConcat, with a
  comment noting it is a no-op for StringFrom* "yet") — TLR-5 makes it
  live; the comment is updated, no logic change.
- Exc*: owned registration is PREPASS-ONLY (like Call — verified, no
  rewrite-loop re-add arm), fully covered by the per-block
  `owned_values -= recognized_released` subtraction; documented at the
  prepass branches.
- The shim, analysis, and recognition all consume the predicate — no
  further consumers exist (one-source rule holds by construction).

## 4. Expected corpus delta (acceptance, vs cleanup-tlr4)

- `temp_lastuse_release` 25,017 → **18,493** (−6,524);
- `materialized_lastuse_release` 593,727 → **600,251** (+6,524);
- per-family sub-check (from the §1 split): the transfer must be
  exactly 6,479 (StringFrom*) + 45 (Exc*);
- sum conserved (618,744 lifetime total); every other counter +0
  (incl. `events` 2,772,052); universe identical; all nine hard gates
  zero; FULL memcheck STANDALONE (sequential after the corpus job —
  standing rule since the TLR-3 flake).

Hard stop triggers: transfer ≠ 6,524 in either direction; any non-TLR
counter movement; any gate movement; A/B pin divergence; any memcheck
movement; recognition tripwire firing on any corpus fixture.

## 5. Regression plan

Unit pins (A/B byte-identity, pass+arc vs arc-only):
- **StringFrom* qualified**: all four instruction kinds produced from
  ConstInt/ConstBool/etc. operands, drained as StringEq/Concat
  operands → materialized; multi-use (ONE release after LAST use) and
  consumed (StoreLocal → none) carriers; idempotence re-run.
- **Exc* qualified**: synthetic `ExcGetParamsJson(dest, error)` /
  `ExcGetContextJson` temps drained non-consumingly → materialized;
  a consumed carrier (ctor/store) → none.
- **Out-of-contract**: misplaced + duplicated StringFrom*-temp release
  trips.  NOTE: the standing shape-rejection carrier in
  `test_tlr2b_out_of_contract_input_release_trips_string_arc` uses
  StringFromInt, which JOINS the family in TLR-5 (the same flip that
  happened to the Concat carrier in TLR-3) — the shape case moves to
  the next non-member (CopyValue-produced temp), and the StringFromInt
  carrier converts to placement cases.
- **Cross-block StringFrom* temp** stays out (per-block producer map).
- Conformance pin: qualified StringFrom* column added (the
  contract-level record, as with Concat and Call).

Heap-backed memcheck rows (one new file):
- **f-string row** (StringFrom* under valgrind): runtime Int/Uint/
  Float/Bool values interpolated (`f"row-{i} {b} {u} {x}"`) with the
  from-temps drained INTO the interpolation concat chain, plus
  interpolation results compared non-consumingly — missing release
  reads definitely-lost, double release reads Invalid free.
- **error-inspection row** (Exc* under valgrind): a throws callee;
  catch arm reads `e.params` / `e.context` and uses the views
  non-consumingly (compare/length) — exercises
  ExcGetParamsJson/ExcGetContextJson on the live error path.  The
  error edge is exercised on every iteration.

Batteries: reporter; stage2 + ledger-cache guardrails; FULL memcheck
STANDALONE; corpus exact-delta signature (§4).

## 6. Out of scope, reaffirmed

CopyValue (11,095) — stake-precision investigation (churn elimination
at the source, not migration); cross-block `none` (7,398) — the
lifetime analysis; both explicitly excluded here.  After TLR-5, those
two are the ONLY remaining temp_lastuse populations (18,493), and the
`_note_use` release-arm tripwire remains parked until they resolve.
4a′/4b′ deletion still parked on a clean cert cycle.

## 7. STOP

Awaiting review.  If accepted, implementation is one reviewed slice:
the two-family tuple extension + comment updates + pins + two memcheck
rows, exact −6,524/+6,524 acceptance with per-family sub-checks.
