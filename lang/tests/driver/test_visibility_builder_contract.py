# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Contract tests for the test-helper visibility builder
`_build_test_visible_module_names_by_name` (the ONE builder shared by the
pre-typecheck ConstShare synthesis and the typechecker in `compile_stubbed_funcs`).

For SOURCE modules these pin the same semantics the CLI's import/re-export graph
uses, so the two pipelines cannot silently diverge: a module sees ITSELF + prelude
modules + its DIRECT imports + transitively RE-EXPORTED modules — but NOT modules
reachable only through another module's PRIVATE (non-re-exported) imports.

NOT parity with the CLI: external PACKAGE modules absent from the import graph
(`module_deps`) fall back to broad visibility (the temporary "K25" fallback, since
DMIR does not yet serialize the per-package import graph).  The CLI scopes package
modules strictly — own-package siblings + prelude + other packages + stdlib, and
explicitly NOT consumer source (see `driftc.py` "Option B Phase 2a").  This is an
intentional test-helper-only broadening; it does not affect ConstShare derivation
for the consumer's own types (package types were derived at package build time).
"""
from __future__ import annotations

from lang.driftc.driftc import _build_test_visible_module_names_by_name
from lang.driftc.core.function_id import FunctionId


def _reexport_types(*modules):
	"""A module_exports `reexports` block re-exporting a struct from each module."""
	structs = {f"T{i}": {"module": m} for i, m in enumerate(modules)}
	return {"reexports": {"types": {"structs": structs}}}


def _build(*, module_deps, module_exports=None, prelude_enabled=False, signatures=None):
	sigs = {}
	for m in (signatures or []):
		sigs[FunctionId(module=m, name="f", ordinal=0)] = object()
	return _build_test_visible_module_names_by_name(
		prelude_enabled=prelude_enabled,
		signatures_by_id=sigs,
		module_exports=module_exports if module_exports is not None else {},
		module_deps=module_deps,
	)


def test_direct_imports_are_visible() -> None:
	vis = _build(module_deps={"A": {"B"}, "B": set()})
	assert "B" in vis["A"] and "A" in vis["A"]
	assert vis["B"] == {"B"}


def test_private_transitive_imports_are_not_visible() -> None:
	# A imports B; B privately imports C (no re-export) -> C must NOT reach A.
	vis = _build(module_deps={"A": {"B"}, "B": {"C"}, "C": set()})
	assert "B" in vis["A"]
	assert "C" not in vis["A"], "a private transitive import must not be visible"
	assert "C" in vis["B"]


def test_reexports_are_transitively_visible() -> None:
	# A imports B; B RE-EXPORTS C -> C is visible to A.
	vis = _build(
		module_deps={"A": {"B"}, "B": set(), "C": set()},
		module_exports={"A": {}, "B": _reexport_types("C"), "C": {}},
	)
	assert "C" in vis["A"], "a re-exported module must be transitively visible"


def test_prelude_modules_are_added() -> None:
	vis = _build(
		module_deps={"app": set()},
		module_exports={"std.iter": {}, "std.containers": {}},
		prelude_enabled=True,
		signatures=["lang.core", "app"],
	)
	assert {"lang.core", "std.iter", "std.containers"} <= vis["app"]


def test_prelude_disabled_adds_nothing() -> None:
	vis = _build(
		module_deps={"app": set()},
		module_exports={"std.iter": {}},
		prelude_enabled=False,
		signatures=["lang.core", "app"],
	)
	assert "lang.core" not in vis["app"]
	assert "std.iter" not in vis["app"]


def test_package_modules_absent_from_deps_get_test_helper_fallback() -> None:
	# Test-helper-only K25 fallback (NOT CLI parity): a package module present in
	# module_exports but absent from module_deps (its import graph isn't serialized)
	# falls back to broad visibility.  The CLI deliberately scopes package modules
	# tighter and does NOT expose consumer source ("app") to them; this asserts the
	# documented test-helper fallback, not the CLI's behavior.
	vis = _build(
		module_deps={"app": set()},
		module_exports={"app": {}, "pkg.mod": {}, "pkg.other": {}},
	)
	assert "pkg.mod" in vis
	assert {"app", "pkg.mod", "pkg.other"} <= vis["pkg.mod"], (
		"documents the broad K25 fallback for package modules absent from module_deps"
	)
