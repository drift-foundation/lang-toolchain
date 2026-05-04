import textwrap
from pathlib import Path

from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def test_hidden_lambda_local_types_for_try_temps(tmp_path: Path) -> None:
	source = tmp_path / "main.drift"
	source.write_text(textwrap.dedent(
		"""
		module main;

		import std.concurrent as conc;

		error Oops {}
		struct Stream { v: Int }

		fn accept() nothrow -> Stream { return Stream(v = 1); }

		fn handle(var s: Stream) -> Int {
			if s.v == 0 {
				throw Oops();
			}
			return s.v;
		}

		fn main() nothrow -> Int {
			while true {
				val s = accept();
				val _ = conc.spawn_cb(| | captures(move s) => {
					return try handle(move s) catch { 1 };
				});
				return 0;
			}
			return 0;
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
		if getattr(fn_id, "name", "") == "__lambda_cb_main_0_0":
			lambda_fn = func
			break
	assert lambda_fn is not None
	unknown = table.ensure_unknown()
	tracked = []
	for local_name in lambda_fn.locals:
		if local_name.startswith("__try_expr_tmp") or local_name.startswith("__try_err") or local_name.startswith("__call_ok"):
			tracked.append(local_name)
			assert lambda_fn.local_types.get(local_name) != unknown
	assert tracked
