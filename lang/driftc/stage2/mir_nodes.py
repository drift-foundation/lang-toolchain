# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Middle Intermediate Representation (MIR).

Pipeline placement:
  AST (lang/stage0/ast.py) → HIR (lang/stage1/hir_nodes.py) → MIR (this file) → SSA → LLVM/obj

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

from lang.driftc.core.function_id import FunctionId, function_symbol
from lang.driftc.core.types_core import TypeId
from lang.driftc.stage1 import UnaryOp, BinaryOp


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
class ConstUint(MInstr):
	"""dest = constant unsigned integer"""
	dest: ValueId
	value: int


@dataclass
class ConstUint64(MInstr):
	"""dest = constant unsigned 64-bit integer"""
	dest: ValueId
	value: int


@dataclass
class ConstByte(MInstr):
	"""dest = constant byte (u8)"""
	dest: ValueId
	value: int


@dataclass
class IntFromUint(MInstr):
	"""dest = cast Int from Uint (isize/usize conversion)."""
	dest: ValueId
	value: ValueId


@dataclass
class UintFromInt(MInstr):
	"""dest = cast Uint from Int (usize/isize conversion)."""
	dest: ValueId
	value: ValueId


@dataclass
class CastScalar(MInstr):
	"""dest = cast scalar value to another scalar type."""
	dest: ValueId
	value: ValueId
	src_ty: TypeId
	dst_ty: TypeId


@dataclass
class ConstBool(MInstr):
	"""dest = constant bool"""
	dest: ValueId
	value: bool


@dataclass
class ConstVoid(MInstr):
	"""dest = constant void value (placeholder)."""
	dest: ValueId


@dataclass
class ConstString(MInstr):
	"""dest = constant string (UTF-8 bytes as-is)."""
	dest: ValueId
	value: str


@dataclass
class ConstFloat(MInstr):
	"""
	dest = constant Float

	In lang v1, `Float` is IEEE-754 double precision and maps to LLVM `double`.
	This instruction carries the Python `float` value that should be emitted as a
	`double` constant in LLVM IR.
	"""
	dest: ValueId
	value: float


@dataclass
class FnPtrConst(MInstr):
	"""dest = function pointer constant."""
	dest: ValueId
	fn_ref: "FunctionRefId"
	call_sig: "CallSig"


@dataclass
class ConstructIface(MInstr):
	"""dest = interface value from a function pointer (callback)."""
	dest: ValueId
	iface_ty: TypeId
	fn_ref: "FunctionRefId"
	call_sig: "CallSig"
	data: ValueId | None = None
	data_ty: TypeId | None = None
	env_ty: TypeId | None = None


@dataclass
class ConstructIfaceValue(MInstr):
	"""dest = interface value by boxing a concrete value."""
	dest: ValueId
	iface_ty: TypeId
	value: ValueId
	value_ty: TypeId


@dataclass
class IfaceUpcast(MInstr):
	"""dest = interface value with vtable pointer retargeted to parent segment."""
	dest: ValueId
	iface: ValueId
	slot_offset: int


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
class StringRetain(MInstr):
	"""dest = retain(value) (String only)."""
	dest: ValueId
	value: ValueId


@dataclass
class StringRelease(MInstr):
	"""release(value) (String only)."""
	value: ValueId


@dataclass
class CopyValue(MInstr):
	"""dest = copy(value) (semantic copy for Copy types)."""
	dest: ValueId
	value: ValueId
	ty: TypeId


@dataclass
class DropValue(MInstr):
	"""drop(value) (semantic drop for destructible values)."""
	value: ValueId
	ty: TypeId


