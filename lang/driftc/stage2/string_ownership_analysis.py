# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
String-ownership ANALYSIS library (R10 extraction,
string-arc-endgame-r10-extraction, 2026-07-20).

The shared, NON-EMITTING analyses behind the last-use-release
materialization pipeline (extracted VERBATIM from the legacy
`string_arc.py`, R10) so that both consumers — `string_releases`
(authoring) and `ownership_normalization` (R8 recognition/copy-through)
— use ONE neutral module with no dependency between them.  This module
must never import a consuming pass (fail-closed AST pin in
`lang/tests/stage2/test_string_ownership_analysis_extraction.py`).

Contents: `iter_used_values`, `seed_string_dest_types`,
`is_materialized_release_family_producer`, `build_fnwide_producers`,
`compute_lastuse_release_points`, `recognize_materialized_releases`,
`compute_string_temp_liveness`, `string_operand_dispositions`, the
`DISPOSITION_*` constants, `DRIFT_STRING_HELPER_SYMBOLS`, the private
`_analyze_lastuse_block` / `_is_semantic_string_tid` helpers, and (B2+C
S6) the `R8Recognition` frozen vessel + `compute_recognized_releases` —
the SINGLE plan-window recognition entry point that re-homes R8 off
the normalization pass's rewrite loop.
The per-operand dispositions CONTRACT prose lives with
`string_operand_dispositions` below (the former
`consumes_string_operand` thin wrapper was deleted with the R10 slice
— it had zero call sites; consumers wanting the per-operand answer
derive it from `string_operand_dispositions` directly).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Dict, Iterable, Mapping, Sequence, Set

from lang.driftc.checker import FnInfo
from lang.driftc.core.function_id import FunctionId, function_symbol
from lang.driftc.core.types_core import TypeId, TypeKind, TypeTable
from . import mir_nodes as M
from . import cfg as _cfg


def iter_used_values(instr: M.MInstr) -> Iterable[str]:
	"""Module-level single source for per-instruction String-relevant
	operand iteration (TLR-2a contract support).  Pure — no closure
	state; extracted verbatim from the legacy consumer's former
	_iter_used_values closure (which now aliases this)."""
	if isinstance(instr, M.StoreLocal):
		yield instr.value
	elif isinstance(instr, M.StoreRef):
		yield instr.ptr
		yield instr.value
	elif isinstance(instr, M.MoveFromRef):
		# MoveFromRef reads through `ptr`; the destination is a
		# named local (no SSA value yielded — the local is
		# tracked separately).
		yield instr.ptr
	elif isinstance(instr, M.ArrayIndexStore):
		yield instr.array
		yield instr.index
		yield instr.value
	elif isinstance(instr, M.ArrayLit):
		yield from instr.elements
	elif isinstance(instr, M.ArrayAlloc):
		yield instr.length
		yield instr.cap
	elif isinstance(instr, M.ArrayElemInit):
		yield instr.array
		yield instr.index
		yield instr.value
	elif isinstance(instr, M.ArrayElemInitUnchecked):
		yield instr.array
		yield instr.index
		yield instr.value
	elif isinstance(instr, M.ArrayElemAssign):
		yield instr.array
		yield instr.index
		yield instr.value
	elif isinstance(instr, M.ArrayElemDrop):
		yield instr.array
		yield instr.index
	elif isinstance(instr, M.ArrayElemTake):
		yield instr.array
		yield instr.index
	elif isinstance(instr, M.ArrayDrop):
		yield instr.array
	elif isinstance(instr, M.ArrayDup):
		yield instr.array
	elif isinstance(instr, (M.ArrayIndexLoad, M.ArrayIndexLoadUnchecked)):
		yield instr.array
		yield instr.index
	elif isinstance(instr, M.ArraySetLen):
		yield instr.array
		yield instr.length
	elif isinstance(instr, M.ConstructStruct):
		yield from instr.args
	elif isinstance(instr, M.ConstructVariant):
		yield from instr.args
	elif isinstance(instr, M.StructGetField):
		yield instr.subject
	elif isinstance(instr, M.VariantGetField):
		yield instr.variant
	elif isinstance(instr, M.ConstructIfaceValue):
		yield instr.value
	elif isinstance(instr, M.LoadLocal):
		# Do not yield instr.local — that is a storage-local name, not
		# an SSA value name.  Yielding it conflated the storage namespace
		# with the SSA-value namespace and required _is_local_name() to
		# filter it back out, which silently mis-classified SSA values
		# whose MIR temp counter happened to land on a name already in
		# use as a user storage local (e.g. user `val t4 = call_ret`).
		# Storage-local liveness is tracked separately via store_defs /
		# assigned_in / assigned_out and via _release_local at scope exit.
		pass
	elif isinstance(instr, M.LoadRef):
		yield instr.ptr
	elif isinstance(instr, M.Call):
		yield from instr.args
	elif isinstance(instr, M.CallIndirect):
		yield instr.callee
		yield from instr.args
	elif isinstance(instr, M.CallIface):
		yield instr.iface
		yield from instr.args
	elif isinstance(instr, M.IfaceUpcast):
		yield instr.iface
	elif isinstance(instr, M.StringConcat):
		yield instr.left
		yield instr.right
	elif isinstance(instr, M.StringEq):
		yield instr.left
		yield instr.right
	elif isinstance(instr, M.StringCmp):
		yield instr.left
		yield instr.right
	elif isinstance(instr, M.StringLen):
		yield instr.value
	elif isinstance(instr, M.StringByteAt):
		yield instr.value
	elif isinstance(instr, M.StringBytesBase):
		yield instr.value
	elif isinstance(instr, M.StringRetain):
		yield instr.value
	elif isinstance(instr, M.StringRelease):
		yield instr.value
	elif isinstance(instr, M.CopyValue):
		yield instr.value
	elif isinstance(instr, M.DropValue):
		yield instr.value
	elif isinstance(instr, M.UnaryOpInstr):
		yield instr.operand
	elif isinstance(instr, M.BinaryOpInstr):
		yield instr.left
		yield instr.right
	elif isinstance(instr, M.AssignSSA):
		yield instr.src
	elif isinstance(instr, M.Phi):
		for val in instr.incoming.values():
			yield val
	elif isinstance(instr, M.ConstructResultOk):
		if instr.value is not None:
			yield instr.value
	elif isinstance(instr, M.ConstructResultErr):
		yield instr.error
	elif isinstance(instr, M.ResultOk):
		yield instr.result
	elif isinstance(instr, M.ResultErr):
		yield instr.result
	elif isinstance(instr, M.ResultIsErr):
		yield instr.result
	elif isinstance(instr, M.ConstructError):
		yield instr.code
		yield instr.event_fqn
		if instr.payload is not None:
			yield instr.payload
		if instr.attr_key is not None:
			yield instr.attr_key
	elif isinstance(instr, M.ErrorRaise):
		yield instr.error
	elif isinstance(instr, M.ExcGetParamsJson):
		yield instr.error
	elif isinstance(instr, M.ExcSetParamsJson):
		yield instr.error
		yield instr.json_text
	elif isinstance(instr, M.ExcGetContextJson):
		yield instr.error
	elif isinstance(instr, M.ExcAppendContextFrame):
		yield instr.error
		yield instr.frame_json
	elif isinstance(instr, M.ErrorEvent):
		yield instr.error
	elif isinstance(instr, M.ErrorEventFqn):
		yield instr.error
	elif isinstance(instr, M.StringFromInt):
		yield instr.value
	elif isinstance(instr, M.StringFromUint):
		yield instr.value
	elif isinstance(instr, M.StringFromBool):
		yield instr.value
	elif isinstance(instr, M.StringFromFloat):
		yield instr.value
	elif isinstance(instr, M.MoveOut):
		# Do not yield instr.local — same reason as LoadLocal above.
		# It is a storage-local name, not an SSA value.  MoveOut has
		# its own rewrite path that handles the storage-local move
		# semantics; it does not need the name to flow through the
		# SSA-value liveness analyses.  Yielding it would re-open the
		# namespace-collision class for explicit `move local` shapes.
		pass




