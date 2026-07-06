from lang.driftc.core.function_id import FunctionId
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-10
"""
Headers and call lowering for typed parameters (Int, String).
"""

from lang.driftc.checker import FnInfo, FnSignature
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage2 import BasicBlock, Call, ConstInt, ConstString, MirFunc, Return
from lang.driftc.stage4.ssa import MirToSSA
from lang.codegen.llvm import lower_module_to_llvm
from lang.codegen.llvm.test_utils import host_word_bits


def _int_and_string_types():
    table = TypeTable()
    int_ty = table.new_scalar("Int")
    str_ty = table.new_scalar("String")
    table._int_type = int_ty  # type: ignore[attr-defined]
    table._string_type = str_ty  # type: ignore[attr-defined]
    return table, int_ty, str_ty


def _fn_type(table: TypeTable, int_ty: int, *, can_throw: bool) -> int:
    return table.ensure_function([int_ty], int_ty, can_throw=can_throw)


def test_int_param_header_and_call():
    table, int_ty, _ = _int_and_string_types()

    # add(x: Int, y: Int) -> Int { return x }
    add_block = BasicBlock(name="entry", instructions=[], terminator=Return(value="x"))
    add_id = FunctionId(module="main", name="add", ordinal=0)
    add = MirFunc(fn_id=add_id, name="add", params=["x", "y"], locals=[], blocks={"entry": add_block}, entry="entry")
    add_ssa = MirToSSA().run(add)
    add_sig = FnSignature(name="add", param_type_ids=[int_ty, int_ty], return_type_id=int_ty)
    add_info = FnInfo(fn_id=add_id, name="add", declared_can_throw=False, signature=add_sig, return_type_id=int_ty)

    # main() -> Int { return add(1, 2) }
    main_block = BasicBlock(
        name="entry",
        instructions=[
            ConstInt(dest="t0", value=1),
            ConstInt(dest="t1", value=2),
            Call(dest="t2", fn_id=add_id, args=["t0", "t1"], can_throw=False),
        ],
        terminator=Return(value="t2"),
    )
    main_id = FunctionId(module="main", name="main", ordinal=0)
    main = MirFunc(fn_id=main_id, name="main", params=[], locals=["t0", "t1", "t2"], blocks={"entry": main_block}, entry="entry")
    main_ssa = MirToSSA().run(main)
    main_sig = FnSignature(name="main", param_type_ids=[], return_type_id=int_ty)
    main_info = FnInfo(fn_id=main_id, name="main", declared_can_throw=False, signature=main_sig, return_type_id=int_ty)

    word_bits = host_word_bits()
    word_ty = f"i{word_bits}"
    mod = lower_module_to_llvm(
        {add_id: add, main_id: main},
        {add_id: add_ssa, main_id: main_ssa},
        {add_id: add_info, main_id: main_info},
        type_table=table, word_bits=word_bits)
    ir = mod.render()

    assert f"define {word_ty} @add({word_ty} %x, {word_ty} %y)" in ir
    assert f"define {word_ty} @main()" in ir
    assert f"call {word_ty} @add({word_ty} %t0, {word_ty} %t1)" in ir


