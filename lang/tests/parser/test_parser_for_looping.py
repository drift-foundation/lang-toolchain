from lang.driftc.parser import ast as parser_ast
from lang.driftc.parser import parser as p
from lang.driftc.parser import parse_drift_to_hir


def _parse_with_diagnostics(src: str, tmp_path):
	path = tmp_path / "for_looping.drift"
	path.write_text(src)
	_module, _type_table, _exc_catalog, diagnostics = parse_drift_to_hir(path)
	return diagnostics


def test_parse_for_iter_colon_infer_binding() -> None:
	prog = p.parse_program(
		"""
fn main() -> Int {
	for val x : xs {
		x;
	}
	return 0;
}
"""
	)
	stmt = prog.functions[0].body.statements[0]
	assert isinstance(stmt, parser_ast.ForStmt)
	assert stmt.var == "x"
	assert stmt.var_mutable is False
	assert stmt.var_type_expr is None


def test_parse_for_iter_colon_typed_binding() -> None:
	prog = p.parse_program(
		"""
fn main() -> Int {
	for Int x : xs {
		x;
	}
	return 0;
}
"""
	)
	stmt = prog.functions[0].body.statements[0]
	assert isinstance(stmt, parser_ast.ForStmt)
	assert stmt.var == "x"
	assert stmt.var_mutable is False
	assert stmt.var_type_expr is not None
	assert stmt.var_type_expr.name == "Int"


def test_parse_for_count_infer_init() -> None:
	prog = p.parse_program(
		"""
fn main() -> Int {
	for var i = 0; i < n; i += 1 {
		i;
	}
	return 0;
}
"""
	)
	stmt = prog.functions[0].body.statements[0]
	assert isinstance(stmt, parser_ast.ForCountStmt)
	assert stmt.init_name == "i"
	assert stmt.init_mutable is True
	assert stmt.init_type_expr is None
	assert isinstance(stmt.step, parser_ast.AugAssignStmt)


def test_parse_for_count_typed_init() -> None:
	prog = p.parse_program(
		"""
fn main() -> Int {
	for Int i = 0; i < n; i += 1 {
		i;
	}
	return 0;
}
"""
	)
	stmt = prog.functions[0].body.statements[0]
	assert isinstance(stmt, parser_ast.ForCountStmt)
	assert stmt.init_name == "i"
	assert stmt.init_mutable is False
	assert stmt.init_type_expr is not None
	assert stmt.init_type_expr.name == "Int"


def test_parse_for_count_missing_step_reports_diagnostic(tmp_path) -> None:
	diagnostics = _parse_with_diagnostics(
		"""
fn main() -> Int {
	for var i = 0; i < 3; {
		i;
	}
	return 0;
}
""",
		tmp_path,
	)
	assert diagnostics


def test_parse_for_count_missing_condition_reports_diagnostic(tmp_path) -> None:
	diagnostics = _parse_with_diagnostics(
		"""
fn main() -> Int {
	for var i = 0; ; i += 1 {
		i;
	}
	return 0;
}
""",
		tmp_path,
	)
	assert diagnostics


def test_parse_for_count_missing_init_reports_diagnostic(tmp_path) -> None:
	diagnostics = _parse_with_diagnostics(
		"""
fn main() -> Int {
	for ; i < 3; i += 1 {
		i;
	}
	return 0;
}
""",
		tmp_path,
	)
	assert diagnostics


def test_parse_for_count_missing_semicolon_between_cond_and_step_reports_diagnostic(tmp_path) -> None:
	diagnostics = _parse_with_diagnostics(
		"""
fn main() -> Int {
	for var i = 0; i < 3 i += 1 {
		i;
	}
	return 0;
}
""",
		tmp_path,
	)
	assert diagnostics


def test_parse_for_iter_colon_missing_binding_reports_diagnostic(tmp_path) -> None:
	diagnostics = _parse_with_diagnostics(
		"""
fn main() -> Int {
	for : xs {
		0;
	}
	return 0;
}
""",
		tmp_path,
	)
	assert diagnostics