@dataclass
class MoveOut(MInstr):
	"""dest = move local (read local, then reset storage to zero)."""
	dest: ValueId
	local: LocalId
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
	- Variants are *compiler-private ABI* in v1. Lowering/codegen treat the
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

	MIR exposes the tag as a `Uint` (usize) for simplicity; LLVM lowers the stored
	tag byte (i8) to a word-sized integer via zero-extension.
	"""

	dest: ValueId
	variant: ValueId
	variant_ty: TypeId


@dataclass
class VariantTagRef(MInstr):
	"""
	dest = tag(*variant_ref) as Uint (0..N-1), without materializing a by-value copy.

	Use this when scrutinee is borrowed; it avoids ownership-changing loads of
	non-Copy variants.
	"""

	dest: ValueId
	variant_ref: ValueId
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
class VariantGetFieldAddr(MInstr):
	"""
	dest = &variant_ref.<ctor>.<field_index>

	Project the address of a constructor field from a *referenced* variant.

	Contract:
	- `variant_ref` must be a reference/pointer to `variant_ty`.
	- The caller must ensure the active constructor is `ctor` (usually via tag check).
	- `field_ty` is the pointee type of the produced reference.
	"""

	dest: ValueId
	variant_ref: ValueId
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
class ConstArray(MInstr):
	"""dest = const array from compile-time scalar data.
	Codegen emits a read-only LLVM global + DriftArrayHeader."""
	dest: ValueId
	elem_ty: TypeId
	values: list  # Python scalars (int/float/bool)


@dataclass
class ArrayLit(MInstr):
	"""dest = Array literal of the given element type."""
	dest: ValueId
	elem_ty: TypeId
	elements: List[ValueId]


@dataclass
class ArrayAlloc(MInstr):
	"""
	dest = allocate Array buffer with len=0/cap and uninitialized elements.

	`length` is reserved for future use and must be zero in v1; callers must set
	the final length via ArraySetLen after initializing elements.
	"""
	dest: ValueId
	elem_ty: TypeId
	length: ValueId
	cap: ValueId


@dataclass
class ArrayElemInit(MInstr):
	"""array[index] = value (initialize uninitialized slot)."""
	elem_ty: TypeId
	array: ValueId
	index: ValueId
	value: ValueId


@dataclass
class ArrayElemInitUnchecked(MInstr):
	"""array[index] = value (initialize slot without bounds checks)."""
	elem_ty: TypeId
	array: ValueId
	index: ValueId
	value: ValueId


@dataclass
class ArrayElemAssign(MInstr):
	"""array[index] = value (drop old element, then init new)."""
	elem_ty: TypeId
	array: ValueId
	index: ValueId
	value: ValueId


@dataclass
class ArrayElemDrop(MInstr):
	"""drop array[index] (destroy element in place)."""
	elem_ty: TypeId
	array: ValueId
	index: ValueId


@dataclass
class ArrayElemTake(MInstr):
	"""dest = take array[index] (move element out; slot becomes uninitialized)."""
	dest: ValueId
	elem_ty: TypeId
	array: ValueId
	index: ValueId


@dataclass
class ArrayDrop(MInstr):
	"""drop all elements and free array backing store."""
	elem_ty: TypeId
	array: ValueId


@dataclass
class ArrayDup(MInstr):
	"""dest = dup(array) with element-wise copy."""
	dest: ValueId
	elem_ty: TypeId
	array: ValueId


@dataclass
class ArrayIndexLoad(MInstr):
	"""dest = array[index] (typed array load)."""
	dest: ValueId
	elem_ty: TypeId
	array: ValueId
	index: ValueId


@dataclass
class ArrayIndexLoadUnchecked(MInstr):
	"""dest = array[index] (no bounds check)."""
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
class ArraySetLen(MInstr):
	"""dest = array with updated len field."""
	dest: ValueId
	array: ValueId
	length: ValueId


@dataclass
class ArraySetGen(MInstr):
	"""dest = array with updated gen field."""
	dest: ValueId
	array: ValueId
	gen: ValueId


@dataclass
class ArrayLen(MInstr):
	"""dest = len(array) as Int."""
	dest: ValueId
	array: ValueId


@dataclass
class ArrayCap(MInstr):
	"""dest = cap(array) as Int."""
	dest: ValueId
	array: ValueId


@dataclass
class ArrayGen(MInstr):
	"""dest = gen(array) as Int."""
	dest: ValueId
	array: ValueId


@dataclass
class RawBufferAlloc(MInstr):
	"""dest = allocate RawBuffer for element type with given capacity."""
	dest: ValueId
	raw_ty: TypeId
	elem_ty: TypeId
	cap: ValueId


@dataclass
class RawBufferDealloc(MInstr):
	"""deallocate RawBuffer."""
	buffer: ValueId
	raw_ty: TypeId


@dataclass
class RawBufferPtrAt(MInstr):
	"""dest = &mut elem at index in RawBuffer (no bounds check)."""
	dest: ValueId
	buffer: ValueId
	raw_ty: TypeId
	elem_ty: TypeId
	index: ValueId


@dataclass
class RawBufferWrite(MInstr):
	"""write value into RawBuffer slot (no bounds check)."""
	buffer: ValueId
	raw_ty: TypeId
	elem_ty: TypeId
	index: ValueId
	value: ValueId


@dataclass
class RawBufferRead(MInstr):
	"""read value from RawBuffer slot (moves out, slot becomes uninit)."""
	dest: ValueId
	buffer: ValueId
	raw_ty: TypeId
	elem_ty: TypeId
	index: ValueId


@dataclass
class PtrFromRef(MInstr):
	"""dest = raw pointer from &T or &mut T."""
	dest: ValueId
	src: ValueId
	ptr_ty: TypeId


@dataclass
class PtrOffset(MInstr):
	"""dest = ptr + offset (element offset)."""
	dest: ValueId
	ptr: ValueId
	ptr_ty: TypeId
	elem_ty: TypeId
	offset: ValueId


@dataclass
class PtrRead(MInstr):
	"""dest = *ptr (raw read, no drop)."""
	dest: ValueId
	ptr: ValueId
	elem_ty: TypeId


@dataclass
class PtrWrite(MInstr):
	"""*ptr = value (raw write)."""
	ptr: ValueId
	value: ValueId
	elem_ty: TypeId


@dataclass
class PtrIsNull(MInstr):
	"""dest = (ptr == null)."""
	dest: ValueId
	ptr: ValueId
	ptr_ty: TypeId


@dataclass
class StringLen(MInstr):
	"""dest = len(string) as Int."""
	dest: ValueId
	value: ValueId


@dataclass
class StringByteAt(MInstr):
	"""dest = string_byte_at(value, index) as Byte."""
	dest: ValueId
	value: ValueId
	index: ValueId


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
class AssertLoc(MInstr):
	cond: ValueId
	file: ValueId
	line: ValueId
	expr: ValueId
	msg: ValueId


@dataclass
class Call(MInstr):
	"""
	dest = fn(args...) (plain function call; dest may be None for void returns).
	"""
	dest: Optional[ValueId]  # None for void calls
	fn_id: FunctionId
	args: List[ValueId]
	can_throw: bool


@dataclass
class CallIndirect(MInstr):
	"""
	dest = callee(args...) via a function value (dest may be None for void returns).
	"""
	dest: Optional[ValueId]  # None for void calls
	callee: ValueId
	args: List[ValueId]
	param_types: List[TypeId]
	user_ret_type: TypeId
	can_throw: bool


@dataclass
class CallIface(MInstr):
	"""
	dest = iface.call(args...) via interface vtable (dest may be None for void returns).
	"""
	dest: Optional[ValueId]
	iface: ValueId
	args: List[ValueId]
	param_types: List[TypeId]
	user_ret_type: TypeId
	can_throw: bool
	slot_index: int


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
class ErrorAddLocalDV(MInstr):
	"""error.ctx_frames[frame].locals[key] = dv (append-style in runtime)."""

	error: ValueId
	frame: ValueId
	key: ValueId
	value: ValueId


@dataclass
class ErrorRaise(MInstr):
	"""Raise an error via drift_error_raise (noreturn). Used in nothrow context."""
	error: ValueId


@dataclass
class ConstructResultOk(MInstr):
	"""
	Construct FnResult.Ok(value).

	In the surface language, functions may be "can-throw" (exceptional control
	flow) while still declaring `-> T`. Internally, the compiler lowers
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
class ErrorCapturesGetDV(MInstr):
	"""
	dest = error.captures[frame][key] (DiagnosticValue lookup; missing yields DV_MISSING).
	"""

	dest: ValueId
	error: ValueId
	frame: ValueId
	key: ValueId


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
class DVAsFloat(MInstr):
	"""dest = drift_dv_as_float(dv) (returns Optional<Float>)."""

	dest: ValueId
	dv: ValueId


