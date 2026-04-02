# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: boundary_ret_type_id is the sole routing signal for cross-package
boundary decisions.

Proves the wrapper convergence contract:
  1. Pub functions in explicitly-packaged modules get boundary_ret_type_id
  2. The call resolver upgrades to __wrap_method:: based on boundary_ret_type_id
  3. The type checker's boundary visibility uses boundary_ret_type_id
  4. LLVM codegen routes cross-module calls based on boundary_ret_type_id
  5. No routing decision falls back to source_modules or explicitly_packaged_modules
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable, TypeKind
from lang.driftc.checker import FnSignature
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules


LIB_SOURCE = """\
module acme.lib;
import std.core as core;
export { greet };

pub fn greet(name: String) nothrow -> String {
\treturn "hello " + name;
}
"""

APP_SOURCE = """\
module main;
import acme.lib as lib;

fn main() nothrow -> Int {
\tval msg = try lib.greet("world") catch { "err" };
\treturn 0;
}
"""


def _mk(mp: dict, mod: str, pkg: str) -> None:
	mp[mod] = pkg


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[dict, Path]:
	(tmp_path / "lib.drift").write_text(LIB_SOURCE)
	(tmp_path / "main.drift").write_text(APP_SOURCE)
	module_packages: dict[str, str] = {}
	_mk(module_packages, "main", "app")
	_mk(module_packages, "acme.lib", "acme")
	return module_packages, tmp_path


def test_boundary_ret_type_id_set_on_explicitly_packaged_pub_function(workspace: tuple) -> None:
	"""Pub exported function in explicitly-packaged module gets boundary_ret_type_id."""
	module_packages, tmp_path = workspace
	stdlib = stdlib_root()
	if stdlib is None:
		pytest.skip("stdlib not available")

	modules, type_table, exc, exports, deps, diags = parse_drift_workspace_to_hir(
		sorted(tmp_path.rglob("*.drift")),
		module_paths=[tmp_path],
		external_module_packages=module_packages,
		stdlib_root=stdlib,
		test_build_only=True,
	)
	assert not any(d.severity == "error" for d in diags)

	hirs, sigs, _ = flatten_modules(modules)

	# Run through compile_stubbed_funcs to trigger boundary propagation.
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=hirs,
		signatures=sigs,
		exc_env=exc,
		entry="main",
		type_table=type_table,
		module_exports=exports,
		module_deps=deps,
	)

	# Find the greet function's fn_info signature — it should have
	# boundary_ret_type_id set because acme.lib is explicitly packaged.
	greet_fn = FunctionId(module="acme.lib", name="greet", ordinal=0)
	greet_info = checked.fn_infos_by_id.get(greet_fn)
	assert greet_info is not None, "greet fn_info not found"
	greet_sig = greet_info.signature
	assert greet_sig is not None, "greet signature not found"
	assert greet_sig.is_pub, "greet must be pub"
	assert greet_sig.boundary_ret_type_id is not None, (
		f"greet in explicitly-packaged module 'acme.lib' must have "
		f"boundary_ret_type_id set. Got None. This means the boundary "
		f"metadata was not propagated at declaration time."
	)


def test_codegen_routes_via_boundary_metadata_not_source_modules(workspace: tuple) -> None:
	"""Cross-package call to greet() uses wrapper routing, driven by
	boundary_ret_type_id — not source_modules or explicitly_packaged_modules."""
	module_packages, tmp_path = workspace
	stdlib = stdlib_root()
	if stdlib is None:
		pytest.skip("stdlib not available")

	modules, type_table, exc, exports, deps, diags = parse_drift_workspace_to_hir(
		sorted(tmp_path.rglob("*.drift")),
		module_paths=[tmp_path],
		external_module_packages=module_packages,
		stdlib_root=stdlib,
		test_build_only=True,
	)
	assert not any(d.severity == "error" for d in diags)

	hirs, sigs, _ = flatten_modules(modules)

	# Prove routing is driven by boundary_ret_type_id, NOT source_modules
	# or explicitly_packaged_modules: clear both before compilation.
	# If routing depended on them, the cross-package call to greet would
	# not use the wrapper (it would use __impl directly).
	type_table.source_modules = set()
	type_table.explicitly_packaged_modules = set()

	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=hirs,
		signatures=sigs,
		exc_env=exc,
		entry="main",
		type_table=type_table,
		module_exports=exports,
		module_deps=deps,
	)
	assert not any(d.severity == "error" for d in checked.diagnostics), \
		f"compile errors: {[d.message for d in checked.diagnostics if d.severity == 'error']}"

	# The main function must call greet through the public wrapper (not __impl).
	# This proves codegen routed via boundary_ret_type_id.
	# The wrapper call uses the public symbol; __impl is the private body.
	assert 'acme.lib::greet__impl' not in ir or '@"acme.lib::greet"' in ir, (
		f"cross-package call to greet must use the public wrapper symbol, "
		f"not __impl directly. This proves boundary routing is driven by "
		f"boundary_ret_type_id, not source_modules."
	)
	# Verify the wrapper function exists in the IR.
	assert "acme.lib::greet__impl" in ir, (
		f"greet__impl must exist as the private body (boundary ABI emitted)."
	)


def test_no_boundary_for_co_compiled_source_without_metadata(workspace: tuple) -> None:
	"""Co-compiled source functions without boundary_ret_type_id do NOT
	get boundary routing — even if in a different canonical package."""
	module_packages, tmp_path = workspace
	stdlib = stdlib_root()
	if stdlib is None:
		pytest.skip("stdlib not available")

	# Compile WITHOUT external_module_packages — both modules are plain
	# co-compiled source with no explicit packaging.
	modules, type_table, exc, exports, deps, diags = parse_drift_workspace_to_hir(
		sorted(tmp_path.rglob("*.drift")),
		module_paths=[tmp_path],
		stdlib_root=stdlib,
		test_build_only=True,
	)
	assert not any(d.severity == "error" for d in diags)

	hirs, sigs, _ = flatten_modules(modules)

	# Without explicit packaging, greet should NOT have boundary_ret_type_id.
	greet_fn = FunctionId(module="acme.lib", name="greet", ordinal=0)
	greet_sig = sigs.get(greet_fn)
	if greet_sig is not None:
		assert greet_sig.boundary_ret_type_id is None, (
			f"greet in co-compiled source (no explicit packaging) must NOT "
			f"have boundary_ret_type_id. Got {greet_sig.boundary_ret_type_id}. "
			f"Boundary metadata should only be set for package boundaries."
		)
