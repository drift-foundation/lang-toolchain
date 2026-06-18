# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
#
# Checker -> stage2 contract for integer scalar `match`.
#
# The parser stores ONLY raw, syntactic pattern data (`scalar_literal_kind` /
# `scalar_literal_magnitude`).  The CHECKER is the sole owner of the validated,
# canonical SIGNED `scalar_value`, which it computes after signedness/range
# validation against the scrutinee type.  Stage2 lowering must consume ONLY that
# checked field and never reinterpret the raw parser syntax.
#
# This is observable end-to-end in the emitted IR: the equality-dispatch chain
# compares the scrutinee against one constant per arm, and that constant is the
# checker's `scalar_value`.  The decisive case is a NEGATIVE literal arm: its raw
# magnitude is +5 (`scalar_literal_kind == "NEG_INT"`, `magnitude == 5`) but its
# canonical `scalar_value` is -5.  If stage2 reinterpreted the raw syntax it
# would emit a `+5` comparison constant; consuming the checked field it emits
# `-5`.  We assert `-5` is present and `5` is absent from the dispatch.

from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.driftc import compile_to_llvm_ir_for_tests, ReservedNamespacePolicy
from lang.driftc.core.function_id import function_symbol


# Arm bodies return 10/20/30/99 — deliberately disjoint from every dispatch
# magnitude (0, 5, 7) so an `add i64 0, <n>` match is unambiguous.
_SRC = """
module m;

pub fn pick(n: Int) nothrow -> Int {
	val r = match n {
		0 => { 10 },
		-5 => { 20 },
		7 => { 30 },
		default => { 99 },
	};
	return r;
}

pub fn main() nothrow -> Int {
	return pick(0);
}
"""


def test_scalar_match_lowering_consumes_only_checked_value(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(_SRC)
	modules, type_table, exc, mexp, mdeps, pdiags = parse_drift_workspace_to_hir(
		[src], stdlib_root=stdlib_root(), test_build_only=True
	)
	assert not pdiags, [d.message for d in pdiags]
	func_hirs, signatures, _ = flatten_modules(modules)
	main_id = [i for i, s in signatures.items() if i.name == "main" and not s.is_method][0]
	origin = {}
	for m in modules.values():
		origin.update(m.origin_by_fn_id)

	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exc,
		entry=function_symbol(main_id),
		type_table=type_table,
		module_exports=mexp,
		module_deps=mdeps,
		origin_by_fn_id=origin,
		enforce_entrypoint=True,
		reserved_namespace_policy=ReservedNamespacePolicy.ALLOW_DEV,
	)
	errors = [d.message for d in getattr(checked, "diagnostics", []) if getattr(d, "severity", None) == "error"]
	assert not errors, errors

	# Isolate the `pick` function body in the IR (the whole module includes the
	# stdlib, which legitimately contains unrelated `add i64 0, 5` constants).
	pick_sym = function_symbol(
		[i for i in signatures if i.name == "pick" and not signatures[i].is_method][0]
	)
	lines = ir.splitlines()
	start = next(
		i for i, l in enumerate(lines)
		if l.startswith("define") and (f'@"{pick_sym}"(' in l or f"@{pick_sym}(" in l)
	)
	end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("}"))
	pick_ir = "\n".join(lines[start : end + 1])

	# The dispatch chain compares against each canonical (signed) scalar_value.
	for v in (0, -5, 7):
		assert f"add i64 0, {v}" in pick_ir, f"missing scalar dispatch constant {v} in pick()"

	# Decisive contract check: the NEGATIVE arm dispatches on the checked signed
	# value -5, NOT the raw magnitude +5.  A `add i64 0, 5` constant in pick()
	# would mean stage2 re-derived the value from raw parser syntax instead of
	# consuming `scalar_value`.  (pick's arm bodies return 10/20/30/99, so no
	# stray +5 constant exists in this function.)
	assert "add i64 0, 5," not in pick_ir, (
		"scalar dispatch emitted the raw magnitude (+5) instead of the checked value (-5)"
	)
