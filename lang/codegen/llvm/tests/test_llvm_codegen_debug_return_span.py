# SSA-first LLVM debug info regression: return span survives ownership normalization.

from lang.codegen.llvm import lower_module_to_llvm
from lang.codegen.llvm.test_utils import host_word_bits
from lang.driftc.checker import FnInfo, FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.span import Span
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage2 import BasicBlock, ConstInt, MirFunc, Return
from lang.driftc.stage2.ownership_normalization import normalize_ownership_mir
from lang.driftc.stage4 import MirToSSA


def _ret_dbg_id(ir: str) -> str:
	for line in ir.splitlines():
		if line.strip().startswith("ret ") and " !dbg !" in line:
			return line.rsplit("!dbg !", 1)[1].strip()
	raise AssertionError("ret instruction missing !dbg")


def _dilocation_line(ir: str, dbg_id: str) -> str:
	needle = f"!{dbg_id} = !DILocation("
	for line in ir.splitlines():
		if line.startswith(needle):
			return line
	raise AssertionError(f"missing DILocation for !{dbg_id}")


def test_debug_return_span_survives_ownership_normalization():
	table = TypeTable()
	int_ty = table.ensure_int()

	block = BasicBlock(
		name="entry",
		instructions=[ConstInt(dest="t0", value=1)],
		terminator=Return(value="t0"),
	)
	block.instructions[0].span = Span(file="src/main.drift", line=10, column=5)
	block.terminator.span = Span(file="src/main.drift", line=12, column=2)

	fn_id = FunctionId(module="main", name="main", ordinal=0)
	sig = FnSignature(
		name="main",
		return_type_id=int_ty,
		param_type_ids=[],
		declared_can_throw=False,
		loc=Span(file="src/main.drift", line=1, column=1),
	)
	fn_info = FnInfo(fn_id=fn_id, name="main", declared_can_throw=False, signature=sig, return_type_id=int_ty)
	mir = MirFunc(fn_id=fn_id, name="main", params=[], locals=[], blocks={"entry": block}, entry="entry")

	mir = normalize_ownership_mir(mir, type_table=table, fn_infos={fn_id: fn_info})
	ssa = MirToSSA().run(mir)
	mod = lower_module_to_llvm(
		funcs={fn_id: mir},
		ssa_funcs={fn_id: ssa},
		fn_infos={fn_id: fn_info},
		type_table=table,
		word_bits=host_word_bits(),
	)
	ir = mod.render()

	dbg_id = _ret_dbg_id(ir)
	di_line = _dilocation_line(ir, dbg_id)
	assert "line: 12" in di_line
