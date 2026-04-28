# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-27
"""
Typed call metadata shared between stage1 and stage2.

Stage2 must not re-resolve call targets or throw-ness; it consumes this data
verbatim from the type checker.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Tuple

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeId, TypeTable


SymbolId = FunctionId


class CallTargetKind(Enum):
	"""Call target classification for typed HIR."""
	DIRECT = auto()
	INDIRECT = auto()
	INTRINSIC = auto()
	CONSTRUCTOR = auto()
	TRAIT = auto()


class IntrinsicKind(Enum):
	"""Intrinsic operations emitted by the type checker."""
	SWAP = "swap"
	REPLACE = "replace"
	WRAPPING_ADD_U64 = "wrapping_add_u64"
	WRAPPING_MUL_U64 = "wrapping_mul_u64"
	BYTE_LENGTH = "byte_length"
	STRING_BYTE_AT = "string_byte_at"
	STRING_EQ = "string_eq"
	STRING_CONCAT = "string_concat"
	CALLBACK0 = "callback0"
	CALLBACK1 = "callback1"
	CALLBACK2 = "callback2"
	CALLBACK3 = "callback3"
	CALLBACK4 = "callback4"
	CALLBACK5 = "callback5"
	CALLBACK6 = "callback6"
	CALLBACK_THROW0 = "callback_throw0"
	CALLBACK_THROW1 = "callback_throw1"
	CALLBACK_THROW2 = "callback_throw2"
	CALLBACK_THROW3 = "callback_throw3"
	CALLBACK_THROW4 = "callback_throw4"
	CALLBACK_THROW5 = "callback_throw5"
	CALLBACK_THROW6 = "callback_throw6"
	TYPE_ID = "type_id"
	DROP_VALUE = "drop_value"
	RAW_ALLOC = "raw_alloc"
	RAW_DEALLOC = "raw_dealloc"
	RAWBUFFER_PTR = "rawbuffer_ptr"
	RAWBUFFER_CAP = "rawbuffer_cap"
	RAWBUFFER_FROM_PARTS = "rawbuffer_from_parts"
	RAWBUFFER_EMPTY = "rawbuffer_empty"
	RAW_PTR_AT_REF = "raw_ptr_at_ref"
	RAW_PTR_AT_MUT = "raw_ptr_at_mut"
	RAW_WRITE = "raw_write"
	RAW_READ = "raw_read"
	PTR_FROM_REF = "ptr_from_ref"
	PTR_FROM_REF_MUT = "ptr_from_ref_mut"
	PTR_OFFSET = "ptr_offset"
	PTR_READ = "ptr_read"
	PTR_WRITE = "ptr_write"
	PTR_IS_NULL = "ptr_is_null"
	PTR_AS_MUT_REF = "ptr_as_mut_ref"
	MAYBE_UNINIT = "maybe_uninit"
	MAYBE_WRITE = "maybe_write"
	MAYBE_ASSUME_INIT_REF = "maybe_assume_init_ref"
	MAYBE_ASSUME_INIT_MUT = "maybe_assume_init_mut"
	MAYBE_ASSUME_INIT_READ = "maybe_assume_init_read"
	# ---------------------------------------------------------------
	# Arc runtime boundary — centralized Arc ownership primitives
	# ---------------------------------------------------------------
	#
	# Drift's `std.concurrent.Arc<T>` is a compiler-known ownership
	# primitive: its payload and control-block management cannot be
	# expressed as ordinary Drift field access once `Arc<Interface>`
	# has a representation distinct from `Arc<Concrete>` (see the
	# representation-boundary comment block in types_core.py around
	# `is_arc_interface_view`).  The four intrinsics below are the
	# sole runtime seam for Arc — every Arc clone, borrow, drop, and
	# interface-view coercion lowers through one of them, with per-
	# intrinsic concrete-vs-interface dispatch handled in hir_to_mir
	# and llvm_codegen.
	#
	# Adding new Arc semantics (e.g. weak Arcs) should extend this
	# block rather than introducing scattered Arc-aware paths.
	ARC_CLONE = "arc_clone"
	ARC_GET = "arc_get"
	ARC_DESTROY = "arc_destroy"
	ARC_AS_INTERFACE = "arc_as_interface"



@dataclass(frozen=True)
class CallTarget:
	kind: CallTargetKind
	symbol: SymbolId | None = None
	callee_node_id: int | None = None
	intrinsic: IntrinsicKind | None = None
	variant_type_id: TypeId | None = None
	struct_type_id: TypeId | None = None
	ctor_name: str | None = None
	ctor_arg_field_indices: Tuple[int, ...] | None = None
	trait_key: object | None = None
	method_name: str | None = None

	@staticmethod
	def direct(symbol: SymbolId) -> "CallTarget":
		return CallTarget(kind=CallTargetKind.DIRECT, symbol=symbol)

	@staticmethod
	def indirect(callee_node_id: int) -> "CallTarget":
		return CallTarget(kind=CallTargetKind.INDIRECT, callee_node_id=callee_node_id)

	@staticmethod
	def intrinsic(kind: IntrinsicKind) -> "CallTarget":
		return CallTarget(kind=CallTargetKind.INTRINSIC, intrinsic=kind)

	@staticmethod
	def constructor(
		variant_type_id: TypeId,
		ctor_name: str,
		*,
		ctor_arg_field_indices: Tuple[int, ...] | None = None,
	) -> "CallTarget":
		return CallTarget(
			kind=CallTargetKind.CONSTRUCTOR,
			variant_type_id=variant_type_id,
			ctor_name=ctor_name,
			ctor_arg_field_indices=ctor_arg_field_indices,
		)

	@staticmethod
	def constructor_struct(
		struct_type_id: TypeId,
		*,
		ctor_arg_field_indices: Tuple[int, ...] | None = None,
	) -> "CallTarget":
		return CallTarget(
			kind=CallTargetKind.CONSTRUCTOR,
			struct_type_id=struct_type_id,
			ctor_arg_field_indices=ctor_arg_field_indices,
		)

	@staticmethod
	def trait(trait_key: object, method_name: str) -> "CallTarget":
		return CallTarget(kind=CallTargetKind.TRAIT, trait_key=trait_key, method_name=method_name)


@dataclass(frozen=True)
class CallSig:
	param_types: Tuple[TypeId, ...]
	user_ret_type: TypeId
	can_throw: bool
	includes_callee: bool = False
	declared_terminal_throws: bool = False


@dataclass(frozen=True)
class CallInfo:
	target: CallTarget
	sig: CallSig


def call_abi_ret_type(sig: CallSig, type_table: TypeTable) -> TypeId:
	"""
	Return the ABI return type for a call signature.

	Can-throw calls return FnResult<ok, Error>; nothrow calls return the user
	return type directly.
	"""
	if sig.can_throw:
		err = type_table.ensure_error()
		return type_table.ensure_fnresult(sig.user_ret_type, err)
	return sig.user_ret_type


__all__ = [
	"CallInfo",
	"CallSig",
	"CallTarget",
	"CallTargetKind",
	"IntrinsicKind",
	"SymbolId",
	"call_abi_ret_type",
]
