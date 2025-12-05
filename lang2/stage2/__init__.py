# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage 2 package: MIR and HIR→MIR lowering.

Pipeline placement:
  stage0 (AST) → stage1 (HIR) → stage2 (MIR) → SSA → LLVM/obj

Public API:
  - MIR node classes (instructions, terminators, blocks, functions)
  - MirBuilder and HIRToMIR lowering entry point
"""

from .mir_nodes import (
	MNode,
	MInstr,
	MTerminator,
	ConstInt,
	ConstBool,
	ConstString,
	LoadLocal,
	StoreLocal,
	AddrOfLocal,
	LoadField,
	StoreField,
	LoadIndex,
	StoreIndex,
	Call,
	MethodCall,
	ConstructDV,
	ConstructError,
	ConstructResultOk,
	ConstructResultErr,
	ErrorEvent,
	UnaryOpInstr,
	BinaryOpInstr,
	AssignSSA,
	Phi,
	Goto,
	IfTerminator,
	Return,
	BasicBlock,
	MirFunc,
)
from .hir_to_mir import MirBuilder, HIRToMIR

__all__ = [
	"MNode",
	"MInstr",
	"MTerminator",
	"ConstInt",
	"ConstBool",
	"ConstString",
	"LoadLocal",
	"StoreLocal",
	"AddrOfLocal",
	"LoadField",
	"StoreField",
	"LoadIndex",
	"StoreIndex",
	"Call",
	"MethodCall",
	"ConstructDV",
	"ConstructError",
	"ConstructResultOk",
	"ConstructResultErr",
	"ErrorEvent",
	"UnaryOpInstr",
	"BinaryOpInstr",
	"AssignSSA",
	"Phi",
	"Goto",
	"IfTerminator",
	"Return",
	"BasicBlock",
	"MirFunc",
	"MirBuilder",
	"HIRToMIR",
]