def seed_string_dest_types(
	blocks_in_order: "list[M.BasicBlock]",
	local_types: "Dict[str, TypeId]",
	*,
	fn_infos: Mapping[FunctionId, FnInfo],
	type_table: TypeTable,
) -> None:
	"""Shared dest-type seeding (TLR-2a review finding 1): pre-seed
	missing destination types from instruction shapes — the SAME logic
	normalize_ownership_mir runs internally (its `_seed_dest_types` delegates
	here), exported so the TLR-2b pass can seed a COPY of
	`func.local_types` before calling `compute_lastuse_release_points`
	instead of duplicating the rules or silently missing temps whose
	types HIR lowering did not record.  Mutates `local_types` in place."""
	string_ty = type_table.ensure_string()
	for block in blocks_in_order:
		for instr in block.instructions:
			dest = getattr(instr, "dest", None)
			if dest is None:
				continue
			if local_types.get(dest) is not None:
				continue
			if isinstance(instr, (M.ConstString, M.StringConcat, M.StringRetain, M.StringFromInt, M.StringFromBool, M.StringFromUint, M.StringFromFloat)):
				local_types[dest] = string_ty
				continue
			if isinstance(instr, M.MoveOut):
				# TLR-8: MoveOut carries its type on the instruction; the
				# family analysis must see the dest as String even when
				# upstream metadata omitted the temp (same completeness
				# contract as the ZeroValue/ArrayIndexLoad prescan seeds).
				local_types[dest] = instr.ty
				continue
			if isinstance(instr, M.AssignSSA):
				src_ty = local_types.get(instr.src)
				if src_ty is not None:
					local_types[dest] = src_ty
				continue
			if isinstance(instr, M.Phi):
				for incoming in instr.incoming.values():
					in_ty = local_types.get(incoming)
					if in_ty is not None:
						local_types[dest] = in_ty
						break
				continue
			if isinstance(instr, M.Call):
				info = fn_infos.get(instr.fn_id)
				if info is not None and info.signature is not None and info.signature.return_type_id is not None and not bool(getattr(info, "declared_can_throw", False)):
					local_types[dest] = info.signature.return_type_id
				else:
					sym = function_symbol(instr.fn_id)
					if sym in {
						"drift_string_from_cstr",
						"drift_string_from_utf8_bytes",
						"drift_string_from_int64",
						"drift_string_from_uint64",
						"drift_string_from_f64",
						"drift_string_from_bool",
						"drift_string_literal",
						"drift_string_concat",
						"drift_string_retain",
					}:
						local_types[dest] = string_ty
				continue
			if isinstance(instr, M.CallIndirect):
				if instr.user_ret_type is not None:
					local_types[dest] = instr.user_ret_type
				continue
			if isinstance(instr, M.CallIface):
				if instr.user_ret_type is not None:
					local_types[dest] = instr.user_ret_type
				continue


# ── TLR-2a shared contracts (TLR-2-DESIGN.md rev 2) ─────────────────────
#
# Contract 1: `string_operand_dispositions` — per-operand
# consuming/using/ignoring classification, mirroring the rewrite
# loop's arm dispatch (the former `consumes_string_operand` thin
# wrapper over this was deleted with the R10 slice, 2026-07-20 — it
# had zero call sites; the per-operand consume answer is
# `disp == DISPOSITION_CONSUME` over this function's output).
# Contract 2: `compute_lastuse_release_points` — the occurrence-level
# release-point calculator (multiplicity rule §3a included).
#
# Both are PURE functions over the input MIR.  Faithfulness note: the
# implementation mirrors the rewrite-loop arms via a three-way DISPOSITION
# per String operand — the review-approved two contracts under-modeled one
# axis discovered during implementation and reported in the TLR-2a report:
# some HANDLED arms neither consume nor note an operand at all
# (ref-position and non-String-param call args, info-less calls, Exc/ctor
# arms' non-selected operands).  Those are IGNORE: counted by the prescan
# but never drained, so the temp can never reach zero and is never
# released.  The calculator must reproduce that, or it would invent
# releases the consumer never re-emits.  Conformance is pinned empirically
# (calculator-vs-consumer agreement) in
# test_string_arc_audit_reporter.py.

