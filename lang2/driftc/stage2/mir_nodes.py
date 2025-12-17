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
class ConstFloat(MInstr):
	"""
	dest = constant Float

	In lang2 v1, `Float` is IEEE-754 double precision and maps to LLVM `double`.
	This instruction carries the Python `float` value that should be emitted as a
	`double` constant in LLVM IR.
	"""
	dest: ValueId
	value: float


@dataclass
class ZeroValue(MInstr):
	"""
	dest = 0-value of a type (zero / null / zero-initialized aggregate).

	This instruction exists primarily to support `move <place>` semantics in a
	way that is:
	- ABI-safe (moved-from storage is reset to a known safe value), and
	- allocation-free (unlike constructing an empty `String` via runtime helpers).

	Codegen contract:
	- For scalars, this should be a cheap constant.
	- For aggregates, this should be constructed without calling into the runtime.
	"""
	dest: ValueId
	ty: TypeId


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
class StringFromFloat(MInstr):
	"""
	dest = String(value)

	Converts a `Float` (`double`) value to a decimal `String` using the runtime's
	canonical formatting.

	This is used by f-string interpolation. The runtime implementation is
	deterministic (Ryu-based) so codegen does not depend on libc `snprintf`.
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
	"""
	dest = &locals[local] (address-taking).

	`is_mut` records whether the borrow was `&mut` at the surface level. LLVM
	lowering uses the same pointer representation for `&T` and `&mut T` (both are
	`ptr` in v1), but the type system and borrow checker need to preserve the
	distinction for mutability rules.
	"""
	dest: ValueId
	local: LocalId
	is_mut: bool = False


@dataclass
class AddrOfArrayElem(MInstr):
	"""
	dest = &array[index] (address of an array element).

	This is the MIR primitive backing `&arr[i]` / `&mut arr[i]`.

	Lowering responsibility:
	- Codegen must perform bounds checks when computing the element address, so
	  subsequent `LoadRef` / `StoreRef` do not need to re-check bounds.
	- `inner_ty` identifies the element type for typed pointer computation in
	  LLVM IR (`T*`).
	"""
	dest: ValueId
	array: ValueId
	index: ValueId
	inner_ty: TypeId
	is_mut: bool = False


@dataclass
class LoadRef(MInstr):
	"""
	dest = *ptr (load through a reference).

	This is the MIR-level primitive for reading via `&T` / `&mut T`.

	We keep `inner_ty` as a TypeId so downstream stages can:
	  - compute the correct LLVM element type for the `load`, and
	  - validate that dereference is only used on reference-typed values.
	"""
	dest: ValueId
	ptr: ValueId
	inner_ty: TypeId


@dataclass
class StoreRef(MInstr):
	"""
	*ptr = value (store through a mutable reference).

	This is the MIR-level primitive for `*p = v` where `p: &mut T`.
	`inner_ty` is the element TypeId for LLVM lowering and basic validation.
	"""
	ptr: ValueId
	value: ValueId
	inner_ty: TypeId


@dataclass
class StoreLocal(MInstr):
	"""locals[local] = value"""
	local: LocalId
	value: ValueId


@dataclass
class ConstructStruct(MInstr):
	"""
	dest = StructName(field0, field1, ...)

	This instruction constructs a struct value by positional field order.

	Design notes:
	- `struct_ty` is the nominal TypeId of the struct. Field names and field
	  types are looked up in the shared `TypeTable` downstream.
	- This is a pure value constructor (no allocation); it maps naturally to
	  LLVM `insertvalue` chains into an `undef` aggregate.
	"""

	dest: ValueId
	struct_ty: TypeId
	args: List[ValueId]


@dataclass
class ConstructVariant(MInstr):
	"""
	dest = Ctor(args...) for a variant value.

	`variant_ty` is the concrete instantiated variant TypeId (e.g. Optional<Int>).
	`ctor` is the constructor name (e.g. "Some", "None").

	Design notes:
	- Variants are *compiler-private ABI* in MVP. Lowering/codegen treat the
	  shared `TypeTable`'s `VariantInstance` data as authoritative for:
	    - tag values (declaration order),
	    - field types and arity per constructor.
	- This instruction is pure value construction; it maps to building a struct
	  value in LLVM with tag + payload bytes.
	"""

	dest: ValueId
	variant_ty: TypeId
	ctor: str
	args: List[ValueId]


@dataclass
class VariantTag(MInstr):
	"""
	dest = tag(variant) as Uint (0..N-1).

	MIR exposes the tag as a `Uint` (i64) for simplicity; LLVM lowers the stored
	tag byte (i8) to i64 via zero-extension.
	"""

	dest: ValueId
	variant: ValueId
	variant_ty: TypeId


@dataclass
class VariantGetField(MInstr):
	"""
	dest = variant.<ctor>.<field_index>

	Extract the value of a constructor field from a variant payload.

	Contract:
	- The caller must ensure `variant` currently holds the constructor `ctor`
	  (typically by checking `VariantTag`).
	- `field_ty` is carried for downstream codegen typing.
	"""

	dest: ValueId
	variant: ValueId
	variant_ty: TypeId
	ctor: str
	field_index: int
	field_ty: TypeId


@dataclass
class StructGetField(MInstr):
	"""
	dest = subject.<field_index> (struct field read).

	We encode the field selection by index (not name) so MIR is independent of
	string-based name resolution once lowering has validated schemas.
	"""

	dest: ValueId
	subject: ValueId
	struct_ty: TypeId
	field_index: int
	field_ty: TypeId


@dataclass
class AddrOfField(MInstr):
	"""
	dest = &base_ptr.<field_index> (address of a struct field).

	This is the MIR primitive backing field borrows and field assignments via
	reference operations (`LoadRef` / `StoreRef`).

	Inputs:
	  - `base_ptr` must be a pointer to a struct value (`struct_ty*` in LLVM IR).
	  - `struct_ty` is the nominal TypeId of that struct.
	  - `field_index` selects the field by positional order.
	  - `field_ty` is the TypeId of the selected field (for typed pointer
	    computation downstream).

	`is_mut` records whether the originating borrow/assignment was mutable at the
	surface level; LLVM does not encode mutability, but the checker/borrow-checker
	do.
	"""

	dest: ValueId
	base_ptr: ValueId
	struct_ty: TypeId
	field_index: int
	field_ty: TypeId
	is_mut: bool = False


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
class StringCmp(MInstr):
	"""
	dest = string_cmp(left, right) (Int).

	This is a deterministic, locale-independent lexicographic comparison of the
	underlying UTF-8 byte sequences (unsigned byte ordering).

	Contract:
	  - dest < 0 if left < right
	  - dest == 0 if left == right
	  - dest > 0 if left > right
	"""

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
	"ConstFloat",
	"ZeroValue",
	"StringFromInt",
	"StringFromBool",
	"StringFromUint",
	"StringFromFloat",
	"StringLen",
	"StringEq",
	"StringCmp",
	"StringConcat",
	"LoadLocal",
	"AddrOfLocal",
	"AddrOfArrayElem",
	"LoadRef",
	"StoreRef",
	"StoreLocal",
	"ConstructStruct",
	"ConstructVariant",
	"VariantTag",
	"VariantGetField",
	"StructGetField",
	"AddrOfField",
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