@dataclass
class DVAsString(MInstr):
	"""dest = drift_dv_as_string(dv) (returns Optional<String>)."""

	dest: ValueId
	dv: ValueId


@dataclass
class DVAsObject(MInstr):
	"""dest = drift_dv_as_object(dv) (returns Optional<DiagnosticValue>)."""

	dest: ValueId
	dv: ValueId


@dataclass
class DVGetField(MInstr):
	"""dest = drift_dv_get_field(dv, key) (returns Optional<DiagnosticValue>)."""

	dest: ValueId
	dv: ValueId
	key: ValueId

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
class WrappingAddU64(MInstr):
	"""dest = wrapping_add_u64(left, right)."""
	dest: ValueId
	left: ValueId
	right: ValueId


@dataclass
class WrappingMulU64(MInstr):
	"""dest = wrapping_mul_u64(left, right)."""
	dest: ValueId
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
	fn_id: FunctionId
	blocks: Dict[str, BasicBlock] = field(default_factory=dict)
	entry: str = "entry"
	local_types: Dict[str, TypeId] = field(default_factory=dict)
	debug_local_names: Dict[str, str] = field(default_factory=dict)

	def __post_init__(self) -> None:
		if self.name != function_symbol(self.fn_id):
			raise AssertionError(
				f"MirFunc name '{self.name}' must match fn_id symbol '{function_symbol(self.fn_id)}'"
			)


