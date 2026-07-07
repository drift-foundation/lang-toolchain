# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Contract tests for `HIRToMIR._drop_policy` — the Phase 1
single-source-of-truth drop/copy classifier on the
`fix/ownership-drop-ledger` track.

Phase 1 is a semantic-preserving refactor: `_drop_policy` centralises
the ~40 ownership-decision sites that previously consulted
`TypeTable.copy_status` / `has_drop` / `is_bitcopy` / `is_destructible`
independently, but the RULES it encodes are the same rules the
pre-Phase-1 helpers implemented.  The tests in this file pin those
rules so Phase 2 (the per-program-point ownership ledger) lands as a
visible diff: any semantic change to `_drop_policy` must update
these pins, and the accompanying consumer-side changes get reviewed
against the pinned behaviour.

Canonical types covered:
  - `Int`               — POD.  Bitcopy, no drop, cheap-copy, not
                          destructible, no structural drop.
  - `String`            — refcounted scalar.  Not bitcopy, needs
                          drop, cheap-copy (retain), not
                          destructible, has structural drop.
  - `V<Int>`            — POD variant.  Same axes as Int except
                          `is_bitcopy` may be False (variants are
                          not bitcopy-shaped even when their
                          fields are) — test pins the observed
                          value, not an assumption.
  - `V<String>`         — structural-with-drop variant.  The
                          classical `Optional<String>` shape.  Test
                          covers BOTH the natural classification
                          (no Copy hook) AND the bug-shape
                          classification (Copy hook forces
                          `copy_status=True`, mimicking the
                          packaged-load resolution that caused the
                          `match Optional<String>` UAF).

