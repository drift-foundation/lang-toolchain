# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import pytest

from lang.driftc.stage0 import ast
from lang.driftc.stage1.macro_expander import expand_macro_call


def test_expand_log_info_macro_to_helper_call() -> None:
	expanded = expand_macro_call(
		ast.MacroCall(
			func=ast.Attr(ast.Name("log"), "info"),
			args=[ast.Name("lg"), ast.Literal("ev"), ast.MapLiteral(entries=[])],
			kwargs=[],
		)
	)
	assert isinstance(expanded, ast.Call)
	assert isinstance(expanded.func, ast.Attr)
	assert expanded.func.attr == "macro_info"
	assert len(expanded.args) == 4
	assert isinstance(expanded.args[3], ast.Call)
	assert isinstance(expanded.args[3].func, ast.Attr)
	assert isinstance(expanded.args[3].func.value, ast.Name)
	assert expanded.args[3].func.value.ident == "meta"
	assert expanded.args[3].func.attr == "caller"


def test_expand_unknown_macro_reports_error() -> None:
	with pytest.raises(ValueError) as err:
		expand_macro_call(
			ast.MacroCall(
				func=ast.Attr(ast.Name("log"), "warn"),
				args=[ast.Name("lg"), ast.Literal("ev"), ast.MapLiteral(entries=[])],
				kwargs=[],
			)
		)
	assert "unknown macro 'warn!'" in str(err.value)


def test_expand_macro_rejects_kwargs() -> None:
	with pytest.raises(ValueError) as err:
		expand_macro_call(
			ast.MacroCall(
				func=ast.Attr(ast.Name("log"), "info"),
				args=[ast.Name("lg"), ast.Literal("ev"), ast.MapLiteral(entries=[])],
				kwargs=[ast.KwArg(name="attrs", value=ast.MapLiteral(entries=[]))],
			)
		)
	assert "do not support keyword arguments" in str(err.value)
