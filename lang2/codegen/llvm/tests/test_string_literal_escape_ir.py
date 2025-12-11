from lang2.driftc.stage2 import MirBuilder, ConstString, Return
from lang2.codegen.llvm import lower_module_to_llvm
from lang2.driftc.stage4 import MirToSSA
from lang2.driftc.stage2.mir_nodes import BasicBlock, MirFunc


def _build_func(body_instrs):
	block = BasicBlock(name="entry", instructions=body_instrs, terminator=Return(value="s"))
	func = MirFunc(name="f", params=[], locals=["s"], blocks={"entry": block}, entry="entry")
	ssa = MirToSSA().run(func)
	# Minimal FnInfo: returning Int for now because lower_ssa_func_to_llvm is test-only
	from lang2.driftc.checker import FnInfo
	finfo = FnInfo(name="f", declared_can_throw=False)
	mod = lower_module_to_llvm({"f": func}, {"f": ssa}, fn_infos={"f": finfo}, type_table=None)
	return mod.render()


def test_string_literal_with_quote_and_backslash_ir():
	# literal: "a\"b\\c" => bytes: 61 22 62 5c 63
	ir = _build_func([ConstString(dest="s", value='a"b\\c')])
	assert 'private unnamed_addr constant [6 x i8] c"a\\22b\\5Cc\\00"' in ir
	assert 'define i64 @f()' in ir  # return type path still Int-only for this helper
	assert 'ret' in ir
