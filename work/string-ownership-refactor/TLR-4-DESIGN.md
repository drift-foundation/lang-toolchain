# TLR-4 design checkpoint — Call family (report only)

Status: DESIGN/CHECKPOINT ONLY — no implementation.  Prerequisite
state: TLR-3 accepted (`build/tmp/cleanup-tlr3`: temp_lastuse_release
139,797; materialized_lastuse_release 478,947; exact ±192,523 transfer,
all nine hard gates zero).

## 1. Measured split (scratch remeasure on the TLR-3 tree)

One instrumented corpus run (TEMP-MEASURE tagging in `_note_use`'s
release arm splitting the call bucket by kind × throw × provability;
restoration via stored REVERSE EDITS this time, verified zero `TM_`
references + battery 41/41; scratch out `build/tmp/tlr4-measure`,
exit 0, universe identical 924/344/49, events 2,772,052 unchanged,
materialized 478,947 unchanged, plain residue 0):

| bucket | count |
|---|---|
| **Call · nothrow · signature-proven semantic String (`infosem`)** | **114,780** |
| Call · nothrow · `drift_string_*` helper symbol | 0 |
| Call · nothrow · info-less/unproven | 0 |
| Call · can_throw · (any) | 0 |
| CallIndirect · (any × any) | 0 |
| CallIface · (any × any) | 0 |
| CopyValue (stake churn — separate investigation) | 11,095 |
| none (cross-block producer) | 7,398 |
| StringFrom* | 6,479 |
| Exc* | 45 |
| **sum** | **139,797 — lossless** |

The ENTIRE call family is direct `M.Call`, non-throw, with an
`fn_infos` signature whose return type is semantically String
(TypeKind.SCALAR + name — the finding-5 semantic test, not raw TypeId
equality).  The conservative exclusions cost zero population.

## 2. Direct/Indirect/Iface and throw/nothrow splits — answered

- Call 114,780 / CallIndirect 0 / CallIface 0.  Indirect/iface
  String-returning call RESULTS evidently never survive to a
  non-consuming last use in the corpus (they are consumed — stored,
  returned, staked — or their blocks route through envelopes).
- can-throw: 0, and STRUCTURALLY IMPOSSIBLE, not just unobserved (§3).

## 3. Can-throw topology — why the family is structurally non-throw

`_lower_can_throw_call_value` (hir_to_mir.py) lowers every can-throw
call in value position as:

    %fnres = Call(..., can_throw=True)     ; dest = FnResult ENVELOPE
    %is_err = ResultIsErr(%fnres)
    IfTerminator(%is_err, err_block, ok_block)   ; block ENDS here
    ok_block:  %ok = ResultOk(%fnres); StoreLocal(__call_okN, %ok)
    join_block: %dest = MoveOut(__call_okN)      ; the String temp

Consequences:
- A can-throw call's dest is NEVER String-typed — it fails the family's
  type condition outright; the payload's producer downstream is
  `MoveOut` (move-only, not a family candidate; the TM `other` bucket
  is 0 across all three measurements).
- Error edges cannot invalidate block-local release placement: a
  can-throw call always ENDS its block, so any qualified temp draining
  earlier in the block has its release emitted BEFORE the terminator —
  executed on both edges.  A temp whose last use is an ARGUMENT to a
  can-throw call is either CONSUME (by-value String param → moved, out
  of family) or IGNORE (ref param → never drains, out of family).
  Throw topology is already block topology; the per-block analysis
  needs no new reasoning.
- The `can_throw=False` condition in the predicate (§5) is therefore a
  fail-closed guard for an unreachable case, and is pinned as such.

## 4. Runtime/string helpers vs user/package calls — answered

Both return +1-owned Strings (Drift ABI: String return transfers
ownership; `drift_string_*` helpers return retained results) — no
release-semantics difference.  The helper bucket measured ZERO: helper
Call producers essentially don't reach MIR-level last-use releases
(string operations lower to dedicated MIR ops — StringConcat,
StringFrom* — not helper calls; helper Calls appear at codegen level).
The `fn_infos`-vs-symbol distinction affects only `_is_string_creator`
move approvals, unchanged by TLR-4.  The helper-symbol proof path is
KEPT in the predicate (harmless, population 0 today, and it is the
existing proven logic the conservative rule defers to).

## 5. The shared producer predicate (replaces the tuple)

Per review direction, `MATERIALIZED_RELEASE_FAMILY` (isinstance tuple)
is replaced by ONE module-level predicate in `string_arc.py`:

```python
def is_materialized_release_family_producer(
	prod,            # M.MInstr | None — producers.get(v)
	*,
	local_types,     # for the dest String-type condition (caller-side)
	fn_infos,
	type_table,
) -> bool:
	# Unconditional members (TLR-1/2b, TLR-3):
	if isinstance(prod, (M.ConstString, M.StringConcat)):
		return True
	# TLR-4: direct calls only, non-throw only, and only when the
	# result is PROVEN semantically String by existing logic —
	# fn_infos signature return type (semantic SCALAR+"String" test,
	# finding-5 rule) or the drift_string_* helper-symbol list.
	# Info-less/unproven call results are conservatively OUT.
	if isinstance(prod, (M.Call, M.CallIndirect, M.CallIface)):
		if getattr(prod, "can_throw", False):
			return False            # structurally unreachable; fail-closed
		if isinstance(prod, M.Call):
			sym = function_symbol(prod.fn_id)
			if isinstance(sym, str) and sym in _DRIFT_STRING_HELPER_SYMBOLS:
				return True
			info = fn_infos.get(prod.fn_id)
			return (
				info is not None and info.signature is not None
				and info.signature.return_type_id is not None
				and _is_semantic_string(type_table, info.signature.return_type_id)
			)
		# CallIndirect/CallIface: instruction-carried user_ret_type is
		# the proof.  Measured population today: 0 — included because
		# the proof obligation is identical and instruction-local.
		urt = getattr(prod, "user_ret_type", None)
		return urt is not None and _is_semantic_string(type_table, urt)
	return False
```

