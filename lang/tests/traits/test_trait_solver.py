# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.parser import ast as parser_ast
from lang.driftc.parser import parser as p
from lang.driftc.traits.solver import Env, ProofStatus, prove_expr, prove_is
from lang.driftc.traits.world import TraitKey, TypeKey, build_trait_world


def test_solver_proves_simple_impl() -> None:
	prog = p.parse_program(
		"""
trait Debug { fn fmt(self: Int) -> String }
struct File { }
implement Debug for File { fn fmt(self: File) -> String { return ""; } }
"""
	)
	world = build_trait_world(prog)
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
	world = build_trait_world(prog)
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
	world = build_trait_world(prog)
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
	world = build_trait_world(prog)
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
	world = build_trait_world(prog)
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


def test_solver_assumptions_short_circuit() -> None:
	prog = p.parse_program(
		"""
trait A { fn a(self: Int) -> Int }
"""
	)
	world = build_trait_world(prog)
	subj = "T"
	trait_key = TraitKey(package_id=None, module="main", name="A")
	env = Env(default_module="main", assumed_true={(subj, trait_key)})
	res = prove_is(world, env, {}, subj, trait_key)
	assert res.status is ProofStatus.PROVED
	env_false = Env(default_module="main", assumed_false={(subj, trait_key)})
	res_false = prove_is(world, env_false, {}, subj, trait_key)
	assert res_false.status is ProofStatus.REFUTED
