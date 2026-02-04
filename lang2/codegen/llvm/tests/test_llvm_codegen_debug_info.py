# SSA-first LLVM debug info smoke tests.

from __future__ import annotations

from lang2.codegen.llvm import lower_module_to_llvm
from lang2.codegen.llvm.test_utils import host_word_bits
from lang2.driftc.checker import FnInfo, FnSignature
from lang2.driftc.core.function_id import FunctionId
from lang2.driftc.core.span import Span
from lang2.driftc.core.types_core import TypeTable
from lang2.driftc.stage2 import AssignSSA, BasicBlock, ConstInt, MirFunc, Return
from lang2.driftc.stage4 import MirToSSA


def _build_ir_with_span(span_file: str) -> str:
	table = TypeTable()
	int_ty = table.ensure_int()

	entry = BasicBlock(
		name="entry",
		instructions=[
			ConstInt(dest="t0", value=1),
			AssignSSA(dest="t1", src="t0"),
		],
		terminator=Return(value="t1"),
	)
	entry.instructions[0].span = Span(file=span_file, line=3, column=5)
	entry.instructions[1].span = Span(file=span_file, line=4, column=7)
	entry.terminator.span = Span(file=span_file, line=5, column=3)

	fn_id = FunctionId(module="main", name="main", ordinal=0)
	sig = FnSignature(name="main", return_type_id=int_ty, declared_can_throw=False, loc=Span(file=span_file, line=1, column=1))
	fn_info = FnInfo(fn_id=fn_id, name="main", declared_can_throw=False, return_type_id=int_ty, signature=sig)
	mir = MirFunc(fn_id=fn_id, name="main", params=[], locals=[], blocks={"entry": entry}, entry="entry")
	ssa = MirToSSA().run(mir)
	mod = lower_module_to_llvm(
		funcs={fn_id: mir},
		ssa_funcs={fn_id: ssa},
		fn_infos={fn_id: fn_info},
		type_table=table,
		word_bits=host_word_bits(),
	)
	return mod.render()


def test_debug_noop_assignssa_does_not_get_dbg():
	ir = _build_ir_with_span('src/fi"le\\\\name.drift')
	dbg_lines = [line for line in ir.splitlines() if " !dbg !" in line]
	assert len(dbg_lines) == 3
	assert any(line.startswith("define ") and " !dbg !" in line for line in ir.splitlines())
	assert any(" = add " in line and " !dbg !" in line for line in ir.splitlines())
	assert any(line.strip().startswith("ret ") and " !dbg !" in line for line in ir.splitlines())
	assert not any("t1 =" in line for line in ir.splitlines())
	for line in ir.splitlines():
		if line.strip() == "entry:":
			assert " !dbg !" not in line


def test_debug_strings_are_escaped():
	ir = _build_ir_with_span('dir/fi"le\\name.drift')
	di_line = next(line for line in ir.splitlines() if "DIFile(" in line)
	assert 'filename: "fi\\\"le\\\\name.drift"' in di_line
	assert 'directory: "dir"' in di_line


def test_debug_metadata_presence():
	ir = _build_ir_with_span("src/main.drift")
	assert "!llvm.dbg.cu" in ir
	assert "!llvm.module.flags" in ir
	assert "DICompileUnit" in ir
	assert "DIFile" in ir
	assert "DISubprogram" in ir
	assert "DILocation" in ir