__all__ = [
	"MNode",
	"MInstr",
	"MTerminator",
	"UnaryOp",
	"BinaryOp",
	"ConstInt",
	"ConstUint",
	"ConstUint64",
	"ConstByte",
	"IntFromUint",
	"UintFromInt",
	"CastScalar",
	"ConstBool",
	"ConstVoid",
	"ConstString",
	"ConstFloat",
	"FnPtrConst",
	"ConstructIface",
	"ConstructIfaceValue",
	"IfaceUpcast",
	"ZeroValue",
	"StringRetain",
	"StringRelease",
	"CopyValue",
	"DropValue",
	"MoveOut",
	"StringFromInt",
	"StringFromBool",
	"StringFromUint",
	"StringFromFloat",
	"StringLen",
	"StringByteAt",
	"StringEq",
	"StringCmp",
	"StringConcat",
	"AssertLoc",
	"LoadLocal",
	"AddrOfLocal",
	"AddrOfArrayElem",
	"LoadRef",
	"StoreRef",
	"StoreLocal",
	"ConstructStruct",
	"ConstructVariant",
	"VariantTag",
	"VariantTagRef",
	"VariantGetField",
	"VariantGetFieldAddr",
	"StructGetField",
	"AddrOfField",
	"LoadField",
	"StoreField",
	"LoadIndex",
	"StoreIndex",
	"ConstArray",
	"ArrayLit",
	"ArrayAlloc",
	"ArrayElemInit",
	"ArrayElemInitUnchecked",
	"ArrayElemAssign",
	"ArrayElemDrop",
	"ArrayElemTake",
	"ArrayDrop",
	"ArrayDup",
	"ArrayIndexLoad",
	"ArrayIndexLoadUnchecked",
	"ArrayIndexStore",
	"ArraySetLen",
	"ArrayLen",
	"ArrayCap",
	"ArrayGen",
	"RawBufferAlloc",
	"RawBufferDealloc",
	"RawBufferPtrAt",
	"RawBufferWrite",
	"RawBufferRead",
	"PtrFromRef",
	"PtrOffset",
	"PtrRead",
	"PtrWrite",
	"PtrIsNull",
	"Call",
	"CallIndirect",
	"CallIface",
	"ConstructDV",
	"ConstructError",
	"ErrorAddAttrDV",
	"ErrorAddLocalDV",
	"ErrorRaise",
	"ConstructResultOk",
	"ConstructResultErr",
	"ResultIsErr",
	"ResultOk",
	"ResultErr",
	"ErrorAttrsGetDV",
	"ErrorCapturesGetDV",
	"DVAsInt",
	"DVAsBool",
	"DVAsFloat",
	"DVAsString",
	"DVAsObject",
	"DVGetField",
	"ErrorEvent",
	"UnaryOpInstr",
	"BinaryOpInstr",
	"WrappingAddU64",
	"WrappingMulU64",
	"Phi",
	"Goto",
	"IfTerminator",
	"Return",
	"Unreachable",
	"BasicBlock",
	"MirFunc",
]
