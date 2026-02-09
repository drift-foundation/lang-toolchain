from lang.driftc.core.function_id import FunctionId
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-10
"""
LLVM lowering for String literals and ->.
"""

from lang.driftc.checker import FnInfo, FnSignature
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage2 import BasicBlock, Call, ConstString, LoadLocal, MirFunc, Return, StoreLocal, StringRelease
from lang.driftc.stage2.string_arc import insert_string_arc
from lang.driftc.stage4.ssa import MirToSSA
from lang.codegen.llvm import lower_ssa_func_to_llvm, lower_module_to_llvm
from lang.codegen.llvm.test_utils import host_word_bits


def _string_type(table: TypeTable):
	str_ty = table.new_scalar("String")
	table._string_type = str_ty  # type: ignore[attr-defined]
	return str_ty


def test_string_literal_return_ir():
	table = TypeTable()
	str_ty = _string_type(table)
	word_bits = host_word_bits()
	word_ty = f"i{word_bits}"

	block = BasicBlock(
		name="entry",
		instructions=[
			ConstString(dest="t0", value="abc"),
		],
		terminator=Return(value="t0"),
	)
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	func = MirFunc(fn_id=fn_id, name="f", params=[], locals=[], blocks={"entry": block}, entry="entry")
	ssa = MirToSSA().run(func)
	sig = FnSignature(name="f", return_type_id=str_ty, param_type_ids=[])
	fn_info = FnInfo(fn_id=fn_id, name="f", declared_can_throw=False, signature=sig, return_type_id=str_ty)

	ir = lower_ssa_func_to_llvm(func, ssa, fn_info, {fn_id: fn_info}, type_table=table, word_bits=word_bits)

	assert f"%DriftString = type {{ {word_ty}, i8*" in ir
	assert (
		f'@.str0 = private unnamed_addr constant {{ {word_ty}, {word_ty}, [4 x i8] }} '
		f'{{ {word_ty} 1, {word_ty} 1, [4 x i8] c"abc\\00" }}'
	) in ir
	assert "define %DriftString @f()" in ir
	assert "ret %DriftString" in ir


def test_string_utf8_literal_ir():
	"""UTF-8 literals should be escaped byte-wise in the LLVM global."""
	table = TypeTable()
	str_ty = _string_type(table)
	word_bits = host_word_bits()
	word_ty = f"i{word_bits}"

	block = BasicBlock(
		name="entry",
		instructions=[
			ConstString(dest="t0", value="Solidarność"),
		],
		terminator=Return(value="t0"),
	)
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	func = MirFunc(fn_id=fn_id, name="f", params=[], locals=[], blocks={"entry": block}, entry="entry")
	ssa = MirToSSA().run(func)
	sig = FnSignature(name="f", return_type_id=str_ty, param_type_ids=[])
	fn_info = FnInfo(fn_id=fn_id, name="f", declared_can_throw=False, signature=sig, return_type_id=str_ty)

	ir = lower_ssa_func_to_llvm(func, ssa, fn_info, {fn_id: fn_info}, type_table=table, word_bits=word_bits)

	assert f"%DriftString = type {{ {word_ty}, i8*" in ir
	assert (
		f'@.str0 = private unnamed_addr constant {{ {word_ty}, {word_ty}, [14 x i8] }} '
		f'{{ {word_ty} 1, {word_ty} 1, [14 x i8] c"Solidarno\\C5\\9B\\C4\\87\\00" }}'
	) in ir
	assert "define %DriftString @f()" in ir
	assert "ret %DriftString" in ir


