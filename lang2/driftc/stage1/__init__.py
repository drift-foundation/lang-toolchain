# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage 1 package: sugar-free HIR and AST→HIR lowering.

Pipeline placement:
  stage0 (AST) → stage1 (HIR) → stage2 (MIR) → SSA → LLVM/obj

Public API:
  - HIR node classes (expressions, statements, operator enums)
  - AstToHIR lowering entry point
"""

from .hir_nodes import (
	HNode,
	HExpr,
	HStmt,
	BindingId,
	HVar,
	HLiteralInt,
	HLiteralString,
	HLiteralBool,
	HCall,
	HMethodCall,
	HTernary,
	HTryResult,
	HThrow,
	HTry,
	HCatchArm,
	HField,
	HIndex,
	HBorrow,
	HDVInit,
	HExceptionInit,
	HUnary,
	HBinary,
	HArrayLiteral,
	HResultOk,
	HBlock,
	HExprStmt,
	HLet,
	HAssign,
	HIf,
	HLoop,
	HBreak,
	HContinue,
	HReturn,
	UnaryOp,
	BinaryOp,
)
from .ast_to_hir import AstToHIR
from .normalize import normalize_hir

__all__ = [
	"HNode",
	"HExpr",
	"HStmt",
	"HVar",
	"HLiteralInt",
	"HLiteralString",
	"HLiteralBool",
	"HCall",
	"HMethodCall",
	"HTernary",
	"HTryResult",
	"HThrow",
	"HTry",
	"HCatchArm",
	"HField",
	"HIndex",
	"HBorrow",
	"HDVInit",
	"HExceptionInit",
	"HUnary",
	"HBinary",
	"HArrayLiteral",
	"HResultOk",
	"HBlock",
	"HExprStmt",
	"HLet",
	"HAssign",
	"HIf",
	"HLoop",
	"HBreak",
	"HContinue",
	"HReturn",
	"UnaryOp",
	"BinaryOp",
	"AstToHIR",
	"normalize_hir",
]
