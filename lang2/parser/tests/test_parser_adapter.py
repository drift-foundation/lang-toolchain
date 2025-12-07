from pathlib import Path

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
