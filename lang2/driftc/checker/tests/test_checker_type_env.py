from __future__ import annotations

import pytest

from lang2.driftc.core.types_core import TypeTable, TypeKind
from lang2.driftc.checker.type_env_impl import CheckerTypeEnv


def test_checker_type_env_handles_fnresult_parts():
	table = TypeTable()
	int_ty = table.new_scalar("Int")
	err_ty = table.new_error("Error")
	fnres_ty = table.new_fnresult(int_ty, err_ty)
	value_types = {("f", "v0"): fnres_ty}

	env = CheckerTypeEnv(table, value_types)

	assert env.is_fnresult(fnres_ty)
	ok, err = env.fnresult_parts(fnres_ty)
	assert ok == int_ty
	assert err == err_ty


def test_checker_type_env_rejects_non_fnresult_parts():
	table = TypeTable()
	int_ty = table.new_scalar("Int")
	env = CheckerTypeEnv(table, {("f", "v0"): int_ty})

	assert not env.is_fnresult(int_ty)
	with pytest.raises(TypeError):
		env.fnresult_parts(int_ty)
