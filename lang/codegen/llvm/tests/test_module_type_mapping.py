# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: LlvmModuleBuilder.llvm_type_for_typeid provides module-level
type mapping for standalone wrapper LLVM emission.

This is the 4A-prereq regression. The module-level method covers the
type kinds needed for wrapper param/return types (scalars, refs, ptrs,
arrays, structs, errors, FnResult, MaybeUninit). It is NOT a full
equivalent of _FuncBuilder._llvm_type_for_typeid — known gaps include
simplified forward nominal canonicalization and approximate variant layout.
"""
from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.codegen.llvm.llvm_codegen import LlvmModuleBuilder


def _make_module() -> LlvmModuleBuilder:
	return LlvmModuleBuilder(word_bits=64)


def test_scalar_types() -> None:
	"""Module-level type mapping handles all scalar types.

	Note: returns raw type strings (e.g. "drift.int"), not emit-form
	(e.g. "i64").  Use mod._llty() to get the emit form.
	"""
	table = TypeTable()
	mod = _make_module()

	# Raw form — callers use mod._llty() for emit form
	assert mod._llty(mod.llvm_type_for_typeid(table.ensure_int(), table)) == "i64"
	assert mod.llvm_type_for_typeid(table.ensure_bool(), table) == "i1"
	assert mod.llvm_type_for_typeid(table.ensure_string(), table) == "%DriftString"
	assert mod.llvm_type_for_typeid(table.ensure_byte(), table) == "i8"
	assert mod._llty(mod.llvm_type_for_typeid(table.ensure_uint(), table)) == "i64"
	assert mod.llvm_type_for_typeid(table.ensure_float(), table) == "double"


def test_ref_and_ptr_types() -> None:
	"""Module-level type mapping handles ref and ptr types."""
	table = TypeTable()
	mod = _make_module()
	int_tid = table.ensure_int()

	ref_tid = table.ensure_ref(int_tid)
	assert mod.llvm_type_for_typeid(ref_tid, table) == "ptr"

	ptr_tid = table.new_ptr(int_tid, module_id="std.mem")
	assert mod.llvm_type_for_typeid(ptr_tid, table) == "ptr"


def test_array_type() -> None:
	"""Module-level type mapping handles Array types."""
	table = TypeTable()
	mod = _make_module()
	int_tid = table.ensure_int()

	arr_tid = table.new_array(int_tid)
	assert mod.llvm_type_for_typeid(arr_tid, table) == "%DriftArrayHeader"


def test_struct_type() -> None:
	"""Module-level type mapping handles struct types."""
	table = TypeTable()
	mod = _make_module()
	int_tid = table.ensure_int()

	point_tid = table.declare_struct(module_id="mymod", name="Point", field_names=["x", "y"])
	table.define_struct_fields(point_tid, field_types=[int_tid, int_tid])

	llty = mod.llvm_type_for_typeid(point_tid, table)
	assert llty.startswith("%Struct_"), f"expected struct type, got {llty}"


def test_error_type() -> None:
	"""Module-level type mapping handles Error type."""
	table = TypeTable()
	mod = _make_module()
	err_tid = table.ensure_error()
	assert mod.llvm_type_for_typeid(err_tid, table) == "ptr"


def test_maybe_uninit_unwrapping() -> None:
	"""Module-level type mapping unwraps MaybeUninit<T> to T."""
	table = TypeTable()
	mod = _make_module()
	int_tid = table.ensure_int()

	mu_tid = table.declare_struct(module_id="std.mem", name="MaybeUninit", field_names=["value"], type_params=["T"])
	inst_tid = table.ensure_struct_instantiated(mu_tid, [int_tid])

	llty = mod.llvm_type_for_typeid(inst_tid, table)
	# MaybeUninit<Int> should unwrap to Int's type
	assert mod._llty(llty) == "i64", f"MaybeUninit<Int> should unwrap to i64, got {mod._llty(llty)}"


def test_fnresult_type() -> None:
	"""Module-level type mapping handles FnResult types."""
	table = TypeTable()
	mod = _make_module()
	int_tid = table.ensure_int()
	err_tid = table.ensure_error()
	fnres_tid = table.ensure_fnresult(int_tid, err_tid)

	llty = mod.llvm_type_for_typeid(fnres_tid, table)
	assert "FnResult" in llty, f"expected FnResult type, got {llty}"