DISPOSITION_CONSUME = "consume"
DISPOSITION_USE = "use"
DISPOSITION_IGNORE = "ignore"

# Runtime String-helper symbols whose call results are known owned
# Strings (+1 transfer) — the existing proof list `_is_string_creator`
# consults for move approvals, extracted to module level so the family
# predicate below defers to the SAME source.
DRIFT_STRING_HELPER_SYMBOLS = frozenset({
	"drift_string_from_cstr",
	"drift_string_from_utf8_bytes",
	"drift_string_from_int64",
	"drift_string_from_uint64",
	"drift_string_from_f64",
	"drift_string_from_bool",
	"drift_string_literal",
	"drift_string_concat",
	"drift_string_retain",
})


def _is_semantic_string_tid(type_table: TypeTable, tid: TypeId) -> bool:
	"""The SEMANTIC String test (TypeKind.SCALAR + name) — raw TypeId
	equality is unreliable across the package/type-table boundary (the
	string_stakes / finding-5 lesson)."""
	td = type_table.get(tid)
	return td.kind is TypeKind.SCALAR and td.name == "String"


def is_materialized_release_family_producer(
	prod: "M.MInstr | None",
	*,
	fn_infos: Mapping[FunctionId, FnInfo],
	type_table: TypeTable,
) -> bool:
	"""THE materialized-release producer family (TLR ladder): temps
	produced by these instructions (in ANY block since TLR-7 — producer
	resolution is fn-wide via `build_fnwide_producers`), with all-USE occurrences and no
	live-out/terminator use, get their last-use release emitted by the
	string_releases pass instead of the historical consumer's in-pass bookkeeping.
	SINGLE SOURCE (replaces the TLR-3 MATERIALIZED_RELEASE_FAMILY tuple):
	the release-point analysis / recognition (`_analyze_lastuse_block`)
	consumes this predicate for qualification AND shape rejection — the
	single production consumer.  (Historical consumers, both retired:
	the TLR-1 shim classification in `_note_use`, with the release-arm
	tripwire slice 2026-07-16; the tripwire's payload family flag, with
	the tripwire-deletion slice 2026-07-18.)  The dest
	String-typed-ness condition is the CALLER's (`_is_family_temp`).

	- Unconditional: ConstString (TLR-1/2b), StringConcat (TLR-3),
	  StringFrom{Int,Bool,Uint,Float} and ExcGetParamsJson/
	  ExcGetContextJson (TLR-5 — plain single-dest instructions with
	  scalar/error operands, +1-owned results: drift_string_from_* /
	  ABI §2.3 retained returns), CopyValue (TLR-6 — a String CopyValue
	  dest is an unconditional +1 owner, codegen drift_string_retain;
	  consumed `.stake` copies never qualify, measured zero at
	  last-use).
	- MoveOut (TLR-8): the dest inherits the storage local's +1 stake
	  verbatim — the expansion zero-stores the local, so the dest is the
	  SOLE holder and an unconditional owner.  First wild population
	  found by the release-arm tripwire (drift-workflows, 2026-07-17:
	  `"lit" + move s` — a moved String operand draining at a
	  non-consuming concat; issues/string-arc-release-arm-tripwire/).
	  The toolchain corpus had zero such sites, so the TLR measurement
	  never saw the class.  Consumed move dests (`return move x`,
	  by-value call args, stores) never qualify — a CONSUME disposition
	  disqualifies at the calculator, same as every other member.
	- Direct Call (TLR-4): NOT can_throw AND the result proven
	  semantically String — fn_infos signature return type (semantic
	  test, finding-5 rule) or a known drift_string_* helper symbol.
	  Info-less/unproven call results are conservatively OUT
	  (population 0 measured; pinned so a metadata regression cannot
	  silently widen the family).
	- CallIndirect/CallIface (TLR-4): NOT can_throw AND semantic-String
	  instruction-carried user_ret_type (population 0 measured; the
	  proof is instruction-local and exactly as strong).
	- can_throw admission is STRUCTURALLY impossible — a can-throw
	  call's dest is the FnResult envelope, never a String
	  (TLR-4-DESIGN.md §3) — and fail-closed here anyway.
	- TLR-7 closed the ladder for every MEASURED population: with
	  fn-wide producer resolution, all corpus temp_lastuse temps are
	  family-covered — the `_note_use` release arm went corpus-zero and
	  was fail-closed.  TLR-8 (MoveOut) is the first post-closure
	  member, admitted from a production tripwire firing rather than a
	  corpus measurement."""
	if isinstance(prod, (
		M.ConstString, M.StringConcat,                      # TLR-1..3
		M.StringFromInt, M.StringFromBool,                  # TLR-5
		M.StringFromUint, M.StringFromFloat,                # TLR-5
		M.ExcGetParamsJson, M.ExcGetContextJson,            # TLR-5
		M.CopyValue,                                        # TLR-6
		M.MoveOut,                                          # TLR-8
	)):
		return True
	if isinstance(prod, (M.Call, M.CallIndirect, M.CallIface)):
		if getattr(prod, "can_throw", False):
			return False
		if isinstance(prod, M.Call):
			sym = function_symbol(prod.fn_id)
			if isinstance(sym, str) and sym in DRIFT_STRING_HELPER_SYMBOLS:
				return True
			info = fn_infos.get(prod.fn_id)
			return (
				info is not None
				and info.signature is not None
				and info.signature.return_type_id is not None
				and _is_semantic_string_tid(type_table, info.signature.return_type_id)
			)
		urt = getattr(prod, "user_ret_type", None)
		return urt is not None and _is_semantic_string_tid(type_table, urt)
	return False


