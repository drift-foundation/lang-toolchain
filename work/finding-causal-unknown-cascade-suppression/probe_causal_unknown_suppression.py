#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Expected-red probes for function-global Unknown cascade suppression.

These are reviewer evidence, not in-tree regression tests. Both tests should
fail before a causal fix because the second, independent diagnostic is globally
suppressed after the first error.
"""

from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.type_checker import TypeChecker


_UNKNOWN_BINDING = 41
_UNKNOWN_NAME = "independent_unknown"


def _check(statements: list[H.HStmt]):
	table = TypeTable()
	unknown = table.ensure_unknown()
	result = TypeChecker(table).check_function(
		FunctionId(module="probe", name="f", ordinal=0),
		H.HBlock(statements=statements),
		preseed_binding_types={_UNKNOWN_BINDING: unknown},
		preseed_binding_names={_UNKNOWN_BINDING: _UNKNOWN_NAME},
		preseed_scope_env={_UNKNOWN_NAME: unknown},
		preseed_scope_bindings={_UNKNOWN_NAME: _UNKNOWN_BINDING},
	)
	return result.diagnostics


def _unrelated_invalid_copy() -> H.HExprStmt:
	# This is a stable, unrelated first diagnostic: explicit copy requires an
	# addressable place. It neither reads nor writes _UNKNOWN_BINDING.
	return H.HExprStmt(expr=H.HCopy(subject=H.HLiteralInt(1)))


def test_unrelated_error_does_not_suppress_independent_copy_unknown():
	diagnostics = _check(
		[
			_unrelated_invalid_copy(),
			H.HLet(
				name="sink",
				value=H.HVar(_UNKNOWN_NAME, binding_id=_UNKNOWN_BINDING),
				declared_type_expr=None,
			),
		]
	)
	assert any(d.code == "E-COPY-UNKNOWN" for d in diagnostics), [d.message for d in diagnostics]


def test_unrelated_error_does_not_suppress_independent_unknown_callee():
	diagnostics = _check(
		[
			_unrelated_invalid_copy(),
			H.HExprStmt(
				expr=H.HCall(
					fn=H.HVar(_UNKNOWN_NAME, binding_id=_UNKNOWN_BINDING),
					args=[],
				)
			),
		]
	)
	assert sum(d.message == "call target is not a function value" for d in diagnostics) == 1, [d.message for d in diagnostics]
