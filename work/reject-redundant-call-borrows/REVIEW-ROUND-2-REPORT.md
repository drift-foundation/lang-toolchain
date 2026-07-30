# Implementation review round 2 — response report (2026-07-29)

Scope: the three round-2 findings. Targeted verification only, per direction —
no large suites run yet (driver/e2e/full gate deliberately held for post-review).

## Finding 1 — D6 broken on trait paths / family-local formal detection: FIXED

**Root causes found (three, progressively exposed by the pins):**
1. Both new trait wirings tested `type_expr.name in ("&","&mut")` — literal
   token check; alias-declared references got mask False and the explicit
   borrow was then EXEMPT-stamped and accepted.
2. Trait-IMPL methods resolved DIRECTLY carried the impl's concrete template
   (`v: &String` written to satisfy `v: V`) — the call-site contract is the
   TRAIT declaration, so a generic-by-value formal was mis-read as declared
   (the D6 mirror image: over-rejection instead of under-rejection).
3. `trait.type_params` entries are plain strings; a `getattr(tp, "name")`
   collection silently emptied the generic-name set.

**Fix (centralized, per the approved design):** new module-level
`declared_ref_formal(type_expr, resolved_tid, type_table, generic_param_names)`
in `checker/call_resolver.py` — declared iff RESOLVED type is REF and the
declared syntax is not a bare generic-parameter name (alias-transparent per D6;
`param_index` nodes and bare typevar names exempt). Both trait paths now use it;
DIRECT targets with trait linkage (via `impl_trait_*` or the registry's
`METHOD_TRAIT` + `trait_key_for_id`) reroute to the TRAIT declaration's shapes;
trait lookup falls back by name when synthesized keys lack module/package.

**Pins** (`lang/tests/driver/test_trait_path_declared_ref_masks.py`, 5/5):
- alias formal through require-bound dispatch (supported idiom: trait-qualified
  call inside the require fn, the stdlib `cmp.Comparable::cmp` pattern): bare
  auto-borrows AND explicit rejected;
- the same through a generic trait-qualified call: bare + explicit;
- generic-by-value formal instantiated at a reference stays exempt, pinned via
  the `core.Fn1<&String, Int>` require pattern (the stdlib ffi shape). A
  user-defined parameterized trait exercised through the qualified form hits a
  PRE-EXISTING impl-lookup gap (`no implementation … on receiver Ref<T>`),
  unrelated to the rule — noted in the test comment.

Also verified: `mem_replace_helper_param_ref`-class stdlib sites
(`cmp.Comparable::cmp(&self.arr[i], self.arr[j])`) classify correctly —
receiver-slot borrow exempt, declared arg swept bare — and stdlib compiles with
ZERO errors under the strict totality validator (no fallback).

## Finding 2 — stale D5 corpus authorization: RECONCILED

Appendix added to D5-test-changes.md. Round-2 enumeration [SUPERSEDED
post-round-3: the round-3 pin `trait_qualified_ref_type_arg_impl_lookup` is
enumerated compiled_ok #23 — final figures are **23 additions (15 failed +
8 compiled_ok), universe 1,269 → 1,292**; D5-test-changes.md is authoritative]:
the approved 20 all exist; TWO mandated joiners then made 22 additions:
`array_extend_elem_mismatch_rejected` (the LANGUAGE_BUG regression from round-1
finding 2) and `redundant_arg_borrow_assoc_rejected` (round-1 finding 5's
explicit-half mandate). **426 deltas; 0 removals; 0
expected flips.** The "11 remaining including assoc" phrasing was a log
counting slip, not a dropped approval — all 13 approved negatives verified on
disk.

## Finding 3 — extend explicit-spelling absence assertion: ADDED

`lang/tests/driver/test_array_extend_source_type.py` (2/2): bare wrong-type →
`extend() source element type mismatch`; explicit `&wrong` → the SAME mismatch
with an explicit `E_REDUNDANT_ARG_BORROW`-ABSENT assertion (driver-level,
because the e2e subset-matcher cannot prove absence).

## Verification snapshot (targeted, pre-big-suite)

- **Consolidated run: 122/122** — trait-path pins (5), extend source-type (2),
  W0 validator units (6, incl. the new surviving-MUT_RVALUE_BINDING rejection),
  fnptr D8 controls (11), D9 package pins (4, incl. encode→decode→recompile),
  A/B gate (3, single-outer-borrow equivalence), full borrow_checker suite —
  one 8-way pytest invocation. Stdlib compiles clean under strict totality.
- NOT yet run (awaiting review go-ahead): full driver suite, e2e corpus suite,
  ownership-corpus audit dry run, memcheck/ASAN lanes, full gate.

## Still open before "compiler complete" can be claimed

- Reviewer sign-off on this round.
- The held big suites above (will surface any corpus-sweep stragglers:
  instantiation-dependent stdlib sites appear only under the programs that
  instantiate them; the sweeps converged for the e2e corpus + gate drivers).
- Remaining migration tail: effective-drift scattered samples, grammar-doc
  note + SCR addendum, combined 0.33.91 history entry + release notes with
  final numbers, corpus-promotion package.