def string_operand_dispositions(
	instr: M.MInstr,
	*,
	local_types: Mapping[str, TypeId],
	fn_infos: Mapping[FunctionId, FnInfo],
	type_table: TypeTable,
) -> list[tuple[str, str]]:
	"""(operand value-id, disposition) for every STRING-TYPED SSA operand
	of `instr`, mirroring the historical consumer's rewrite-loop arms.  Assumes
	well-typed MIR (e.g. a String value cannot be stored into an
	array/destructible-typed local, so those early StoreLocal arms never
	intercept a String operand)."""
	string_ty = type_table.ensure_string()

	def _is_str_val(v: object) -> bool:
		return isinstance(v, str) and local_types.get(v) == string_ty

	def _is_str_tid(tid) -> bool:
		return tid == string_ty

	out: list[tuple[str, str]] = []
	if isinstance(instr, M.StoreLocal):
		if _is_str_tid(local_types.get(instr.local)) and _is_str_val(instr.value):
			out.append((instr.value, DISPOSITION_CONSUME))
		elif _is_str_val(instr.value):
			out.append((instr.value, DISPOSITION_USE))
		return out
	if isinstance(instr, M.MoveFromRef):
		if _is_str_tid(instr.inner_ty) and _is_str_val(instr.ptr):
			out.append((instr.ptr, DISPOSITION_CONSUME))
			return out
		# non-String MoveFromRef falls through to the generic note.
		if _is_str_val(instr.ptr):
			out.append((instr.ptr, DISPOSITION_USE))
		return out
	if isinstance(instr, M.StoreRef):
		if _is_str_tid(instr.inner_ty):
			if _is_str_val(instr.value):
				out.append((instr.value, DISPOSITION_CONSUME))
			if _is_str_val(instr.ptr):
				out.append((instr.ptr, DISPOSITION_IGNORE))
			return out
		for v in (instr.ptr, instr.value):
			if _is_str_val(v):
				out.append((v, DISPOSITION_USE))
		return out
	if isinstance(instr, M.ArrayIndexStore):
		if _is_str_tid(instr.elem_ty):
			if _is_str_val(instr.value):
				out.append((instr.value, DISPOSITION_CONSUME))
			for v in (instr.array, instr.index):
				if _is_str_val(v):
					out.append((v, DISPOSITION_IGNORE))
			return out
		for v in (instr.array, instr.index, instr.value):
			if _is_str_val(v):
				out.append((v, DISPOSITION_USE))
		return out
	if isinstance(instr, (M.ArrayElemInit, M.ArrayElemInitUnchecked, M.ArrayElemAssign)):
		if _is_str_tid(instr.elem_ty):
			if _is_str_val(instr.value):
				out.append((instr.value, DISPOSITION_CONSUME))
			for v in (getattr(instr, "array", None), getattr(instr, "index", None)):
				if _is_str_val(v):
					out.append((v, DISPOSITION_IGNORE))
			return out
		for v in (getattr(instr, "array", None), getattr(instr, "index", None), instr.value):
			if _is_str_val(v):
				out.append((v, DISPOSITION_USE))
		return out
	if isinstance(instr, M.ArrayLit):
		disp = DISPOSITION_CONSUME if _is_str_tid(instr.elem_ty) else DISPOSITION_USE
		for e in instr.elements:
			if _is_str_val(e):
				out.append((e, disp))
		return out
	if isinstance(instr, M.ConstructStruct):
		inst = type_table.get_struct_instance(instr.struct_ty)
		if inst is not None:
			for field_ty, arg in zip(inst.field_types, instr.args):
				if not _is_str_val(arg):
					continue
				out.append((arg, DISPOSITION_CONSUME if _is_str_tid(field_ty) else DISPOSITION_IGNORE))
			return out
		for arg in instr.args:
			if _is_str_val(arg):
				out.append((arg, DISPOSITION_USE))
		return out
	if isinstance(instr, M.ConstructVariant):
		inst = type_table.get_variant_instance(instr.variant_ty)
		if inst is not None and instr.ctor in inst.arms_by_name:
			arm = inst.arms_by_name[instr.ctor]
			for field_ty, arg in zip(arm.field_types, instr.args):
				if not _is_str_val(arg):
					continue
				out.append((arg, DISPOSITION_CONSUME if _is_str_tid(field_ty) else DISPOSITION_IGNORE))
			return out
		for arg in instr.args:
			if _is_str_val(arg):
				out.append((arg, DISPOSITION_USE))
		return out
	if isinstance(instr, M.ConstructIfaceValue):
		if _is_str_val(instr.value):
			out.append((instr.value, DISPOSITION_CONSUME if _is_str_tid(instr.value_ty) else DISPOSITION_IGNORE))
		return out
	if isinstance(instr, M.ConstructResultOk):
		if instr.value is not None and _is_str_val(instr.value):
			out.append((instr.value, DISPOSITION_CONSUME))
		return out
	if isinstance(instr, M.ConstructError):
		if _is_str_val(instr.event_fqn):
			out.append((instr.event_fqn, DISPOSITION_CONSUME))
		return out
	if isinstance(instr, M.ExcSetParamsJson):
		if _is_str_val(instr.json_text):
			out.append((instr.json_text, DISPOSITION_CONSUME))
		return out
	if isinstance(instr, M.ExcAppendContextFrame):
		if _is_str_val(instr.frame_json):
			out.append((instr.frame_json, DISPOSITION_CONSUME))
		return out
	if isinstance(instr, M.ErrorRaise):
		for v in iter_used_values(instr):
			if _is_str_val(v):
				out.append((v, DISPOSITION_IGNORE))
		return out
	# Call params use the SEMANTIC String test (SCALAR + name), mirroring
	# the rewrite arms' `_param_is_string` — NOT raw TypeId equality:
	# String param TypeIds are not canonical across the package/type-table
	# boundary (the string_stakes lesson), and a raw-equality mirror would
	# classify a semantically-String by-value arg as IGNORE while the live
	# arm consumes it.  (IGNORE still disqualifies the temp from
	# release-point output, so no phantom release today — the real risk
	# is CONTRACT DRIFT: `string_operand_dispositions` would lie
	# relative to the live arm, and future users of the predicate would
	# decide wrongly.)
	def _param_is_str_semantic(tid) -> bool:
		return _is_semantic_string_tid(type_table, tid)

	if isinstance(instr, M.Call):
		info = fn_infos.get(instr.fn_id)
		if info is not None and info.signature and info.signature.param_type_ids is not None:
			for ty_id, arg in zip(info.signature.param_type_ids, instr.args):
				if not _is_str_val(arg):
					continue
				if type_table.get(ty_id).kind is TypeKind.REF:
					out.append((arg, DISPOSITION_IGNORE))
				elif _param_is_str_semantic(ty_id):
					out.append((arg, DISPOSITION_CONSUME))
				else:
					out.append((arg, DISPOSITION_IGNORE))
			return out
		for arg in instr.args:
			if _is_str_val(arg):
				out.append((arg, DISPOSITION_IGNORE))
		return out
	if isinstance(instr, (M.CallIndirect, M.CallIface)):
		param_types = list(getattr(instr, "param_types", []) or [])
		for ty_id, arg in zip(param_types, instr.args):
			if not _is_str_val(arg):
				continue
			if type_table.get(ty_id).kind is TypeKind.REF:
				out.append((arg, DISPOSITION_IGNORE))
			elif _param_is_str_semantic(ty_id):
				out.append((arg, DISPOSITION_CONSUME))
			else:
				out.append((arg, DISPOSITION_IGNORE))
		callee = getattr(instr, "callee", None)
		if _is_str_val(callee):
			out.append((callee, DISPOSITION_IGNORE))
		return out
	if isinstance(instr, M.DropValue):
		if _is_str_tid(instr.ty):
			if _is_str_val(instr.value):
				out.append((instr.value, DISPOSITION_CONSUME))
			return out
		if _is_str_val(instr.value):
			out.append((instr.value, DISPOSITION_USE))
		return out
	# Generic fallthrough: every String operand is a non-consuming USE.
	for v in iter_used_values(instr):
		if _is_str_val(v):
			out.append((v, DISPOSITION_USE))
	return out



