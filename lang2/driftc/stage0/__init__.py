# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage 0 package: raw AST and parser.

Pipeline placement:
  stage0 (AST) → stage1 (HIR) → stage2 (MIR) → SSA → LLVM/obj

Public API:
  - AST node classes used by later stages
  - parse_source helper (when added)
"""

from .ast import (
	Expr,
	Stmt,
	Name,
	Literal,
	Unary,
	Binary,
	Attr,
	Index,
	Call,
	ArrayLiteral,
	ExceptionCtor,
	Ternary,
	TryCatchExpr,
	LetStmt,
	AssignStmt,
	ExprStmt,
	ReturnStmt,
	BreakStmt,
	ContinueStmt,
	IfStmt,
	TryStmt,
	WhileStmt,
	ForStmt,
	ThrowStmt,
	RaiseStmt,
)

__all__ = [
	"Expr",
	"Stmt",
	"Name",
	"Literal",
	"Unary",
	"Binary",
	"Attr",
	"Index",
	"Call",
	"ArrayLiteral",
	"ExceptionCtor",
	"Ternary",
	"TryCatchExpr",
	"LetStmt",
	"AssignStmt",
	"ExprStmt",
	"ReturnStmt",
	"BreakStmt",
	"ContinueStmt",
	"IfStmt",
	"TryStmt",
	"WhileStmt",
	"ForStmt",
	"ThrowStmt",
	"RaiseStmt",
	# parse_source can be added here when implemented
]
