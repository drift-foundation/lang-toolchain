#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""Resolver smoke tests for functions/methods."""

import pytest

from lang2.core.types_core import TypeTable
from lang2.driftc.method_registry import (
	CallableRegistry,
	CallableSignature,
	Visibility,
	SelfMode,
	CallableId,
	ModuleId,
)
from lang2.driftc.method_resolver import resolve_function_call, resolve_method_call, ResolutionError


def test_resolve_function_by_exact_types():
	table = TypeTable()
	int_ty = table.ensure_int()
	reg = CallableRegistry()
	reg.register_free_function(
		callable_id=1, name="foo", module_id=1, visibility=Visibility.public(), signature=CallableSignature((int_ty,), int_ty)
	)
	decl = resolve_function_call(reg, table, name="foo", arg_types=[int_ty], visible_modules=[1], current_module=1)
	assert decl.callable_id == 1


def test_function_resolution_ambiguous():
	table = TypeTable()
	int_ty = table.ensure_int()
	reg = CallableRegistry()
	reg.register_free_function(
		callable_id=1, name="foo", module_id=1, visibility=Visibility.public(), signature=CallableSignature((int_ty,), int_ty)
	)
	reg.register_free_function(
		callable_id=2, name="foo", module_id=1, visibility=Visibility.public(), signature=CallableSignature((int_ty,), int_ty)
	)
	try:
		resolve_function_call(reg, table, name="foo", arg_types=[int_ty], visible_modules=[1], current_module=1)
		assert False, "expected ambiguity"
	except ResolutionError:
		pass


def test_resolve_method_with_autoborrow_shared():
	table = TypeTable()
	int_ty = table.ensure_int()
	ref_int = table.ensure_ref(int_ty)
	reg = CallableRegistry()
	reg.register_inherent_method(
		callable_id=10,
		name="get",
		module_id=1,
		visibility=Visibility.public(),
		signature=CallableSignature((ref_int,), int_ty),
		impl_id=1,
		impl_target_type_id=int_ty,
		self_mode=SelfMode.SELF_BY_REF,
	)
	res = resolve_method_call(
		reg,
		table,
		receiver_type=int_ty,
		method_name="get",
		arg_types=[],
		visible_modules=[1],
		current_module=1,
	)
	assert res.decl.callable_id == 10
	assert res.receiver_autoborrow == SelfMode.SELF_BY_REF


def test_resolve_method_rejects_mut_from_shared_receiver():
	table = TypeTable()
	int_ty = table.ensure_int()
	ref_int = table.ensure_ref(int_ty)
	ref_mut_int = table.ensure_ref_mut(int_ty)
	reg = CallableRegistry()
	reg.register_inherent_method(
		callable_id=11,
		name="set",
		module_id=1,
		visibility=Visibility.public(),
		signature=CallableSignature((ref_mut_int, int_ty), ref_int),
		impl_id=1,
		impl_target_type_id=int_ty,
		self_mode=SelfMode.SELF_BY_REF_MUT,
	)
	try:
		resolve_method_call(
			reg,
			table,
			receiver_type=ref_int,  # shared ref, should not satisfy mut
			method_name="set",
			arg_types=[int_ty],
			visible_modules=[1],
			current_module=1,
		)
		assert False, "expected resolution failure"
	except ResolutionError:
		pass


def test_method_and_free_same_name_resolve_separately():
	"""
	A free function and a method with the same name should not collide:
	- method calls resolve via receiver type
	- free calls resolve via name only
	"""
	reg = CallableRegistry()
	table = TypeTable()
	int_ty = table.ensure_int()
	recv_ty = table.ensure_ref(int_ty)
	str_ty = table.ensure_string()

	# free foo(Int) -> String
	reg.register_free_function(
		callable_id=1, name="foo", module_id=0, visibility=Visibility.public(), signature=CallableSignature((int_ty,), str_ty)
	)
	# method foo(&Int) -> String on impl target Int
	reg.register_inherent_method(
		callable_id=2,
		name="foo",
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature((recv_ty,), str_ty),
		impl_id=2,
		impl_target_type_id=int_ty,
		self_mode=SelfMode.SELF_BY_REF,
	)

	# free call sees only the free function
	fn_decl = resolve_function_call(reg, table, name="foo", arg_types=[int_ty], visible_modules=[0])
	assert fn_decl.callable_id == 1

	# method call resolves to the method via receiver type
	method_res = resolve_method_call(
		reg,
		table,
		receiver_type=int_ty,
		method_name="foo",
		arg_types=[],
		visible_modules=[0],
	)
	assert method_res.decl.callable_id == 2


def test_methods_with_same_name_on_different_types_resolve_correctly():
	table = TypeTable()
	reg = CallableRegistry()
	point_ty = table.new_scalar("Point")
	circle_ty = table.new_scalar("Circle")
	reg.register_inherent_method(
		callable_id=20,
		name="move",
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature((point_ty,), point_ty),
		impl_id=20,
		impl_target_type_id=point_ty,
		self_mode=SelfMode.SELF_BY_VALUE,
	)
	reg.register_inherent_method(
		callable_id=21,
		name="move",
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature((circle_ty,), circle_ty),
		impl_id=21,
		impl_target_type_id=circle_ty,
		self_mode=SelfMode.SELF_BY_VALUE,
	)
	res_point = resolve_method_call(reg, table, receiver_type=point_ty, method_name="move", arg_types=[], visible_modules=[0])
	assert res_point.decl.callable_id == 20
	res_circle = resolve_method_call(reg, table, receiver_type=circle_ty, method_name="move", arg_types=[], visible_modules=[0])
	assert res_circle.decl.callable_id == 21


def test_by_value_receiver_does_not_auto_borrow():
	table = TypeTable()
	reg = CallableRegistry()
	foo_ty = table.new_scalar("Foo")
	foo_ref = table.ensure_ref(foo_ty)
	reg.register_inherent_method(
		callable_id=30,
		name="consume",
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature((foo_ty,), foo_ty),
		impl_id=30,
		impl_target_type_id=foo_ty,
		self_mode=SelfMode.SELF_BY_VALUE,
	)
	# Exact receiver type matches.
	res_val = resolve_method_call(reg, table, receiver_type=foo_ty, method_name="consume", arg_types=[], visible_modules=[0])
	assert res_val.decl.callable_id == 30
	# Reference receiver should not be auto-borrowed to a by-value self; expect ambiguity/failure.
	with pytest.raises(ResolutionError):
		resolve_method_call(reg, table, receiver_type=foo_ref, method_name="consume", arg_types=[], visible_modules=[0])
