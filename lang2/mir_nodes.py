# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Middle Intermediate Representation (MIR).

Pipeline placement:
  AST → HIR → MIR (this file) → SSA → LLVM/obj

This MIR sits between HIR (sugar-free AST) and SSA construction. It is explicit:
- No surface sugar.
- Explicit locals, loads/stores, calls, and control flow.
- No SSA yet; φ nodes are represented structurally and added during SSA.

Use this file as a reference for what MIR can express. There are **no semantics**
baked in here; it is just a typed tree of instructions/terminators/blocks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Dict, Optional, Union

from .hir_nodes import UnaryOp, BinaryOp


class MNode:
	"""Base class for MIR nodes (instructions and terminators)."""
	pass


class MInstr(MNode):
	"""Base class for MIR instructions (non-terminators)."""
	pass


class MTerminator(MNode):
	"""Base class for MIR terminators (end of a basic block)."""
	pass


# Locals and values

LocalId = str  # simple alias for now; can be made richer later
ValueId = str


# Instructions

@dataclass
class ConstInt(MInstr):
	"""dest = constant integer"""
	dest: ValueId
	value: int


@dataclass
class ConstBool(MInstr):
	"""dest = constant bool"""
	dest: ValueId
	value: bool


@dataclass
class ConstString(MInstr):
	"""dest = constant string (UTF-8 bytes as-is)."""
	dest: ValueId
	value: str


@dataclass
class LoadLocal(MInstr):
	"""dest = locals[local]"""
	dest: ValueId
	local: LocalId


@dataclass
class AddrOfLocal(MInstr):
	"""dest = &locals[local] (address-taking)"""
	dest: ValueId
	local: LocalId


@dataclass
class StoreLocal(MInstr):
	"""locals[local] = value"""
	local: LocalId
	value: ValueId


@dataclass
class LoadField(MInstr):
	"""dest = subject.field (struct field read)"""
	dest: ValueId
	subject: ValueId
	field: str


@dataclass
class StoreField(MInstr):
	"""subject.field = value (struct field write)"""
	subject: ValueId
	field: str
	value: ValueId


@dataclass
class LoadIndex(MInstr):
	"""dest = subject[index] (array/map-like read)"""
	dest: ValueId
	subject: ValueId
	index: ValueId


@dataclass
class StoreIndex(MInstr):
	"""subject[index] = value (array/map-like write)"""
	subject: ValueId
	index: ValueId
	value: ValueId


@dataclass
class Call(MInstr):
	"""dest = fn(args...) (plain function call; dest may be None for void)."""
	dest: Optional[ValueId]  # None for void calls
	fn: str
	args: List[ValueId]


@dataclass
class MethodCall(MInstr):
	"""dest = receiver.method(args...) with an explicit receiver arg."""
	dest: Optional[ValueId]  # None for void calls
	receiver: ValueId
	method_name: str
	args: List[ValueId]


@dataclass
class ConstructDV(MInstr):
	"""dest = DiagnosticValue constructor with typed args."""
	dest: ValueId
	dv_type_name: str
	args: List[ValueId]


@dataclass
class UnaryOpInstr(MInstr):
	"""dest = op operand (unary numeric/logical/bit ops)."""
	dest: ValueId
	op: UnaryOp
	operand: ValueId


@dataclass
class BinaryOpInstr(MInstr):
	"""dest = left op right (binary numeric/logical/bit ops)."""
	dest: ValueId
	op: BinaryOp
	left: ValueId
	right: ValueId


@dataclass
class Phi(MInstr):
	"""Phi node (added/used during SSA construction)."""
	dest: ValueId
	incoming: Dict[str, ValueId]  # block name -> value


# Terminators

@dataclass
class Goto(MTerminator):
	"""Unconditional branch to another basic block."""
	target: str


@dataclass
class IfTerminator(MTerminator):
	"""Conditional branch to then/else blocks."""
	cond: ValueId
	then_target: str
	else_target: str


@dataclass
class Return(MTerminator):
	"""Function return with optional value."""
	value: Optional[ValueId]


# Containers

@dataclass
class BasicBlock:
	"""
	Basic block: a list of instructions followed by a single terminator.

	No control flow leaves this block except via the terminator.
	"""
	name: str
	instructions: List[MInstr] = field(default_factory=list)
	terminator: Optional[MTerminator] = None


@dataclass
class MirFunc:
	"""
	MIR function: collection of blocks plus parameter/local declarations.

	Blocks are stored in a dict keyed by block name; `entry` names the entry block.
	"""
	name: str
	params: List[LocalId]
	locals: List[LocalId]
	blocks: Dict[str, BasicBlock] = field(default_factory=dict)
	entry: str = "entry"


__all__ = [
	"MNode", "MInstr", "MTerminator",
	"UnaryOp", "BinaryOp",
	"ConstInt", "ConstBool", "ConstString",
	"LoadLocal", "AddrOfLocal", "StoreLocal",
	"LoadField", "StoreField",
	"LoadIndex", "StoreIndex",
	"Call", "MethodCall",
	"ConstructDV",
	"UnaryOpInstr", "BinaryOpInstr",
	"Phi",
	"Goto", "IfTerminator", "Return",
	"BasicBlock", "MirFunc",
]
