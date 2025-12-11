from pathlib import Path
import pytest

from lang2 import stage1 as H
from lang2.parser import parse_drift_to_hir


def test_parse_simple_return(tmp_path: Path):
	src = tmp_path / "main.drift"
	src.write_text(
		"""
fn main() returns Int {
    return 42;
}
"""
	)
	func_hirs, sigs, _type_table, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	assert set(func_hirs.keys()) == {"main"}
	assert sigs["main"].return_type.name == "Int"
	block = func_hirs["main"]
	assert len(block.statements) == 1
	assert isinstance(block.statements[0], H.HReturn)
	assert isinstance(block.statements[0].value, H.HLiteralInt)


def test_parse_fnresult_ok(tmp_path: Path):
	src = tmp_path / "main.drift"
	src.write_text(
		"""
fn callee() returns FnResult<Int, Error> {
    return Ok(1);
}
fn main() returns Int {
    return callee();
}
"""
	)
	func_hirs, sigs, _type_table, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	assert set(func_hirs.keys()) == {"callee", "main"}
	assert sigs["callee"].return_type.name == "FnResult"
	assert sigs["main"].return_type.name == "Int"
	callee = func_hirs["callee"]
	main = func_hirs["main"]
	assert isinstance(callee.statements[0], H.HReturn)
	assert isinstance(callee.statements[0].value, H.HResultOk)
	assert isinstance(main.statements[0], H.HReturn)
	assert isinstance(main.statements[0].value, H.HCall)


def test_parse_ok_as_attr_stays_call(tmp_path: Path):
	src = tmp_path / "main.drift"
	src.write_text(
		"""
fn main() returns Int {
    return ns.Ok(1);
}
"""
	)
	func_hirs, sigs, _type_table, diagnostics = parse_drift_to_hir(src)
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
fn main() returns FnResult<Int, Error> {
    throw err_val;
}
"""
	)
	func_hirs, sigs, _type_table, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	assert sigs["main"].return_type.name == "FnResult"
	main = func_hirs["main"]
	assert isinstance(main.statements[0], H.HThrow)


def test_parse_raise_expr_maps_to_throw(tmp_path: Path):
	src = tmp_path / "main.drift"
	src.write_text(
		"""
fn main() returns FnResult<Int, Error> {
    raise err_val;
}
"""
	)
	func_hirs, sigs, _type_table, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	assert sigs["main"].return_type.name == "FnResult"
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
	_, _sigs, _type_table, diagnostics = parse_drift_to_hir(src)
	assert diagnostics, "expected diagnostic for reference-qualified implement header"
	assert "nominal type" in diagnostics[0].message


def test_parse_unsupported_stmt_fails_loudly(tmp_path: Path):
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
	with pytest.raises(NotImplementedError):
		parse_drift_to_hir(src)


def test_fnresult_typeids_are_resolved(tmp_path: Path):
	"""
	Ensure resolver produces real TypeIds for FnResult return types (no fallback
	string/tuple resolution). Return type and error side should map to Int/Error
	TypeIds on the shared TypeTable.
	"""
	src = tmp_path / "main.drift"
	src.write_text(
		"""
fn drift_main() returns FnResult<Int, Error> {
    return Ok(1);
}
"""
	)
	_, sigs, type_table, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	sig = sigs.get("drift_main") or sigs["main"]
	assert sig.return_type_id is not None
	assert sig.error_type_id is not None


def test_duplicate_function_definition_reports_diagnostic(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
fn main() returns Int { return 0; }
fn main() returns Int { return 1; }
"""
	)
	_, sigs, _type_table, diagnostics = parse_drift_to_hir(src)
	assert any("duplicate function definition" in d.message for d in diagnostics)
	assert "main" in sigs
