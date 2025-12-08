# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-10
"""
Headers and call lowering for typed parameters (Int, String).
"""

from lang2.checker import FnInfo, FnSignature
from lang2.core.types_core import TypeTable
from lang2.stage2 import BasicBlock, Call, ConstInt, ConstString, MirFunc, Return
from lang2.stage4.ssa import MirToSSA
from lang2.codegen.llvm import lower_module_to_llvm


def _int_and_string_types():
    table = TypeTable()
    int_ty = table.new_scalar("Int")
    str_ty = table.new_scalar("String")
    table._int_type = int_ty  # type: ignore[attr-defined]
    table._string_type = str_ty  # type: ignore[attr-defined]
    return table, int_ty, str_ty


def test_int_param_header_and_call():
    table, int_ty, _ = _int_and_string_types()

    # add(x: Int, y: Int) returns Int { return x }
    add_block = BasicBlock(name="entry", instructions=[], terminator=Return(value="x"))
    add = MirFunc(name="add", params=["x", "y"], locals=[], blocks={"entry": add_block}, entry="entry")
    add_ssa = MirToSSA().run(add)
    add_sig = FnSignature(name="add", param_type_ids=[int_ty, int_ty], return_type_id=int_ty)
    add_info = FnInfo(name="add", declared_can_throw=False, signature=add_sig, return_type_id=int_ty)

    # main() returns Int { return add(1, 2) }
    main_block = BasicBlock(
        name="entry",
        instructions=[ConstInt(dest="t0", value=1), ConstInt(dest="t1", value=2), Call(dest="t2", fn="add", args=["t0", "t1"])],
        terminator=Return(value="t2"),
    )
    main = MirFunc(name="main", params=[], locals=["t0", "t1", "t2"], blocks={"entry": main_block}, entry="entry")
    main_ssa = MirToSSA().run(main)
    main_sig = FnSignature(name="main", param_type_ids=[], return_type_id=int_ty)
    main_info = FnInfo(name="main", declared_can_throw=False, signature=main_sig, return_type_id=int_ty)

    mod = lower_module_to_llvm(
        {"add": add, "main": main},
        {"add": add_ssa, "main": main_ssa},
        {"add": add_info, "main": main_info},
        type_table=table,
    )
    ir = mod.render()

    assert "define i64 @add(i64 %x, i64 %y)" in ir
    assert "define i64 @main()" in ir
    assert "call i64 @add(i64 %t0, i64 %t1)" in ir


def test_mixed_int_string_params_and_return():
    table, int_ty, str_ty = _int_and_string_types()

    # combine(x: Int, s: String) returns String { return s }
    comb_block = BasicBlock(name="entry", instructions=[], terminator=Return(value="s"))
    comb = MirFunc(name="combine", params=["x", "s"], locals=[], blocks={"entry": comb_block}, entry="entry")
    comb_ssa = MirToSSA().run(comb)
    comb_sig = FnSignature(name="combine", param_type_ids=[int_ty, str_ty], return_type_id=str_ty)
    comb_info = FnInfo(name="combine", declared_can_throw=False, signature=comb_sig, return_type_id=str_ty)

    # main() returns String { return combine(1, "abc") }
    main_block = BasicBlock(
        name="entry",
        instructions=[
            ConstInt(dest="t0", value=1),
            ConstString(dest="t1", value="abc"),
            Call(dest="t2", fn="combine", args=["t0", "t1"]),
        ],
        terminator=Return(value="t2"),
    )
    main = MirFunc(name="main", params=[], locals=["t0", "t1", "t2"], blocks={"entry": main_block}, entry="entry")
    main_ssa = MirToSSA().run(main)
    main_sig = FnSignature(name="main", param_type_ids=[], return_type_id=str_ty)
    main_info = FnInfo(name="main", declared_can_throw=False, signature=main_sig, return_type_id=str_ty)

    mod = lower_module_to_llvm(
        {"combine": comb, "main": main},
        {"combine": comb_ssa, "main": main_ssa},
        {"combine": comb_info, "main": main_info},
        type_table=table,
    )
    ir = mod.render()

    assert "define %DriftString @combine(i64 %x, %DriftString %s)" in ir
    assert "define %DriftString @main()" in ir
    assert "call %DriftString @combine(i64 %t0, %DriftString %t1)" in ir