def test_mixed_int_string_params_and_return():
    table, int_ty, str_ty = _int_and_string_types()

    # combine(x: Int, s: String) -> String { return s }
    comb_block = BasicBlock(name="entry", instructions=[], terminator=Return(value="s"))
    comb_id = FunctionId(module="main", name="combine", ordinal=0)
    comb = MirFunc(fn_id=comb_id, name="combine", params=["x", "s"], locals=[], blocks={"entry": comb_block}, entry="entry")
    comb_ssa = MirToSSA().run(comb)
    comb_sig = FnSignature(name="combine", param_type_ids=[int_ty, str_ty], return_type_id=str_ty)
    comb_info = FnInfo(fn_id=comb_id, name="combine", declared_can_throw=False, signature=comb_sig, return_type_id=str_ty)

    # main() -> String { return combine(1, "abc") }
    main_block = BasicBlock(
        name="entry",
        instructions=[
            ConstInt(dest="t0", value=1),
            ConstString(dest="t1", value="abc"),
            Call(dest="t2", fn_id=comb_id, args=["t0", "t1"], can_throw=False),
        ],
        terminator=Return(value="t2"),
    )
    main_id = FunctionId(module="main", name="main", ordinal=0)
    main = MirFunc(fn_id=main_id, name="main", params=[], locals=["t0", "t1", "t2"], blocks={"entry": main_block}, entry="entry")
    main_ssa = MirToSSA().run(main)
    main_sig = FnSignature(name="main", param_type_ids=[], return_type_id=str_ty)
    main_info = FnInfo(fn_id=main_id, name="main", declared_can_throw=False, signature=main_sig, return_type_id=str_ty)

    word_bits = host_word_bits()
    word_ty = f"i{word_bits}"
    mod = lower_module_to_llvm(
        {comb_id: comb, main_id: main},
        {comb_id: comb_ssa, main_id: main_ssa},
        {comb_id: comb_info, main_id: main_info},
        type_table=table, word_bits=word_bits)
    ir = mod.render()

    assert f"define %DriftString @combine({word_ty} %x, %DriftString %s)" in ir
    assert "define %DriftString @main()" in ir
    assert f"call %DriftString @combine({word_ty} %t0, %DriftString %t1)" in ir


def test_fnptr_param_headers():
    table, int_ty, _ = _int_and_string_types()
    fnptr_nothrow = _fn_type(table, int_ty, can_throw=False)
    fnptr_throwing = _fn_type(table, int_ty, can_throw=True)

    # apply(f: Fn(Int) nothrow -> Int, x: Int) -> Int { return x }
    apply_block = BasicBlock(name="entry", instructions=[], terminator=Return(value="x"))
    apply_id = FunctionId(module="main", name="apply", ordinal=0)
    apply = MirFunc(fn_id=apply_id, name="apply", params=["f", "x"], locals=[], blocks={"entry": apply_block}, entry="entry")
    apply_ssa = MirToSSA().run(apply)
    apply_sig = FnSignature(name="apply", param_type_ids=[fnptr_nothrow, int_ty], return_type_id=int_ty)
    apply_info = FnInfo(fn_id=apply_id, name="apply", declared_can_throw=False, signature=apply_sig, return_type_id=int_ty)

    # apply_ct(f: Fn(Int) -> Int, x: Int) -> Int { return x }
    apply_ct_block = BasicBlock(name="entry", instructions=[], terminator=Return(value="x"))
    apply_ct_id = FunctionId(module="main", name="apply_ct", ordinal=0)
    apply_ct = MirFunc(fn_id=apply_ct_id, name="apply_ct", params=["f", "x"], locals=[], blocks={"entry": apply_ct_block}, entry="entry")
    apply_ct_ssa = MirToSSA().run(apply_ct)
    apply_ct_sig = FnSignature(name="apply_ct", param_type_ids=[fnptr_throwing, int_ty], return_type_id=int_ty)
    apply_ct_info = FnInfo(fn_id=apply_ct_id, name="apply_ct", declared_can_throw=False, signature=apply_ct_sig, return_type_id=int_ty)

    word_bits = host_word_bits()
    word_ty = f"i{word_bits}"
    mod = lower_module_to_llvm(
        {apply_id: apply, apply_ct_id: apply_ct},
        {apply_id: apply_ssa, apply_ct_id: apply_ct_ssa},
        {apply_id: apply_info, apply_ct_id: apply_ct_info},
        type_table=table, word_bits=word_bits)
    ir = mod.render()

    # ABI 20 (uniform fat): nothrow Fn params stay thin `ptr`; throwing Fn
    # params are the fat %DriftFatFnPtr {adapter, env} pair.
    assert f"define {word_ty} @apply(ptr %f, {word_ty} %x)" in ir
    assert f"define {word_ty} @apply_ct(%DriftFatFnPtr %f, {word_ty} %x)" in ir
