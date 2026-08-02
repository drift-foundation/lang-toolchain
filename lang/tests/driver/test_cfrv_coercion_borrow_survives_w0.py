# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""P5.2 W0-tail — a SURVIVING control-flow-rvalue coercion borrow
(work/control-flow-rvalue-ownership).

Restoring E_REDUNDANT_ARG_BORROW for CFV shared borrows must NOT over-broaden
the classifier: a source-written `&` whose DELETION CHANGES TYPING is a genuine
coercion, not redundant. Canonical surviving case —

    hear(&(match true { true => { mkDog() }, false => { mkDog() } }))   // &Speaker

where `mkDog(): Dog` and `hear(s: &Speaker)`.

Two things are pinned:
  * DELETION CHANGES TYPING — the bare companion `hear(match …)` is REJECTED
    (Dog does not widen to &Speaker without the borrow), so the `&` is a real
    `&Concrete → &Interface` widening, not a redundant borrow; and
  * the surviving source-written borrow classifies EXACTLY `policy_class ==
    "coercion"` (NOT "redundant", NOT unclassified/None, NOT an "exempt" bypass)
    — inspected on the post-check HIR the W0 totality validator actually sees
    (captured by wrapping `validate_typed_hir`), because a clean compile alone
    cannot distinguish "coercion" from an exemption.

Compile+run coverage (base+ASan+memcheck) is the e2e fixture
`cfrv_coerce_iface_borrow`. Accepted BARE synthesized borrows
(source_written=False) are correctly outside W0 totality.
"""
from __future__ import annotations

from pathlib import Path

import lang.driftc.type_checker as TC
from lang.driftc import stage1 as H
from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


_SPEAKER = """
module m;

interface Speaker { fn speak(self: &Self) nothrow -> Int; }
struct Dog { tag: Int, }
implement Speaker for Dog { fn speak(self: &Dog) nothrow -> Int { return 4; } }

fn hear(s: &Speaker) nothrow -> Int { return s.speak(); }
fn mkDog() nothrow -> Dog { return Dog(tag = 1); }
"""

_EXPLICIT = _SPEAKER + """
pub fn main() nothrow -> Int {
	return hear(&(match true { true => { mkDog() }, false => { mkDog() } })) - 4;
}
"""

_BARE = _SPEAKER + """
pub fn main() nothrow -> Int {
	return hear(match true { true => { mkDog() }, false => { mkDog() } }) - 4;
}
"""


def _walk(node, out):
	out.append(node)
	for v in getattr(node, "__dict__", {}).values():
		if isinstance(v, H.HExpr):
			_walk(v, out)
		elif isinstance(v, H.HBlock):
			for st in v.statements:
				_walk_stmt(st, out)
		elif isinstance(v, (list, tuple)):
			for it in v:
				if isinstance(it, H.HExpr):
					_walk(it, out)
				else:
					for vv in getattr(it, "__dict__", {}).values():
						if isinstance(vv, H.HExpr):
							_walk(vv, out)
						elif isinstance(vv, H.HBlock):
							for s2 in vv.statements:
								_walk_stmt(s2, out)


def _walk_stmt(st, out):
	if isinstance(st, H.HExpr):
		_walk(st, out)
	for v in getattr(st, "__dict__", {}).values():
		if isinstance(v, H.HExpr):
			_walk(v, out)
		elif isinstance(v, H.HBlock):
			for s2 in v.statements:
				_walk_stmt(s2, out)


def _compile_capturing_validated_bodies(tmp_path: Path, source: str):
	"""Compile through the full pipeline, capturing every HIR block the W0
	totality validator (`validate_typed_hir`) inspects — that is the post-check,
	post-materialization HIR where `policy_class` is stamped."""
	src = tmp_path / "main.drift"
	src.write_text(source)
	modules, type_table, exc, module_exports, module_deps, parse_diags = parse_drift_workspace_to_hir(
		[src], module_paths=[tmp_path], stdlib_root=stdlib_root(), test_build_only=True)
	assert parse_diags == [], [str(d) for d in parse_diags]
	func_hirs, signatures, _ = flatten_modules(modules)

	captured: list = []
	orig = TC.validate_typed_hir

	def _wrap(body, *a, **k):
		captured.append(body)
		return orig(body, *a, **k)

	TC.validate_typed_hir = _wrap
	try:
		_ir, checked = compile_to_llvm_ir_for_tests(
			func_hirs=func_hirs, signatures=signatures, exc_env=exc, type_table=type_table,
			module_exports=module_exports, module_deps=module_deps,
			enforce_entrypoint=True, entry="m::main")
	finally:
		TC.validate_typed_hir = orig
	return checked, captured


def test_cfv_iface_coercion_borrow_classifies_coercion(tmp_path: Path) -> None:
	checked, bodies = _compile_capturing_validated_bodies(tmp_path, _EXPLICIT)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], [(d.code, d.message) for d in errors]
	assert not any(d.code == "E_REDUNDANT_ARG_BORROW" for d in checked.diagnostics), [
		(d.code, d.message) for d in checked.diagnostics]

	# The validator runs on EVERY compiled function (stdlib too), so isolate
	# main's body — the one containing the `hear(...)` call — before inspecting.
	def _has_hear_call(body) -> bool:
		ns: list = []
		_walk(body, ns)
		return any(
			isinstance(n, H.HCall) and getattr(getattr(n, "fn", None), "name", None) == "hear"
			for n in ns
		)

	main_bodies = [b for b in bodies if _has_hear_call(b)]
	assert len(main_bodies) == 1, f"expected one validated body with the hear() call, found {len(main_bodies)}"
	nodes: list = []
	_walk(main_bodies[0], nodes)
	# EXACTLY one source-written borrow in main, classified "coercion"
	# (materialization rewrote its subject to the hidden owner temp, but
	# source_written / policy_class survive).
	src_borrows = [n for n in nodes if isinstance(n, H.HBorrow) and getattr(n, "source_written", False)]
	assert len(src_borrows) == 1, f"expected exactly one source-written borrow in main, found {len(src_borrows)}"
	assert src_borrows[0].policy_class == "coercion", (
		f"CFV coercion borrow must classify 'coercion', got {src_borrows[0].policy_class!r} — "
		f"restoring E_REDUNDANT_ARG_BORROW must not over-broaden the classifier, "
		f"and it must not slip through unclassified/exempt")


def test_cfv_iface_bare_form_is_rejected_deletion_changes_typing(tmp_path: Path) -> None:
	# The deletion control: without the borrow, Dog does not widen to &Speaker,
	# so the bare form fails to resolve. This is what makes the `&` a coercion
	# rather than a redundant borrow.
	checked, _ = _compile_capturing_validated_bodies(tmp_path, _BARE)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors, "bare hear(match …) must be rejected (Dog does not widen to &Speaker)"
	assert not any(d.code == "E_REDUNDANT_ARG_BORROW" for d in checked.diagnostics), [
		(d.code, d.message) for d in checked.diagnostics]
