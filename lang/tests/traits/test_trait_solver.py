# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.parser import ast as parser_ast
from lang.driftc.parser import parser as p
from lang.driftc.traits.solver import Env, ProofStatus, prove_expr, prove_is
from lang.driftc.traits.world import TraitKey, TraitWorld, TypeKey, build_trait_world


def test_solver_proves_simple_impl() -> None:
	prog = p.parse_program(
		"""
trait Debug { fn fmt(self: Int) -> String }
struct File { }
implement Debug for File { fn fmt(self: File) -> String { return ""; } }
"""
	)
	world = build_trait_world(prog, diag_phase="test")
	env = Env(default_module="main")
	subst = {"Self": TypeKey(package_id=None, module="main", name="File", args=())}
	res = prove_is(world, env, subst, "Self", TraitKey(package_id=None, module="main", name="Debug"))
	assert res.status is ProofStatus.PROVED


def test_solver_refutes_missing_impl_for_concrete_type() -> None:
	prog = p.parse_program(
		"""
trait Debug { fn fmt(self: Int) -> String }
struct File { }
"""
	)
	world = build_trait_world(prog, diag_phase="test")
	env = Env(default_module="main")
	subst = {"Self": TypeKey(package_id=None, module="main", name="File", args=())}
	res = prove_is(world, env, subst, "Self", TraitKey(package_id=None, module="main", name="Debug"))
	assert res.status is ProofStatus.REFUTED


def test_solver_impl_require_blocks_proof_when_missing() -> None:
	prog = p.parse_program(
		"""
trait A { fn a(self: Int) -> Int }
trait B { fn b(self: Int) -> Int }
struct File { }
implement A for File require Self is B { fn a(self: File) -> Int { return 0; } }
"""
	)
	world = build_trait_world(prog, diag_phase="test")
	env = Env(default_module="main")
	subst = {"Self": TypeKey(package_id=None, module="main", name="File", args=())}
	res = prove_is(world, env, subst, "Self", TraitKey(package_id=None, module="main", name="A"))
	assert res.status is ProofStatus.REFUTED


def test_solver_not_is_unknown_without_subst() -> None:
	prog = p.parse_program(
		"""
trait A { fn a(self: Int) -> Int }
"""
	)
	world = build_trait_world(prog, diag_phase="test")
	env = Env(default_module="main")
	expr = parser_ast.TraitNot(
		loc=parser_ast.Located(line=1, column=1),
		expr=parser_ast.TraitIs(
			loc=parser_ast.Located(line=1, column=1),
			subject="T",
			trait=parser_ast.TypeExpr(name="A"),
		),
	)
	res = prove_expr(world, env, {}, expr)
	assert res.status is ProofStatus.UNKNOWN


def test_solver_not_is_proved_for_concrete_missing_impl() -> None:
	prog = p.parse_program(
		"""
trait A { fn a(self: Int) -> Int }
struct File { }
"""
	)
	world = build_trait_world(prog, diag_phase="test")
	env = Env(default_module="main")
	subst = {"Self": TypeKey(package_id=None, module="main", name="File", args=())}
	expr = parser_ast.TraitNot(
		loc=parser_ast.Located(line=1, column=1),
		expr=parser_ast.TraitIs(
			loc=parser_ast.Located(line=1, column=1),
			subject="Self",
			trait=parser_ast.TypeExpr(name="A"),
		),
	)
	res = prove_expr(world, env, subst, expr)
	assert res.status is ProofStatus.PROVED


def test_callback_fn_structural_match_args_validated() -> None:
	"""Callback1<A,R> satisfies Fn1<A,R> only when type args match.

	B1 correctness hardening: the structural shortcut must validate that
	the Callback's type args align with the Fn trait's type args. Without
	this check, Callback1<Int, Void> would incorrectly prove Fn1<String, Void>.
	"""
	world = TraitWorld()
	env = Env(default_module="main")
	_INT = TypeKey(package_id=None, module="std.core", name="Int")
	_VOID = TypeKey(package_id=None, module="std.core", name="Void")
	_STRING = TypeKey(package_id=None, module="std.core", name="String")
	_fn1 = TraitKey(package_id=None, module="std.core", name="Fn1")
	# Positive: Callback1<Int, Void> is Fn1<Int, Void> — args match
	cb_ok = TypeKey(package_id=None, module="std.core", name="Callback1", args=(_INT, _VOID))
	res_ok = prove_is(world, env, {"F": cb_ok}, "F", _fn1, trait_args=(_INT, _VOID))
	assert res_ok.status is ProofStatus.PROVED, f"Matching args must prove: {res_ok}"
	# Negative: Callback1<Int, Void> is Fn1<String, Void> — args mismatch
	res_bad = prove_is(world, env, {"F": cb_ok}, "F", _fn1, trait_args=(_STRING, _VOID))
	assert res_bad.status is not ProofStatus.PROVED, f"Mismatched args must not prove: {res_bad}"


def test_solver_assumptions_short_circuit() -> None:
	prog = p.parse_program(
		"""
trait A { fn a(self: Int) -> Int }
"""
	)
	world = build_trait_world(prog, diag_phase="test")
	subj = "T"
	trait_key = TraitKey(package_id=None, module="main", name="A")
	env = Env(default_module="main", assumed_true={(subj, trait_key)})
	res = prove_is(world, env, {}, subj, trait_key)
	assert res.status is ProofStatus.PROVED
	env_false = Env(default_module="main", assumed_false={(subj, trait_key)})
	res_false = prove_is(world, env_false, {}, subj, trait_key)
	assert res_false.status is ProofStatus.REFUTED
