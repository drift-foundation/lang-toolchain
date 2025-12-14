from pathlib import Path
import pytest

from lang2.driftc import stage1 as H
from lang2.driftc.parser import parse_drift_to_hir


def test_parse_simple_return(tmp_path: Path):
	src = tmp_path / "main.drift"
	src.write_text(
		"""
fn main() returns Int {
    return 42;
}
"""
	)
	func_hirs, sigs, _type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	assert set(func_hirs.keys()) == {"main"}
	assert sigs["main"].return_type.name == "Int"
	block = func_hirs["main"]
	assert len(block.statements) == 1
	assert isinstance(block.statements[0], H.HReturn)
	assert isinstance(block.statements[0].value, H.HLiteralInt)


def test_parse_fnresult_ok(tmp_path: Path):
	"""
	FnResult is an internal ABI carrier in lang2, not a surface type.

	The parser should accept the syntax (so we can diagnose it with spans), but
	should emit an error diagnostic instructing users to write `returns T`.
	"""
	src = tmp_path / "main.drift"
	src.write_text(
		"""
fn callee() returns FnResult<Int, Error> {
    return 1;
}
"""
	)
	_func_hirs, _sigs, _type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics
	assert any("internal-only type 'FnResult'" in d.message for d in diagnostics)


def test_parse_ok_as_attr_stays_call(tmp_path: Path):
	src = tmp_path / "main.drift"
	src.write_text(
		"""
fn main() returns Int {
    return ns.Ok(1);
}
"""
	)
	func_hirs, sigs, _type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	assert set(func_hirs.keys()) == {"main"}
	assert sigs["main"].return_type.name == "Int"
	main = func_hirs["main"]
	assert isinstance(main.statements[0], H.HReturn)
	# ns.Ok should not be rewritten to HResultOk (attr call stays a normal call)
	assert isinstance(main.statements[0].value, (H.HCall, H.HMethodCall))


def test_parse_throw_stmt(tmp_path: Path):
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module m

exception Boom()

fn main() returns Int {
    throw Boom {};
}
"""
	)
	func_hirs, sigs, _type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	assert sigs["main"].return_type.name == "Int"
	main = func_hirs["main"]
	assert isinstance(main.statements[0], H.HThrow)


def test_parse_raise_expr_maps_to_throw(tmp_path: Path):
	src = tmp_path / "main.drift"
	src.write_text(
		"""
fn main() returns Int {
    raise err_val;
}
"""
	)
	func_hirs, sigs, _type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	assert sigs["main"].return_type.name == "Int"
	main = func_hirs["main"]
	assert len(main.statements) == 1
	assert isinstance(main.statements[0], H.HThrow)


def test_implement_header_rejects_reference_target(tmp_path: Path):
	src = tmp_path / "impl_ref.drift"
	src.write_text(
		"""
implement &Point {
    fn move(self: &Point) returns Void { return; }
}
"""
	)
	_, _sigs, _type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics, "expected diagnostic for reference-qualified implement header"
	assert "nominal type" in diagnostics[0].message


def test_parse_while_stmt_lowering(tmp_path: Path):
	src = tmp_path / "main.drift"
	src.write_text(
		"""
fn main() returns Int {
    while true {
        return 1;
    }
}
"""
	)
	func_hirs, _sigs, _type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	block = func_hirs["main"]
	assert isinstance(block.statements[0], H.HLoop)


def test_fnresult_typeids_are_resolved(tmp_path: Path):
	"""
	Ensure the resolver produces real TypeIds for a normal return type.

	FnResult is internal-only, but the type resolver still must produce real
	TypeIds (no fallback string/tuple resolution) for surface types.
	"""
	src = tmp_path / "main.drift"
	src.write_text(
		"""
fn drift_main() returns Int {
    return 1;
}
"""
	)
	_, sigs, type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	sig = sigs.get("drift_main") or sigs["main"]
	assert sig.return_type_id is not None
	assert type_table.get(sig.return_type_id).name == "Int"


def test_duplicate_function_definition_reports_diagnostic(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
fn main() returns Int { return 0; }
fn main() returns Int { return 1; }
"""
	)
	_, sigs, _type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert any("duplicate function definition" in d.message for d in diagnostics)
	assert "main" in sigs