def build_fnwide_producers(
	blocks_in_order: "Sequence[M.BasicBlock]",
) -> Dict[str, M.MInstr]:
	"""TLR-7: the ONE producer-lookup authority shared by the
	materialization pass and the normalization pass's recognition — fn-wide, so
	family temps produced in one block and drained in another qualify.
	SSA single-assignment makes the map unique by construction; a
	duplicate dest fails closed (an upstream SSA-contract violation this
	late in the pipeline is never recoverable)."""
	producers: Dict[str, M.MInstr] = {}
	for block in blocks_in_order:
		for ins in block.instructions:
			dest = getattr(ins, "dest", None)
			if isinstance(dest, str):
				if dest in producers:
					raise AssertionError(
						f"ownership fn-wide producer tripwire "
						f"[duplicate SSA dest]: value '{dest}' defined by "
						f"{type(producers[dest]).__name__} and "
						f"{type(ins).__name__} — SSA single-assignment "
						f"violated upstream (TLR-7 producer contract)."
					)
				producers[dest] = ins
	return producers


def _analyze_lastuse_block(
	block: M.BasicBlock,
	*,
	local_types: Mapping[str, TypeId],
	fn_infos: Mapping[FunctionId, FnInfo],
	type_table: TypeTable,
	live_out_names: Set[str],
	producers_fnwide: "Mapping[str, M.MInstr] | None" = None,
) -> tuple[dict[str, int], Set[str]]:
	"""Shared core of contract 2 (`compute_lastuse_release_points`) and
	the TLR-2b recognition handshake (`recognize_materialized_releases`).
	Returns `(points, recognized_released)` — one analysis, two public
	projections, so the calculator and the recognizer cannot drift.

	Points: for each qualified family temp, the
	instruction index at which its occurrence count drains to zero — the
	position AFTER which exactly ONE release belongs (multiplicity rule:
	repeated operands in one instruction drain together and yield one
	release after that instruction; a terminator-drained temp maps to
	len(block.instructions)).

	Qualified: the temp's FN-WIDE UNIQUE producer satisfies
	`is_materialized_release_family_producer` (TLR-7: the producer may
	sit in ANY block; the release is placed in the DRAIN block);
	String-typed;
	not in `live_out_names`; ≥1 occurrence; every occurrence has USE
	disposition (a CONSUME disqualifies — the consumer contract de-owns and never
	releases; an IGNORE disqualifies — the count never drains, so
	re-releases); no Return-terminator use (consuming).

	Recognition rule (the TLR-2b prescan-exclusion contract): an
	in-contract pre-materialized `StringRelease(%t)` contributes NO
	occurrence to any count, and `%t` itself is excluded from the points
	(already released by the external author).  In-contract means BOTH:
	- shape: `%t`'s fn-wide unique producer satisfies the family
	  predicate; AND
	- placement (review-hardened): it is the UNIQUE StringRelease of
	  `%t` in the block, `%t`'s remaining occurrences are all USE, and
	  the release sits after the draining instruction those occurrences
	  compute, separated only by in-contract releases of temps draining
	  at the SAME instruction (same-group temps release consecutively;
	  never before a later use, never past a non-release instruction,
	  never for a live-out or Return-consumed temp; a NON-Return
	  terminator-drained temp is in contract when its release sits in
	  the trailing release run — the len(instructions) point).
	ANY input StringRelease that fails either half — including the shape
	half: the only legitimate author of pre-normalization releases is the
	string_releases pass, whose family is exactly the family
	predicate — is
	REJECTED fail-closed (AssertionError, `unexpected input release`
	tag).  A mis-placed release recognized silently would suppress
	a duplicate release while leaving a later use reading freed
	memory; an unknown-author release trusted silently would corrupt the
	occurrence counts."""
	string_ty = type_table.ensure_string()
	# TLR-7: producer resolution is FN-WIDE (the shared map built by
	# `build_fnwide_producers`).  The single-block fallback exists for
	# unit callers exercising one block — identical semantics there.
	producers: Mapping[str, M.MInstr]
	if producers_fnwide is not None:
		producers = producers_fnwide
	else:
		producers = build_fnwide_producers([block])

	def _is_family_temp(v: str) -> bool:
		return (
			is_materialized_release_family_producer(
				producers.get(v), fn_infos=fn_infos, type_table=type_table
			)
			and local_types.get(v) == string_ty
		)

	# Phase 1 — SHAPE recognition (needed before occurrence counting so
	# the exclusion below is possible); placement is validated in phase 3.
	# A shape-MISMATCHED input release (operand's fn-wide producer not
	# a family member) is rejected here: no pass other than the
	# string_releases materializer legitimately emits StringRelease
	# before ownership normalization, and its family is exactly the shared predicate.
	release_sites: dict[str, list[int]] = {}
	for idx, ins in enumerate(block.instructions):
		if isinstance(ins, M.StringRelease):
			if not _is_family_temp(ins.value):
				raise AssertionError(
					f"release-recognition tripwire "
					f"[unexpected input release]: block '{block.name}'[{idx}], "
					f"value '{ins.value}' — operand is not a family-producer "
					f"String temp (fn-wide producer resolution) "
					f"(producer={type(producers.get(ins.value)).__name__}). "
					f"Only the string_releases materialization pass may "
					f"emit StringRelease before ownership normalization."
				)
			release_sites.setdefault(ins.value, []).append(idx)
	recognized_released: Set[str] = set(release_sites)

	occurrences: dict[str, list[tuple[int, str]]] = {}
	for idx, ins in enumerate(block.instructions):
		if isinstance(ins, M.StringRelease) and ins.value in recognized_released:
			continue  # prescan-exclusion: contributes no occurrence
		for v, disp in string_operand_dispositions(
			ins, local_types=local_types, fn_infos=fn_infos, type_table=type_table
		):
			if _is_family_temp(v):
				occurrences.setdefault(v, []).append((idx, disp))

	term_used: Set[str] = set()
	term_consumed: Set[str] = set()
	if block.terminator is not None:
		is_return = isinstance(block.terminator, M.Return)
		for v in _cfg.terminator_value_uses(block.terminator):
			if _is_family_temp(v):
				(term_consumed if is_return else term_used).add(v)

	# Phase 3 — PLACEMENT validation of shape-recognized releases
	# (review-hardened): recognition by producer shape alone would let a
	# mis-placed release — e.g. one sitting BEFORE a later use — be
	# excluded from counting and suppress any duplicate in-consumer release,
	# turning an emission bug into a silent use-after-release.  Each
	# recognized release must be the unique release of its temp, the
	# temp's remaining occurrences must all be USE, and the release must
	# sit immediately after the draining instruction.  Anything else is
	# fail-closed (AssertionError → the driver's ownership_normalization boundary
	# wrap → clean `internal:` diagnostic).
	for temp, rel_idxs in release_sites.items():
		occs = occurrences.get(temp, [])
		drain = max((i for i, _d in occs), default=-1)
		# Placement: the release sits after the draining instruction,
		# separated ONLY by in-contract releases of temps draining at the
		# same instruction (same-group temps release CONSECUTIVELY — the
		# multiplicity/grouping reality the legacy in-consumer emission
		# produces; a gap containing ANY non-release instruction, e.g. a
		# later use or a later drain point, still rejects).
		# TERMINATOR-DRAINED temps (TLR-7, caught by the terminator-case
		# pin): a temp whose LAST use is a non-Return terminator operand
		# maps to point len(instructions) — its release sits in the
		# TRAILING release run (after every instruction occurrence, with
		# only in-contract releases from there to the end of the list),
		# exactly where the legacy terminator-note emission put
		# it.  A Return-consumed temp (term_consumed) still rejects.
		if temp in term_used:
			# The drain point is len(instructions), so the constraint is
			# that the release SITS IN the trailing release run (from the
			# release to the end of the list, only in-contract releases)
			# and after every instruction occurrence — instructions
			# between the last occurrence and the trailing run are fine
			# (they are unrelated to this temp; the terminator read is
			# the drain).
			placement_ok = (
				len(rel_idxs) == 1
				and rel_idxs[0] > drain
				and all(
					isinstance(block.instructions[j], M.StringRelease)
					and block.instructions[j].value in recognized_released
					for j in range(rel_idxs[0], len(block.instructions))
				)
			)
		else:
			placement_ok = (
				bool(occs)
				and len(rel_idxs) == 1
				and rel_idxs[0] > drain
				and all(
					isinstance(block.instructions[j], M.StringRelease)
					and block.instructions[j].value in recognized_released
					for j in range(drain + 1, rel_idxs[0])
				)
			)
		in_contract = (
			placement_ok
			and temp not in live_out_names
			and temp not in term_consumed
			and all(d == DISPOSITION_USE for _i, d in occs)
		)
		if not in_contract:
			raise AssertionError(
				f"release-recognition tripwire "
				f"[unexpected input release]: block '{block.name}', "
				f"value '{temp}', release at idx {rel_idxs}, "
				f"expected unique release immediately after draining "
				f"instruction idx {drain} "
				f"(occurrences={occurrences.get(temp)}, "
				f"live_out={temp in live_out_names}, "
				f"terminator_read={temp in term_used or temp in term_consumed}). "
				f"A pre-materialized StringRelease must match the computed "
				f"release point exactly (TLR-2 recognition contract)."
			)

	points: dict[str, int] = {}
	for temp, occs in occurrences.items():
		if temp in recognized_released or temp in live_out_names:
			continue
		if temp in term_consumed:
			continue
		if any(d != DISPOSITION_USE for _i, d in occs):
			continue
		if temp in term_used:
			points[temp] = len(block.instructions)
		else:
			points[temp] = max(i for i, _d in occs)
	# Terminator-only-used temps (no instruction occurrence).
	for temp in term_used:
		if (
			temp not in occurrences
			and temp not in recognized_released
			and temp not in live_out_names
			and temp not in term_consumed
		):
			points[temp] = len(block.instructions)
	return points, recognized_released


