from lang2.driftc.parser import parser as p
from lang2.driftc.parser.ast import Call, QualifiedMember, ReturnStmt, LetStmt, TypeApp


def _get_return_expr(prog, fn_index: int):
	fn_def = prog.functions[fn_index]
	stmt = fn_def.body.statements[0]
	assert isinstance(stmt, ReturnStmt)
	assert stmt.value is not None
	return stmt.value


def test_parse_qualified_member_type_args_pre_and_post() -> None:
	prog = p.parse_program(
		"""
fn pre() returns Optional[Int] {
	return Optional < Int > :: None ( );
}

fn post() returns Optional[Int] {
	return Optional :: None < type Int > ( );
}
"""
	)
	pre_expr = _get_return_expr(prog, 0)
	post_expr = _get_return_expr(prog, 1)

	assert isinstance(pre_expr, Call)
	assert isinstance(pre_expr.func, QualifiedMember)
	assert pre_expr.func.base_type.name == "Optional"
	assert len(pre_expr.func.base_type.args) == 1
	assert pre_expr.func.base_type.args[0].name == "Int"
	assert pre_expr.func.member == "None"

	assert isinstance(post_expr, Call)
	assert isinstance(post_expr.func, QualifiedMember)
	assert post_expr.func.base_type.name == "Optional"
	assert post_expr.type_args is not None
	assert len(post_expr.type_args) == 1
	assert post_expr.type_args[0].name == "Int"
	assert post_expr.func.member == "None"


def test_parse_qualified_member_type_args_no_whitespace() -> None:
	prog = p.parse_program(
		"""
fn pre() returns Optional[Int] {
	return Optional<Int>::None();
}

fn post() returns Optional[Int] {
	return Optional::None<type Int>();
}
"""
	)
	pre_expr = _get_return_expr(prog, 0)
	post_expr = _get_return_expr(prog, 1)

	assert isinstance(pre_expr, Call)
	assert isinstance(pre_expr.func, QualifiedMember)
	assert pre_expr.func.base_type.name == "Optional"
	assert len(pre_expr.func.base_type.args) == 1
	assert pre_expr.func.base_type.args[0].name == "Int"
	assert pre_expr.func.member == "None"

	assert isinstance(post_expr, Call)
	assert isinstance(post_expr.func, QualifiedMember)
	assert post_expr.func.base_type.name == "Optional"
	assert post_expr.type_args is not None
	assert len(post_expr.type_args) == 1
	assert post_expr.type_args[0].name == "Int"
	assert post_expr.func.member == "None"


def test_parse_qualified_member_type_app_reference() -> None:
	prog = p.parse_program(
		"""
fn main() returns Int {
	val f = Optional::None<type Int>;
	return 0;
}
"""
	)
	stmt = prog.functions[0].body.statements[0]
	assert isinstance(stmt, LetStmt)
	assert isinstance(stmt.value, TypeApp)
	assert isinstance(stmt.value.func, QualifiedMember)
	assert stmt.value.func.base_type.name == "Optional"
	assert stmt.value.func.member == "None"
	assert stmt.value.type_args[0].name == "Int"
