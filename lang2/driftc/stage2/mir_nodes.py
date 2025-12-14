# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Middle Intermediate Representation (MIR).

Pipeline placement:
  AST (lang2/stage0/ast.py) → HIR (lang2/stage1/hir_nodes.py) → MIR (this file) → SSA → LLVM/obj

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

from lang2.driftc.core.types_core import TypeId
from lang2.driftc.stage1 import UnaryOp, BinaryOp


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
class StringFromInt(MInstr):
	"""
	dest = String(value)

	Converts an `Int` value to a `String` using the runtime's canonical formatting.
	This is used by f-string interpolation and other compiler-driven formatting.
	"""
	dest: ValueId
	value: ValueId


@dataclass
class StringFromBool(MInstr):
	"""
	dest = String(value)

	Converts a `Bool` value to a `String` (`"true"` / `"false"`).
	"""
	dest: ValueId
	value: ValueId


@dataclass
class StringFromUint(MInstr):
	"""
	dest = String(value)

	Converts a `Uint` value to a decimal `String`.
	"""
	dest: ValueId
	value: ValueId


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
class ArrayLit(MInstr):
	"""dest = Array literal of the given element type."""
	dest: ValueId
	elem_ty: TypeId
	elements: List[ValueId]


@dataclass
class ArrayIndexLoad(MInstr):
	"""dest = array[index] (typed array load)."""
	dest: ValueId
	elem_ty: TypeId
	array: ValueId
	index: ValueId


@dataclass
class ArrayIndexStore(MInstr):
	"""array[index] = value (typed array store)."""
	elem_ty: TypeId
	array: ValueId
	index: ValueId
	value: ValueId


@dataclass
class ArrayLen(MInstr):
	"""dest = len(array) in Size units."""
	dest: ValueId
	array: ValueId


@dataclass
class ArrayCap(MInstr):
	"""dest = cap(array) in Size units."""
	dest: ValueId
	array: ValueId


@dataclass
class StringLen(MInstr):
	"""dest = len(string) in Uint/size units."""
	dest: ValueId
	value: ValueId


@dataclass
class StringEq(MInstr):
	"""dest = (left == right) for strings; result is Bool."""
	dest: ValueId
	left: ValueId
	right: ValueId


@dataclass
class StringConcat(MInstr):
	"""dest = left + right for strings."""
	dest: ValueId
	left: ValueId
	right: ValueId


@dataclass
class Call(MInstr):
	"""
	dest = fn(args...) (plain function call; dest may be None for void returns).
	"""
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
class ConstructError(MInstr):
	"""
	Construct an Error value from an event code and diagnostic payload.

	`code` is the 64-bit event code (as per drift-abi-exceptions).
	`event_fqn` is the canonical FQN label (for logging/telemetry; not used for matching).
	`payload` is a DiagnosticValue representing structured attrs (optional).
	`attr_key` is the attr name under which to store the payload (optional).
	"""
	dest: ValueId
	code: ValueId
	event_fqn: ValueId
	payload: ValueId | None
	attr_key: ValueId | None


@dataclass
class ErrorAddAttrDV(MInstr):
	"""error.attrs[key] = dv (in-place)."""

	error: ValueId
	key: ValueId
	value: ValueId


@dataclass
class ConstructResultOk(MInstr):
	"""
	Construct FnResult.Ok(value).

	In the surface language, functions may be "can-throw" (exceptional control
	flow) while still declaring `returns T`. Internally, the compiler lowers
	can-throw functions to return `FnResult<T, Error>`.

	For `T = Void`, there is no surface value to carry. In that case `value`
	must be `None` and codegen will synthesize a dummy ok payload in the
	internal ABI slot.
	"""
	dest: ValueId
	value: ValueId | None


@dataclass
class ConstructResultErr(MInstr):
	"""Construct FnResult.Err(error)."""
	dest: ValueId
	error: ValueId


@dataclass
class ResultIsErr(MInstr):
	"""dest = result.is_err (Bool)."""

	dest: ValueId
	result: ValueId


@dataclass
class ResultOk(MInstr):
	"""dest = result.ok (undefined if result is Err)."""

	dest: ValueId
	result: ValueId


@dataclass
class ResultErr(MInstr):
	"""dest = result.err (Error handle; undefined if result is Ok)."""

	dest: ValueId
	result: ValueId


@dataclass
class ErrorAttrsGetDV(MInstr):
	"""
	dest = error.attrs[key] (DiagnosticValue lookup; missing yields DV_MISSING).
	"""

	dest: ValueId
	error: ValueId
	key: ValueId


@dataclass
class OptionalIsSome(MInstr):
	"""dest = opt.is_some (Bool)."""

	dest: ValueId
	opt: ValueId


@dataclass
class OptionalValue(MInstr):
	"""dest = opt.value (inner payload; undefined if not some)."""

	dest: ValueId
	opt: ValueId


@dataclass
class DVAsInt(MInstr):
	"""dest = drift_dv_as_int(dv) (returns Optional<Int>)."""

	dest: ValueId
	dv: ValueId


@dataclass
class DVAsBool(MInstr):
	"""dest = drift_dv_as_bool(dv) (returns Optional<Bool>)."""

	dest: ValueId
	dv: ValueId


@dataclass
class DVAsString(MInstr):
	"""dest = drift_dv_as_string(dv) (returns Optional<String>)."""

	dest: ValueId
	dv: ValueId

@dataclass
class ErrorEvent(MInstr):
	"""
	Project the event code from an Error value.

	The concrete layout is defined by the runtime ABI; this just captures the
	"extract the event code" operation so later passes (catch/dispatch) can use
	it without knowing the Error struct shape here.
	"""

	dest: ValueId
	error: ValueId


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
class AssignSSA(MInstr):
	"""
	SSA move/copy used during SSA construction.

	This is introduced by the SSA pass when rewriting LoadLocal/StoreLocal into
	pure SSA value flow. It carries explicit dest/src ValueIds.
	"""

	dest: ValueId
	src: ValueId


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


@dataclass
class Unreachable(MTerminator):
	"""
	Terminator for an unreachable control-flow path.

	This is used as a defensive invariant marker when earlier stages guarantee
	that a path cannot be taken (e.g., "uncaught error reaches a non-can-throw
	function"). Lowering should not crash the compiler in these cases; instead
	we encode the invariant into MIR and let LLVM emit `unreachable`.
	"""



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
	"MNode",
	"MInstr",
	"MTerminator",
	"UnaryOp",
	"BinaryOp",
	"ConstInt",
	"ConstBool",
	"ConstString",
	"StringFromInt",
	"StringFromBool",
	"StringFromUint",
	"LoadLocal",
	"AddrOfLocal",
	"StoreLocal",
	"LoadField",
	"StoreField",
	"LoadIndex",
	"StoreIndex",
	"ArrayLit",
	"ArrayIndexLoad",
	"ArrayIndexStore",
	"ArrayLen",
	"ArrayCap",
	"Call",
	"MethodCall",
	"ConstructDV",
	"ConstructError",
	"ErrorAddAttrDV",
	"ConstructResultOk",
	"ConstructResultErr",
	"ResultIsErr",
	"ResultOk",
	"ResultErr",
	"ErrorAttrsGetDV",
	"OptionalIsSome",
	"OptionalValue",
	"DVAsInt",
	"DVAsBool",
	"DVAsString",
	"ErrorEvent",
	"UnaryOpInstr",
	"BinaryOpInstr",
	"Phi",
	"Goto",
	"IfTerminator",
	"Return",
	"Unreachable",
	"BasicBlock",
	"MirFunc",
]
