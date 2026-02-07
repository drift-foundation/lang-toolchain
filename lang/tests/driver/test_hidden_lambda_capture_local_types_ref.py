import textwrap
from pathlib import Path

from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.core.types_core import TypeKind


def test_hidden_lambda_ref_capture_local_type_is_ref(tmp_path: Path) -> None:
	source = tmp_path / "main.drift"
	source.write_text(textwrap.dedent(
		"""
		module main

		import std.core as core;

		fn main() nothrow -> Int {
			var count = core.cell(0);
			(| | captures(count) => {
				count.set(count.get() + 1);
				return 0;
			})();
			return count.get();
		}
		"""
	))
	modules, table, excs, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		paths=[source],
		stdlib_root=stdlib_root(),
	)
	assert not diags
	func_hirs, signatures, _fn_ids = flatten_modules(modules)
	mir_funcs, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=excs,
		type_table=table,
		module_exports=module_exports,
		module_deps=module_deps,
		build_ssa=False,
		return_checked=True,
	)
	assert not any(d.severity == "error" for d in checked.diagnostics)
	lambda_fn = None
	for fn_id, func in mir_funcs.items():
		if getattr(fn_id, "name", "") == "__lambda_main_0_0":
			lambda_fn = func
			break
	assert lambda_fn is not None
	assert "count" in lambda_fn.locals
	count_ty = lambda_fn.local_types.get("count")
	assert count_ty is not None
	assert table.get(count_ty).kind is TypeKind.REF