def compute_lastuse_release_points(
	block: M.BasicBlock,
	*,
	local_types: Mapping[str, TypeId],
	fn_infos: Mapping[FunctionId, FnInfo],
	type_table: TypeTable,
	live_out_names: Set[str],
	producers_fnwide: "Mapping[str, M.MInstr] | None" = None,
) -> dict[str, int]:
	"""Contract 2: the occurrence-level release-point calculator — see
	`_analyze_lastuse_block` for the full contract (points semantics,
	qualification, multiplicity rule, recognition/rejection of
	pre-materialized input releases)."""
	points, _recognized = _analyze_lastuse_block(
		block,
		local_types=local_types,
		fn_infos=fn_infos,
		type_table=type_table,
		live_out_names=live_out_names,
		producers_fnwide=producers_fnwide,
	)
	return points


def recognize_materialized_releases(
	block: M.BasicBlock,
	*,
	local_types: Mapping[str, TypeId],
	fn_infos: Mapping[FunctionId, FnInfo],
	type_table: TypeTable,
	live_out_names: Set[str],
	producers_fnwide: "Mapping[str, M.MInstr] | None" = None,
) -> Set[str]:
	"""TLR-2b handshake: the set of temps whose pre-materialized
	StringRelease is IN-CONTRACT (shape AND placement — see
	`_analyze_lastuse_block`), raising fail-closed on any out-of-contract
	input release.  the normalization pass's per-block prescan consults this BEFORE
	use counting: recognized releases contribute no occurrence, their
	temps never enter `owned_values` at the ConstString producer (a
	second release is impossible by construction), and the rewrite loop
	copies them through verbatim, noting `materialized_lastuse_release`
	at the recognition arm with NO `_note_use` (symmetric with the
	prescan exclusion — an uncounted decrement would skew move
	decisions)."""
	_points, recognized = _analyze_lastuse_block(
		block,
		local_types=local_types,
		fn_infos=fn_infos,
		type_table=type_table,
		live_out_names=live_out_names,
		producers_fnwide=producers_fnwide,
	)
	return recognized