The `V<String>` bug-shape pin is the regression anchor for the
Phase 0 fail-stop: the combination `has_structural_drop=True AND
needs_drop=False AND is_cheap_copy=True` is EXACTLY the invariant
the fail-stop in `_ensure_arm_scrut_ptr` triggers on, and the pin
here makes sure Phase 2's semantic fix either (a) also updates this
pin or (b) demonstrates why the divergence is no longer reachable.
"""
from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import TypeId, TypeTable, VariantArmSchema, VariantFieldSchema
from lang.driftc.stage2 import HIRToMIR, make_builder
from lang.driftc.stage2.hir_to_mir import DropPolicy


def _policy(type_table: TypeTable, ty: TypeId) -> DropPolicy:
	"""Ask `_drop_policy` for the policy of `ty`.

	Instantiates `HIRToMIR` with the minimum state required to call
	`_drop_policy`.  No HIR is lowered; the test only exercises the
	classification funnel.
	"""
	builder = make_builder(FunctionId(module="main", name="main", ordinal=0))
	lower = HIRToMIR(builder, type_table=type_table)
	return lower._drop_policy(ty)


def _install_copy_hook(type_table: TypeTable, *copy_types: TypeId) -> None:
	"""Force the named types to report `copy_status=True`.

	Mirrors the packaged-`.dmp`-load path that eagerly resolves
	`copy_status` from the transitive trait-impl graph.  This is the
	exact hook the TLS-team-reported UAF repro used, so covering it
	here keeps the Phase 0 assertion's precondition under test
	coverage.
	"""
	wanted = set(copy_types)
	type_table.set_copy_query(lambda tid: tid in wanted, allow_fallback=True)


def _build_opt_variant(type_table: TypeTable, payload_ty: TypeId) -> TypeId:
	"""Build a single-payload variant `V<payload_ty>` mirroring
	`Optional<T>`'s shape — one Some arm with a single payload field
	and one None arm with none."""
	var_base = type_table.declare_variant(
		module_id="main",
		name="V",
		type_params=["T"],
		arms=[
			VariantArmSchema(
				name="Some",
				fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))],
			),
			VariantArmSchema(name="None", fields=[]),
		],
	)
	return type_table.ensure_instantiated(var_base, [payload_ty])


def test_drop_policy_int_is_pod() -> None:
	"""`Int` is the canonical POD: bitcopy, no drop, cheap-copy."""
	type_table = TypeTable()
	int_ty = type_table.ensure_int()
	p = _policy(type_table, int_ty)
	assert p.needs_drop is False
	assert p.is_bitcopy is True
	assert p.is_cheap_copy is True
	assert p.is_destructible is False
	assert p.has_structural_drop is False


def test_drop_policy_string_unshortcut_classification() -> None:
	"""`String` classified WITHOUT a Copy hook — Scope A structural policy.

	Since String Scope A (work/string-ownership-refactor/), String's
	ownership facts are STRUCTURAL and mode-independent:
	`_is_copy_structural(String)` returns True (retain-copy — an ARC
	handle whose copy bumps the refcount), so `copy_status(String)` is
	True even in a unit-test `TypeTable` with no Copy query hook
	installed. The isolated table now agrees with the real-deployment
	classification:
	  - needs_drop          = True   (has_drop is structural too)
	  - is_bitcopy          = False  (copy is a retain, never memcpy-only)
	  - is_cheap_copy       = True   (Copy + SCALAR kind: one retain)
	  - is_destructible     = False
	  - has_structural_drop = True

	Pre-Scope-A, `copy_status(String)` was None here (query-dependent),
	which made `is_cheap_copy` flip between isolated tables and real
	compiles — the two-authority split the Scope A audit closed. The
	Copy-hook variant below
	(`test_drop_policy_string_with_copy_hook_pins_shortcut_behaviour`)
	must now observe the SAME policy, which is the point.
	"""
	type_table = TypeTable()
	string_ty = type_table.ensure_string()
	p = _policy(type_table, string_ty)
	assert p.needs_drop is True, (
		"String.needs_drop must be True in every mode — Copy-ness "
		"does not remove the refcount cleanup; for String, Copy and "
		"needs-drop are BOTH true (the design's core tension, made "
		"structural by Scope A)."
	)
	assert p.is_bitcopy is False
	assert p.is_cheap_copy is True, (
		"String.is_cheap_copy must be True without a Copy hook — "
		"Scope A made String structurally Copy (retain-copy), so the "
		"isolated-table classification no longer diverges from real "
		"compiles."
	)
	assert p.is_destructible is False
	assert p.has_structural_drop is True, (
		"String.has_structural_drop must be True — the refcount "
		"header is the drop-bearing child.  This axis is the "
		"shortcut-free query the Phase 0 fail-stop depends on and "
		"must NEVER be False for a refcounted type, regardless of "
		"how the generic drop path is routed."
	)


def test_drop_policy_string_with_copy_hook_pins_shortcut_behaviour() -> None:
	"""`String` with a Copy hook — real-deployment classification.

	In a compiler run against a real stdlib (source or packaged),
	the Copy trait's `impl Copy for String` resolves and
	`copy_status(String)` returns True.

	After the Copy-short-circuit fix
	(`lang/tests/driver/test_drop_policy_copy_short_circuit_bug.py`),
	the policy is driven by destruction reality, not gated by
	`copy_status`.  So:
	  - needs_drop          = True   (refcount must be released —
	                                  drift_string_release)
	  - is_cheap_copy       = True   (single retain — String is a
	                                  refcounted scalar, the Copy
	                                  semantics ARE one retain)
	  - has_structural_drop = True   (shortcut-free; refcount is the
	                                  drop-bearing child)

	The asymmetric triplet that previously gated the Copy/MOVE
	decision (`needs_drop=False AND is_cheap_copy=True`) is gone:
	the new policy has `needs_drop=True AND is_cheap_copy=True`,
	which the binder/scrut Copy paths handle by emitting `CopyValue`
	(single retain) and registering the binder for its own scope-
	exit drop.  string_arc continues to manage refcounted locals
	independently; double-drop is prevented by `string_arc` skipping
	locals already authored by the generic path (see
	`string_arc.py`'s skip-when-generic-handles logic).
	"""
	type_table = TypeTable()
	string_ty = type_table.ensure_string()
	_install_copy_hook(type_table, string_ty)
	p = _policy(type_table, string_ty)
	assert p.needs_drop is True, (
		"String.needs_drop with Copy hook must be True — the refcount "
		"MUST be released (drift_string_release) regardless of Copy "
		"trait status.  This is the central fix for the LANGUAGE_BUG "
		"in `test_drop_policy_copy_short_circuit_bug.py`."
	)
	assert p.is_cheap_copy is True, (
		"String.is_cheap_copy must remain True — the Copy semantics "
		"for a refcounted scalar are a single retain, which is "
		"cheap.  Decoupled from `needs_drop` after the policy fix."
	)
	assert p.has_structural_drop is True, (
		"String.has_structural_drop must remain True even with "
		"the Copy hook installed — the shortcut-free axis is the "
		"invariant the Phase 0 fail-stop reads.  If this flips "
		"under the Copy hook, the fail-stop silently stops firing "
		"and the UAF ships again."
	)


def test_drop_policy_variant_of_int_has_no_drop() -> None:
	"""`V<Int>` (Optional<Int>-shaped) has no drop-bearing children.

	Axis-by-axis:
	  - needs_drop          = False  (no droppable payload)
	  - is_bitcopy          = False  (variants are not bitcopy-
	                                  shaped in this compiler even
	                                  when their payloads are POD)
	  - is_cheap_copy       = True   (`copy_status(V<Int>)` resolves
	                                  True structurally — POD
	                                  variants are Copy by trait)
	  - is_destructible     = False
	  - has_structural_drop = False  (no drop-bearing children)

	The `is_cheap_copy=True` pin is load-bearing for the Phase 0
	fail-stop's negative test (`V<Int>` must reach the Copy-store
	branch to prove the fail-stop's scoping).  If this flips to
	False, the negative test in
	`test_match_scrut_copy_path_drop_bearing_assertion.py` starts
	exercising the MoveOut branch instead and stops proving
	anything about the fail-stop — update both pins together.
	"""
	type_table = TypeTable()
	opt_int_ty = _build_opt_variant(type_table, type_table.ensure_int())
	p = _policy(type_table, opt_int_ty)
	assert p.needs_drop is False
	assert p.is_cheap_copy is True, (
		"V<Int>.is_cheap_copy must stay True under pre-Phase-1 "
		"rules — POD variants are structurally Copy and the "
		"cheap-copy axis reflects that.  Phase 0's fail-stop "
		"negative test relies on V<Int> reaching the Copy-store "
		"branch; flipping this pin to False makes that test "
		"vacuous.  Update both in the same change."
	)
	assert p.has_structural_drop is False


def test_drop_policy_variant_of_string_needs_drop() -> None:
	"""`V<String>` (Optional<String>-shaped) WITHOUT a Copy hook.

	The natural classification: copy_status unresolved (no Copy
	proof), has_drop True (the Some payload is a String).
	`_drop_policy` must report `needs_drop=True`, not route through
	the Copy-trait shortcut.
	"""
	type_table = TypeTable()
	opt_string_ty = _build_opt_variant(type_table, type_table.ensure_string())
	p = _policy(type_table, opt_string_ty)
	assert p.needs_drop is True
	assert p.is_cheap_copy is False
	assert p.has_structural_drop is True, (
		"V<String> has a String payload; has_structural_drop MUST be "
		"True.  This is the axis the Phase 0 fail-stop relies on to "
		"catch the UAF-shape compilations."
	)


def test_drop_policy_variant_of_string_with_copy_hook() -> None:
	"""`V<String>` WITH the packaged-load Copy hook.

	When a consumer is built against a packaged `.dmp`, the Copy-
	trait graph resolves eagerly and `copy_status(V<String>)` can
	return True (this is the same packaged-load shape that Bug 1
	in `traits/world.py` canonicalises across raw/pkg builds).

	After the Copy-short-circuit fix the policy is:
	  - needs_drop          = True   (raw_has_drop=True for variant
	                                  containing String — refcount
	                                  must be released)
	  - is_cheap_copy       = False  (variant of String is structural
	                                  with drop — per-field traversal
	                                  is required, not cheap)
	  - has_structural_drop = True   (shortcut-free axis)

	Pre-fix: `needs_drop=False AND is_cheap_copy=True` was the UAF-
	producing asymmetric triplet — the Copy shortcut hid required
	release semantics from the generic drop path.  0.31.0 Phase 2a
	mitigated the immediate UAF by switching the Copy-store branch
	in `_ensure_arm_scrut_ptr` to emit `CopyValue`; the policy fix
	now eliminates the asymmetry at the source.
	"""
	type_table = TypeTable()
	opt_string_ty = _build_opt_variant(type_table, type_table.ensure_string())
	_install_copy_hook(type_table, opt_string_ty)
	p = _policy(type_table, opt_string_ty)
	assert p.needs_drop is True, (
		"V<String> with Copy hook must report needs_drop=True — the "
		"variant's String payload requires drift_string_release.  "
		"Pre-fix asymmetric triplet has been eliminated."
	)
	assert p.is_cheap_copy is False, (
		"V<String>.is_cheap_copy must be False — a variant carrying a "
		"refcounted payload requires per-field retain traversal, not "
		"a single bitcopy/retain.  Decoupled from `needs_drop` after "
		"the policy fix; structural-with-drop is not cheap."
	)
	assert p.has_structural_drop is True, (
		"V<String> with Copy hook must STILL report "
		"has_structural_drop=True — this is the shortcut-free axis "
		"the `_ensure_arm_scrut_ptr` Copy-store branch's CopyValue "
		"dispatch depends on indirectly (via `is_bitcopy` being "
		"False).  If this flips to False for a refcounted variant, "
		"bitcopy semantics would apply at the scrut level and the "
		"UAF would return — this is the strongest invariant in "
		"this file."
	)


def test_drop_policy_wrappers_agree_with_policy_fields() -> None:
	"""The Phase-1 thin wrappers (`_needs_runtime_drop`,
	`_should_copy_value`, `_type_is_destructible`) must return the
	same answers as the corresponding `DropPolicy` fields.

	This is the structural invariant that makes Phase 1 a true
	funnel: any future divergence between a wrapper and the policy
	(e.g. "oh let me add a special-case here") is caught by this
	test.
	"""
	type_table = TypeTable()
	string_ty = type_table.ensure_string()
	int_ty = type_table.ensure_int()
	opt_string_ty = _build_opt_variant(type_table, string_ty)
	opt_int_ty = _build_opt_variant(type_table, int_ty)

	builder = make_builder(FunctionId(module="main", name="main", ordinal=0))
	lower = HIRToMIR(builder, type_table=type_table)

	for ty in (int_ty, string_ty, opt_int_ty, opt_string_ty):
		policy = lower._drop_policy(ty)
		assert lower._needs_runtime_drop(ty) == policy.needs_drop, ty
		assert lower._should_copy_value(ty) == policy.is_cheap_copy, ty
		assert lower._type_is_destructible(ty) == policy.is_destructible, ty
		# `_classify_value_transfer` is not a direct mirror — it
		# returns "unknown" for typevars, and "copy"/"move" for
		# concrete types.  For the concrete types exercised here it
		# must agree with `is_cheap_copy`.
		assert lower._classify_value_transfer(ty) == ("copy" if policy.is_cheap_copy else "move"), ty
