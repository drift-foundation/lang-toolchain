from pathlib import Path
import pytest

from lang2 import stage1 as H
from lang2.parser import parse_drift_to_hir


def test_parse_simple_return(tmp_path: Path):
	src = tmp_path / "main.drift"
	src.write_text("""
fn drift_main() returns Int {
    return 42;
}
""")
	func_hirs, sigs = parse_drift_to_hir(src)
	assert set(func_hirs.keys()) == {"drift_main"}
	assert sigs["drift_main"].return_type == "Int"
	block = func_hirs["drift_main"]
	assert len(block.statements) == 1
	assert isinstance(block.statements[0], H.HReturn)
	assert isinstance(block.statements[0].value, H.HLiteralInt)


def test_parse_fnresult_ok(tmp_path: Path):
	src = tmp_path / "main.drift"
	src.write_text("""
fn callee() returns FnResult<Int, Error> {
    return Ok(1);
}
fn drift_main() returns Int {
    return callee();
}
""")
	func_hirs, sigs = parse_drift_to_hir(src)
	assert set(func_hirs.keys()) == {"callee", "drift_main"}
	assert sigs["callee"].return_type.startswith("FnResult")
	assert sigs["drift_main"].return_type == "Int"
	callee = func_hirs["callee"]
	main = func_hirs["drift_main"]
	assert isinstance(callee.statements[0], H.HReturn)
	assert isinstance(callee.statements[0].value, H.HResultOk)
	assert isinstance(main.statements[0], H.HReturn)
	assert isinstance(main.statements[0].value, H.HCall)


def test_parse_ok_as_attr_stays_call(tmp_path: Path):
	src = tmp_path / "main.drift"
	src.write_text("""
fn drift_main() returns Int {
    return ns.Ok(1);
}
""")
	func_hirs, sigs = parse_drift_to_hir(src)
	assert set(func_hirs.keys()) == {"drift_main"}
	assert sigs["drift_main"].return_type == "Int"
	main = func_hirs["drift_main"]
	assert isinstance(main.statements[0], H.HReturn)
	# ns.Ok should not be rewritten to HResultOk (attr call stays a normal call)
	assert isinstance(main.statements[0].value, (H.HCall, H.HMethodCall))


def test_parse_throw_stmt(tmp_path: Path):
	src = tmp_path / "main.drift"
	src.write_text("""
fn drift_main() returns FnResult<Int, Error> {
    throw err_val;
}
""")
	func_hirs, sigs = parse_drift_to_hir(src)
	assert sigs["drift_main"].return_type.startswith("FnResult")
	main = func_hirs["drift_main"]
	assert isinstance(main.statements[0], H.HThrow)


def test_parse_raise_expr_maps_to_throw(tmp_path: Path):
	src = tmp_path / "main.drift"
	src.write_text("""
fn drift_main() returns FnResult<Int, Error> {
    raise err_val;
}
""")
	func_hirs, sigs = parse_drift_to_hir(src)
	assert sigs["drift_main"].return_type.startswith("FnResult")
	main = func_hirs["drift_main"]
	assert len(main.statements) == 1
	assert isinstance(main.statements[0], H.HThrow)


def test_parse_unsupported_stmt_fails_loudly(tmp_path: Path):
	src = tmp_path / "main.drift"
	src.write_text("""
fn drift_main() returns Int {
    while true {
        return 1;
    }
}
""")
	with pytest.raises(NotImplementedError):
		parse_drift_to_hir(src)