def compute_string_temp_liveness(
	blocks_by_name: Mapping[str, M.BasicBlock],
	block_order: Sequence[str],
	*,
	local_types: Mapping[str, TypeId],
	string_ty: TypeId,
) -> Dict[str, Set[str]]:
	"""Shared per-block live-out sets of String-typed SSA temps —
	extracted verbatim from the legacy consumer's inline fixpoint (TLR-2b)
	so the materialization pass and the plan-window recognition compute
	liveness with ONE
	author.  In-contract pre-materialized releases cannot change the
	result (TLR-7 refinement of the argument — the conclusion is
	unchanged): every in-contract release site is DOMINATED BY A USE of
	the same temp within the drain block — the release sits after the
	drain, i.e. after the temp's last instruction occurrence there, or at
	end-of-instructions for a terminator-drained temp whose terminator
	use this walk also sees — so `block_use` already contains the temp
	before the release occurrence is reached, and defs are untouched.
	The pass (running on MIR without releases) and the recognition (running on
	MIR with them) see identical live-out sets by construction."""

	def _is_str_temp(v: object) -> bool:
		return isinstance(v, str) and local_types.get(v) == string_ty

	use: Dict[str, Set[str]] = {}
	defs: Dict[str, Set[str]] = {}
	for name in block_order:
		block = blocks_by_name[name]
		block_use: Set[str] = set()
		block_def: Set[str] = set()
		seen_def: Set[str] = set()
		for instr in block.instructions:
			for val in iter_used_values(instr):
				if not _is_str_temp(val):
					continue
				if val not in seen_def:
					block_use.add(val)
			dest = getattr(instr, "dest", None)
			if dest is not None and _is_str_temp(dest):
				block_def.add(dest)
				seen_def.add(dest)
		if block.terminator is not None:
			for val in _cfg.terminator_value_uses(block.terminator):
				if _is_str_temp(val) and val not in seen_def:
					block_use.add(val)
		use[name] = block_use
		defs[name] = block_def

	live_in: Dict[str, Set[str]] = {name: set() for name in block_order}
	live_out: Dict[str, Set[str]] = {name: set() for name in block_order}
	changed = True
	while changed:
		changed = False
		for name in block_order:
			block = blocks_by_name[name]
			out: Set[str] = set()
			for succ in _cfg.terminator_successors(block.terminator):
				out |= live_in.get(succ, set())
			new_in = use[name] | (out - defs[name])
			if new_in != live_in[name] or out != live_out[name]:
				live_in[name] = new_in
				live_out[name] = out
				changed = True
	return live_out


def classify_string_array_locals(
	func: "M.MirFunc",
	type_table: TypeTable,
) -> "tuple[TypeId, Set[str], Set[str]]":
	"""Shared single-source classifier for the STRING and ARRAY local
	sets used by the overwrite-cleanup family (Slice B1) and the
	normalization/plan passes.

	Returns `(string_ty, string_locals, array_locals)`.  Extracted
	VERBATIM from the legacy inline builds so every consumer (overwrite
	cleanup, normalization, the planner) classifies identically — a
	mismatch would leak
	(missed release/drop) or double-free (both passes emit).  The
	destructible / nullsafe / error apparatus is deliberately NOT here
	(Slice B2 owns it)."""
	string_ty = type_table.ensure_string()
	local_types = func.local_types
	string_locals: Set[str] = {
		name
		for name in (list(func.params) + list(func.locals))
		if local_types.get(name) == string_ty
	}
	array_locals: Set[str] = {
		name
		for name in (list(func.params) + list(func.locals))
		if (not name.startswith("__"))
		or name.startswith("__match_binder_")
		or name.startswith("__borrow_tmp")
		if (tid := local_types.get(name)) is not None
		and type_table.get(tid).kind is TypeKind.ARRAY
	}
	return string_ty, string_locals, array_locals


