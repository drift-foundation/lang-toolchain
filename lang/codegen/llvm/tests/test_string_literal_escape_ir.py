from lang.driftc.core.function_id import FunctionId
from lang.driftc.stage2 import ConstString, Return
from lang.codegen.llvm import lower_module_to_llvm
from lang.codegen.llvm.test_utils import host_word_bits
from lang.driftc.stage4 import MirToSSA
from lang.driftc.stage2.mir_nodes import BasicBlock, MirFunc


def _build_func(body_instrs):
	block = BasicBlock(name="entry", instructions=body_instrs, terminator=Return(value="s"))
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	func = MirFunc(fn_id=fn_id, name="f", params=[], locals=["s"], blocks={"entry": block}, entry="entry")
	ssa = MirToSSA().run(func)
	# Minimal FnInfo: returning Int for now because lower_ssa_func_to_llvm is test-only
	from lang.driftc.checker import FnInfo
	finfo = FnInfo(fn_id=fn_id, name="f", declared_can_throw=False)
	mod = lower_module_to_llvm({fn_id: func}, {fn_id: ssa}, fn_infos={fn_id: finfo}, type_table=None, word_bits=host_word_bits())
	return mod.render()


def test_string_literal_with_quote_and_backslash_ir():
	# literal: "a\"b\\c" => bytes: 61 22 62 5c 63
	word_bits = host_word_bits()
	word_ty = f"i{word_bits}"
	ir = _build_func([ConstString(dest="s", value='a"b\\c')])
	assert (
		f'private unnamed_addr constant {{ {word_ty}, {word_ty}, [6 x i8] }} '
		f'{{ {word_ty} 1, {word_ty} 5, [6 x i8] c"a\\22b\\5Cc\\00" }}'
	) in ir
	assert f'define {word_ty} @f()' in ir  # return type path still Int-only for this helper
	assert 'ret' in ir
