from __future__ import annotations

from lang2.driftc.checker import FnSignature
from lang2.driftc.driftc import compile_to_llvm_ir_for_tests
from lang2.driftc import stage1 as H


def test_entry_wrapper_uses_main_symbol() -> None:
	func_hirs = {
		"drift_main": H.HBlock(statements=[H.HReturn(value=H.HLiteralInt(value=1))])
	}
	signatures = {"drift_main": FnSignature(name="drift_main", return_type="Int", declared_can_throw=False)}

	ir, _ = compile_to_llvm_ir_for_tests(func_hirs=func_hirs, signatures=signatures, entry="drift_main")
	assert "@drift_main" in ir
	assert "main::drift_main" not in ir