@dataclass(frozen=True)
class R8Recognition:
	"""Frozen per-function materialized-release RECOGNITION (R8, B2+C S6).

	Carries the per-block recognized-released temp set that the legacy
	string_arc used
	to OWN inline (`build_fnwide_producers` + `compute_string_temp_liveness`
	+ per-block `recognize_materialized_releases`).  Computed once at the
	pre-normalization planning window and CONSUMED by the normalization pass's rewrite
	loop (its R5/MoveOut/copy-through arm now reads these frozen values
	rather than recomputing recognition).  Driver-local (parallel to
	`_dplans`/`_dc1contrib`); never on the MIR, never inside the immutable
	`CleanupPlan`.

	`producers_fnwide`/`live_out` are deliberately NOT carried: they are
	pure inputs to `recognize_materialized_releases` with NO other consumer
	in the consumer, so freezing the recognition OUTPUT alone removes
	the normalization pass's recognition ownership.

	GENUINELY immutable (S6 closure): the mapping is validated and COPIED
	into a read-only `MappingProxyType` at construction, so no alias of the
	input dict can mutate the frozen vessel afterwards.  `for_block` is
	fail-closed: recognition covers EVERY block of its function (the wrapper
	records an explicit empty frozenset for release-free blocks), so a
	missing key is a contract violation, never "nothing recognized"."""
	fn_name: str
	recognized_by_block: "Mapping[str, frozenset]"

	def __post_init__(self) -> None:
		items = dict(self.recognized_by_block)
		for _bn, _vals in items.items():
			if not isinstance(_bn, str) or not isinstance(_vals, frozenset):
				raise AssertionError(
					f"R8Recognition[{self.fn_name}]: malformed entry "
					f"({_bn!r} -> {type(_vals).__name__}); every entry must be "
					f"str -> frozenset"
				)
			for _m in _vals:
				if not isinstance(_m, str):
					raise AssertionError(
						f"R8Recognition[{self.fn_name}]: block {_bn!r} carries a "
						f"non-string member {_m!r} ({type(_m).__name__}); "
						f"recognized temps are local NAMES"
					)
		object.__setattr__(self, "recognized_by_block", MappingProxyType(items))

	def for_block(self, block_name: str) -> "frozenset":
		try:
			return self.recognized_by_block[block_name]
		except KeyError:
			raise AssertionError(
				f"R8Recognition[{self.fn_name}]: no entry for block "
				f"{block_name!r} — a missing planned block must fail closed, "
				f"never default to empty recognition"
			) from None


def compute_recognized_releases(
	func: "M.MirFunc",
	*,
	type_table: TypeTable,
	fn_infos: "Mapping[FunctionId, FnInfo]",
) -> "R8Recognition":
	"""B2+C S6 — compute the per-block materialized-release recognition at
	the pre-mutation planning window over the ORIGINAL MIR (which already
	carries the StringReleases `materialize_lastuse_releases` emitted).

	Byte-identical to the legacy mid-rewrite recognition: NOTHING
	mutates the MIR between the plan window and normalization entry, and
	recognition reads ONLY pre-normalization operand types
	(`materialize_lastuse_releases` + `seed_string_dest_types`), never a
	type the rewrite adds mid-pass.  NON-MUTATING: reproduces the
	`_seed_dest_types` effect on a COPY of `func.local_types`,
	so `func.local_types` is untouched here.

	This is the SINGLE recognition entry point: the driver calls it at the
	plan window (freezing the result) and `normalize_ownership_mir` calls
	it as the
	bare-invocation fallback — so the three underlying analyses
	(`build_fnwide_producers` / `compute_string_temp_liveness` /
	`recognize_materialized_releases`) are invoked ONLY here, never in
	the consumer's own body.

	Fail-closed: an out-of-contract input release raises the same
	`AssertionError` the legacy consumer used to raise, now at the plan
	window."""
	block_order = sorted(func.blocks.keys())
	blocks = [func.blocks[b] for b in block_order]
	string_ty, _string_locals, _array_locals = classify_string_array_locals(func, type_table)
	# Reproduce `_seed_dest_types` on a COPY — deterministic over the SAME
	# original blocks, so the seeded copy equals `func.local_types` after
	# the consumer's own `_seed_dest_types` at recognition time.
	lt = dict(func.local_types)
	seed_string_dest_types(blocks, lt, fn_infos=fn_infos, type_table=type_table)
	producers_fnwide = build_fnwide_producers(blocks)
	live_out = compute_string_temp_liveness(
		func.blocks, block_order, local_types=lt, string_ty=string_ty,
	)
	recognized: "Dict[str, frozenset]" = {}
	for bname in block_order:
		block = func.blocks[bname]
		# SAME per-block gate the legacy consumer used: skip the analysis for blocks
		# without any input release (fast path; unit-test MIR with no
		# materialization pass, or post-pass blocks with no qualified temps).
		if any(isinstance(_i, M.StringRelease) for _i in block.instructions):
			recognized[bname] = frozenset(recognize_materialized_releases(
				block,
				local_types=lt,
				fn_infos=fn_infos,
				type_table=type_table,
				live_out_names=live_out.get(bname, set()),
				producers_fnwide=producers_fnwide,
			))
		else:
			recognized[bname] = frozenset()
	return R8Recognition(fn_name=func.name, recognized_by_block=recognized)
