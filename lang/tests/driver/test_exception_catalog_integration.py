# vim: set noexpandtab: -*- indent-tabs-mode: t -*-

from pathlib import Path

from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.core.function_id import function_symbol
from lang.driftc.parser import parse_drift_to_hir


def _compile_source(src: str, tmp_path: Path):
	path = tmp_path / "main.drift"
	path.write_text(src)
	module, type_table, exc_catalog, diagnostics = parse_drift_to_hir(path)
	assert diagnostics == []
	mir_funcs, checked = compile_stubbed_funcs(
		func_hirs=module.func_hirs,
		signatures=module.signatures_by_id,
		exc_env=exc_catalog,
		type_table=type_table,
		return_checked=True,
	)
	return mir_funcs, checked, module.fn_ids_by_name


def test_catch_unknown_event_reports_diagnostic(tmp_path: Path) -> None:
	_, checked, _fn_ids_by_name = _compile_source(
		"""
module main;

error Boom {}
fn boom() -> Int {
    throw Boom();
}

pub fn main() -> Int {
    try {
        boom();
    } catch Unknown(e) {
    }
    return 1;
}
""",
		tmp_path,
	)
	assert checked.diagnostics
	assert any("unknown catch event" in d.message for d in checked.diagnostics)


def test_declared_exception_enables_catch(tmp_path: Path) -> None:
	mir_funcs, checked, fn_ids_by_name = _compile_source(
		"""
module main;

error Boom { msg: String }
fn boom() -> Int {
    throw Boom(msg = "boom");
}

pub fn main() -> Int {
    try {
        boom();
    } catch Boom(e) {
        return 0;
    }
    return 1;
}
""",
		tmp_path,
	)
	# No checker diagnostics; try lowering should succeed with event codes.
	assert checked.diagnostics == []
	main_ids = fn_ids_by_name.get("main") or []
	if not main_ids:
		qualified = [name for name in fn_ids_by_name.keys() if name.endswith("::main")]
		if len(qualified) == 1:
			main_ids = fn_ids_by_name.get(qualified[0]) or []
	assert len(main_ids) == 1
	assert any(function_symbol(fn_id) == function_symbol(main_ids[0]) for fn_id in mir_funcs)