def test_string_pass_through_call_ir():
	table = TypeTable()
	str_ty = _string_type(table)
	word_bits = host_word_bits()
	word_ty = f"i{word_bits}"

	# callee: return its string argument
	callee_block = BasicBlock(
		name="entry",
		instructions=[
		],
		terminator=Return(value="s"),
	)
	callee_id = FunctionId(module="main", name="id", ordinal=0)
	callee = MirFunc(fn_id=callee_id, name="id", params=["s"], locals=[], blocks={"entry": callee_block}, entry="entry")
	callee_ssa = MirToSSA().run(callee)
	callee_sig = FnSignature(name="id", param_type_ids=[str_ty], return_type_id=str_ty)
	callee_info = FnInfo(fn_id=callee_id, name="id", declared_can_throw=False, signature=callee_sig, return_type_id=str_ty)

	# caller: pass literal through id
	caller_block = BasicBlock(
		name="entry",
		instructions=[
			ConstString(dest="t0", value="abc"),
			Call(dest="t1", fn_id=callee_id, args=["t0"], can_throw=False),
		],
		terminator=Return(value="t1"),
	)
	caller_id = FunctionId(module="main", name="main", ordinal=0)
	caller = MirFunc(fn_id=caller_id, name="main", params=[], locals=["t0", "t1"], blocks={"entry": caller_block}, entry="entry")
	caller_ssa = MirToSSA().run(caller)
	caller_sig = FnSignature(name="main", param_type_ids=[], return_type_id=str_ty)
	caller_info = FnInfo(fn_id=caller_id, name="main", declared_can_throw=False, signature=caller_sig, return_type_id=str_ty)

	mod = lower_module_to_llvm(
		{callee_id: callee, caller_id: caller},
		{callee_id: callee_ssa, caller_id: caller_ssa},
		{callee_id: callee_info, caller_id: caller_info},
		type_table=table,
		word_bits=word_bits,
	)
	ir = mod.render()

	assert f"%DriftString = type {{ {word_ty}, i8*" in ir
	assert (
		f'@.str0 = private unnamed_addr constant {{ {word_ty}, {word_ty}, [4 x i8] }} '
		f'{{ {word_ty} 1, {word_ty} 1, [4 x i8] c"abc\\00" }}'
	) in ir
	assert "define %DriftString @id(%DriftString %s)" in ir
	assert "define %DriftString @main()" in ir
	assert "call %DriftString @id(%DriftString %t0)" in ir
	assert "ret %DriftString" in ir


def test_string_literal_overwrite_emits_release():
	table = TypeTable()
	str_ty = _string_type(table)

	block = BasicBlock(
		name="entry",
		instructions=[
			ConstString(dest="t0", value="a"),
			StoreLocal(local="s", value="t0"),
			ConstString(dest="t1", value="b"),
			StoreLocal(local="s", value="t1"),
			LoadLocal(dest="t2", local="s"),
		],
		terminator=Return(value="t2"),
	)
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	func = MirFunc(
		fn_id=fn_id,
		name="f",
		params=[],
		locals=["s"],
		blocks={"entry": block},
		entry="entry",
		local_types={"s": str_ty, "t0": str_ty, "t1": str_ty, "t2": str_ty},
	)
	sig = FnSignature(name="f", return_type_id=str_ty, param_type_ids=[])
	fn_info = FnInfo(fn_id=fn_id, name="f", declared_can_throw=False, signature=sig, return_type_id=str_ty)

	func = insert_string_arc(func, type_table=table, fn_infos={fn_id: fn_info})
	ssa = MirToSSA().run(func)

	ir = lower_ssa_func_to_llvm(func, ssa, fn_info, {fn_id: fn_info}, type_table=table, word_bits=host_word_bits())

	assert "call void @drift_string_release(%DriftString" in ir


def test_string_arc_return_from_local_without_temp_type_does_not_release_return_value():
	table = TypeTable()
	str_ty = _string_type(table)
	block = BasicBlock(
		name="entry",
		instructions=[
			ConstString(dest="t0", value="x"),
			StoreLocal(local="s", value="t0"),
			LoadLocal(dest="t1", local="s"),
		],
		terminator=Return(value="t1"),
	)
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	func = MirFunc(
		fn_id=fn_id,
		name="f",
		params=[],
		locals=["s"],
		blocks={"entry": block},
		entry="entry",
		local_types={"s": str_ty, "t0": str_ty},
	)
	sig = FnSignature(name="f", return_type_id=str_ty, param_type_ids=[])
	fn_info = FnInfo(fn_id=fn_id, name="f", declared_can_throw=False, signature=sig, return_type_id=str_ty)
	func = insert_string_arc(func, type_table=table, fn_infos={fn_id: fn_info})
	entry = func.blocks["entry"]
	assert isinstance(entry.terminator, Return)
	ret_val = entry.terminator.value
	assert ret_val is not None
	assert ret_val != "t1"
	for instr in entry.instructions:
		if isinstance(instr, StringRelease):
			assert instr.value != ret_val