Open sub-decision for review: ADMIT CallIndirect/CallIface via
`user_ret_type` as sketched (population 0 — pure future-proofing with
an instruction-local proof), or EXCLUDE them entirely until they
measure nonzero.  The report recommends ADMIT (the proof is exactly as
strong as the direct-call signature proof and needs no fn_infos), but
either choice keeps the corpus delta identical; excluding is one
`isinstance(prod, M.Call)` gate stricter.

ONE-SOURCE consumers (all three, same predicate object):
1. `_analyze_lastuse_block`'s `_is_family_temp` — becomes
   `is_materialized_release_family_producer(producers.get(v), ...) and
   local_types.get(v) == string_ty` (analysis + recognition shape,
   including the strict `unexpected input release` shape rejection);
2. the TLR shim classification in `_note_use` (dead-in-production for
   family members, still the config-A classifier the A/B pins compare);
3. the pass consumes (1) via the calculator — no pass-side change.
The suppression arms are set-driven (recognized set) except the
owned-registration re-add skips, which need the Call/CallIndirect/
CallIface owned arm to gain the same `recognized_released` guard the
ConstString and StringFrom*/Concat arms carry.

`fn_infos` threading: the predicate needs `fn_infos`/`type_table`,
which every current consumer already has in scope (the analysis takes
both as kwargs; the shim closure sees them; the pass passes them to the
calculator).  No signature changes to the public contracts.

## 6. One family or split?

ONE family, one slice.  The can-throw split dissolves structurally
(§3): there is no "can-throw later" population — a can-throw call
result temp cannot exist.  The measured family is homogeneous
(Call·nothrow·infosem = 100%), and the machinery is family-agnostic
past the predicate.

## 7. Expected corpus delta (acceptance, vs cleanup-tlr3)

- `temp_lastuse_release` 139,797 → **25,017** (−114,780);
- `materialized_lastuse_release` 478,947 → **593,727** (+114,780);
- sum conserved exactly (618,744 lifetime total); every other counter
  +0 (incl. `events` 2,772,052); universe identical 924/344/49; all
  nine hard gates zero; FULL memcheck 98 + 1 skip.

Hard stop triggers: transfer ≠ 114,780 in either direction; any
non-TLR counter movement; any gate movement; A/B pin divergence; any
memcheck movement (run memcheck STANDALONE — the TLR-3 lesson: a
concurrent corpus run produced 19 false timeout failures); recognition
tripwire firing on any corpus fixture; any consumer found needing
producer logic beyond the shared predicate.

## 8. Regression plan

A/B byte-identity pins (pass+arc vs arc-only):
- **Call-result temp qualified:** `%r = Call(f)` (fn_infos signature,
  semantic-String return, can_throw=False), last use StringEq → release
  after it; materialized in B, shim-classified materialized in A.
- **THROWING-CALL TOPOLOGY (required):** a driver-level pin compiling
  real Drift source where a `throws` function's String result is used
  non-consumingly inside `try {}` — asserting (a) compile+run green,
  (b) memcheck row clean on BOTH edges (throw path exercised: the
  callee actually throws in one run), (c) the corpus-shape assertion
  that the envelope dest never enters the family (unit pin: a
  `Call(can_throw=True)` with a String-typed dest is NOT family —
  fail-closed guard pinned directly).
- **Info-less call result stays out (conservative rule):** unit pin —
  `Call` with String-typed dest but no fn_infos entry → calculator
  yields no point; live pass still releases it under temp_lastuse; A/B
  byte-identical.  (This is today's 0-population exclusion, pinned so a
  future metadata regression cannot silently widen the family.)
- **Semantic-String pin:** signature return_type_id =
  `new_scalar("String")` (non-canonical) → IN family (finding-5 rule).
- **CallIndirect/CallIface** (if admitted): user_ret_type
  semantic-String, can_throw=False → qualified; can_throw=True → not.
- **Multi-use / repeated-operand / consumed call results** — same
  occurrence-level shapes as TLR-3's pins, Call carrier.
- **Cross-block call result untouched**; **idempotence** re-run with a
  Call temp; **out-of-contract**: misplaced/duplicated Call-temp
  release trips; the SHAPE rejection carrier stays StringFromInt (still
  out of family).
- Conformance pin update: the info-less-call IGNORE temp in the
  existing pin keeps its role (args IGNORE is orthogonal); a new
  qualified-Call column is added.
- Double-release direction: suppression by construction + strict
  recognition + idempotence + memcheck Invalid-free.
  Missing-release direction: A/B equality + corpus deficit + memcheck
  definitely-lost.
- Batteries: reporter; stage2 + ledger-cache guardrails; FULL memcheck
  STANDALONE; corpus exact-delta signature (§7).

## 9. Out of scope, reaffirmed

CopyValue (11,095) rides the stake-precision investigation (the churn
may be eliminated at the source rather than migrated); StringFrom*
(6,479) and Exc* (45) are a later mechanical family (unconditional
producers, same shape as Concat); cross-block `none` (7,398) needs the
lifetime analysis flagged since the first measurement; 4a′/4b′ deletion
still parked on a clean cert cycle; the `_note_use` release-arm
tripwire waits for the LAST family.

## 10. STOP

Awaiting review.  If accepted, implementation is one reviewed slice:
the predicate swap + owned-arm guard + pins, exact −114,780/+114,780
acceptance.
