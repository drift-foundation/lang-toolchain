# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
String ARC insertion for MIR.

This pass inserts explicit StringRetain/StringRelease (and CopyValue) ops so
LLVM codegen does not need to guess ownership. It also expands MoveOut into
LoadLocal + ZeroValue + StoreLocal once retains/releases are inserted.
"""

from __future__ import annotations

from typing import Dict, Iterable, Mapping, Sequence, Set

from lang.driftc.checker import FnInfo
from lang.driftc.core.types_core import TypeId, TypeKind, TypeTable
from lang.driftc.core.function_id import function_symbol
from lang.driftc.core.function_id import FunctionId
from lang.driftc import debug as drift_debug
from . import mir_nodes as M
from . import cfg as _cfg
from . import ownership_ledger_events as _ledger_events
from . import ownership_ledger_reporter as _ledger_reporter
from .ledger_cache import mark_ledger_dirty, maybe_fresh_ledger
from .ownership_ledger import DropVerdict as _DropVerdict
from .drop_policy_compute import compute_drop_policy as _compute_drop_policy
from .drop_flags import is_flag_managed as _is_flag_managed


def variant_zero_tag_drop_safe(local_ty: TypeId, type_table: TypeTable) -> bool:
	"""Phase 4 site-3 sub-step 3 — explicit policy axis.

	True iff `local_ty` is a variant type whose tag-0 destructor is a
	no-op.  Today: all variants — variant codegen lays out tag=0 as
	the default (PHI-zero) state, and the runtime's variant
	destructor dispatches on tag, so a tag=0 storage is a no-op
	drop.  Future: if variant layout changes (e.g. non-zero default
	tag, or per-variant destructor protocols), this predicate
	tightens here in one place.

	Used by site 3 to drive the conditionally-initialized variant
	widening: when the ledger reports `PathDependent` for such a
	local at a Return terminator, site 3 includes it in
	`initialized_at_return` so the drop fires.  The drop is safe
	(no-op on the uninit path) and necessary (live paths leak
	otherwise).  Replaces the dataflow-based widening that lived
	inline at the Return-terminator branch in 0.27.145–0.31.9.
	"""
	td = type_table.get(local_ty)
	return td.kind is TypeKind.VARIANT


def iter_used_values(instr: M.MInstr) -> Iterable[str]:
	"""Module-level single source for per-instruction String-relevant
	operand iteration (TLR-2a contract support).  Pure — no closure
	state; extracted verbatim from insert_string_arc's former
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
	insert_string_arc runs internally (its `_seed_dest_types` delegates
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
# Contract 1: `consumes_string_operand` — per-operand consuming/non-
# consuming classification, mirroring the rewrite loop's arm dispatch.
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
# releases string_arc never emits.  Conformance is pinned empirically
# (calculator-vs-insert_string_arc agreement) in
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
	"""THE materialized-release producer family (TLR ladder): block-local
	temps produced by these instructions, with all-USE occurrences and no
	live-out/terminator use, get their last-use release emitted by the
	string_releases pass instead of string_arc's in-pass bookkeeping.
	SINGLE SOURCE (replaces the TLR-3 MATERIALIZED_RELEASE_FAMILY tuple):
	the release-point analysis / recognition (`_analyze_lastuse_block`)
	and the TLR shim classification in `_note_use` both consume this
	predicate — they cannot disagree by construction.  The dest
	String-typed-ness condition is the CALLER's (`_is_family_temp`).

	- Unconditional: ConstString (TLR-1/2b), StringConcat (TLR-3).
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
	- CopyValue / StringFrom* / Exc* / cross-block tails stay OUT until
	  their own design gates."""
	if isinstance(prod, (M.ConstString, M.StringConcat)):
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
	of `instr`, mirroring insert_string_arc's rewrite-loop arms.  Assumes
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
	# is CONTRACT DRIFT: `consumes_string_operand` would lie relative to
	# the live arm, and future users of the predicate would decide
	# wrongly.)
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


def consumes_string_operand(
	instr: M.MInstr,
	operand: str,
	*,
	local_types: Mapping[str, TypeId],
	fn_infos: Mapping[FunctionId, FnInfo],
	type_table: TypeTable,
) -> bool:
	"""Contract 1: True iff ANY occurrence of `operand` in `instr` is
	consuming per the rewrite-loop arm dispatch."""
	return any(
		v == operand and d == DISPOSITION_CONSUME
		for v, d in string_operand_dispositions(
			instr, local_types=local_types, fn_infos=fn_infos, type_table=type_table
		)
	)


def _analyze_lastuse_block(
	block: M.BasicBlock,
	*,
	local_types: Mapping[str, TypeId],
	fn_infos: Mapping[FunctionId, FnInfo],
	type_table: TypeTable,
	live_out_names: Set[str],
) -> tuple[dict[str, int], Set[str]]:
	"""Shared core of contract 2 (`compute_lastuse_release_points`) and
	the TLR-2b recognition handshake (`recognize_materialized_releases`).
	Returns `(points, recognized_released)` — one analysis, two public
	projections, so the calculator and the recognizer cannot drift.

	Points: for each qualified block-local family temp, the
	instruction index at which its occurrence count drains to zero — the
	position AFTER which exactly ONE release belongs (multiplicity rule:
	repeated operands in one instruction drain together and yield one
	release after that instruction; a terminator-drained temp maps to
	len(block.instructions)).

	Qualified: producer satisfies
	`is_materialized_release_family_producer` (ConstString / StringConcat /
	proven non-throw String-returning calls since TLR-4) in THIS block;
	String-typed;
	not in `live_out_names`; ≥1 occurrence; every occurrence has USE
	disposition (a CONSUME disqualifies — string_arc de-owns and never
	releases; an IGNORE disqualifies — the count never drains, so
	string_arc never releases); no Return-terminator use (consuming).

	Recognition rule (the TLR-2b prescan-exclusion contract): an
	in-contract pre-materialized `StringRelease(%t)` contributes NO
	occurrence to any count, and `%t` itself is excluded from the points
	(already released by the external author).  In-contract means BOTH:
	- shape: `%t`'s producer is block-local and satisfies the family
	  predicate; AND
	- placement (review-hardened): it is the UNIQUE StringRelease of
	  `%t` in the block, `%t`'s remaining occurrences are all USE, and
	  the release sits after the draining instruction those occurrences
	  compute, separated only by in-contract releases of temps draining
	  at the SAME instruction (same-group temps release consecutively;
	  never before a later use, never past a non-release instruction,
	  never for a live-out or terminator-read temp).
	ANY input StringRelease that fails either half — including the shape
	half: the only legitimate author of pre-string_arc releases is the
	string_releases pass, whose family is exactly the family
	predicate — is
	REJECTED fail-closed (AssertionError, `unexpected input release`
	tag).  A mis-placed release recognized silently would suppress
	string_arc's own release while leaving a later use reading freed
	memory; an unknown-author release trusted silently would corrupt the
	occurrence counts."""
	string_ty = type_table.ensure_string()
	producers: dict[str, M.MInstr] = {}
	for ins in block.instructions:
		dest = getattr(ins, "dest", None)
		if isinstance(dest, str):
			producers[dest] = ins

	def _is_family_temp(v: str) -> bool:
		return (
			is_materialized_release_family_producer(
				producers.get(v), fn_infos=fn_infos, type_table=type_table
			)
			and local_types.get(v) == string_ty
		)

	# Phase 1 — SHAPE recognition (needed before occurrence counting so
	# the exclusion below is possible); placement is validated in phase 3.
	# A shape-MISMATCHED input release (operand not a block-local
	# MATERIALIZED_RELEASE_FAMILY temp) is rejected here: no pass other
	# than the string_releases materializer legitimately emits
	# StringRelease before string_arc, and its family is exactly this
	# constant.
	release_sites: dict[str, list[int]] = {}
	for idx, ins in enumerate(block.instructions):
		if isinstance(ins, M.StringRelease):
			if not _is_family_temp(ins.value):
				raise AssertionError(
					f"string_arc release-recognition tripwire "
					f"[unexpected input release]: block '{block.name}'[{idx}], "
					f"value '{ins.value}' — operand is not a block-local "
					f"family-producer String temp "
					f"(producer={type(producers.get(ins.value)).__name__}). "
					f"Only the string_releases materialization pass may "
					f"emit StringRelease before string_arc."
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
	# excluded from counting and suppress string_arc's own release,
	# turning an emission bug into a silent use-after-release.  Each
	# recognized release must be the unique release of its temp, the
	# temp's remaining occurrences must all be USE, and the release must
	# sit immediately after the draining instruction.  Anything else is
	# fail-closed (same AssertionError → driver-boundary diagnostic path
	# as the dead-stake tripwires).
	for temp, rel_idxs in release_sites.items():
		occs = occurrences.get(temp, [])
		drain = max((i for i, _d in occs), default=None)
		# Placement: the release sits after the draining instruction,
		# separated ONLY by in-contract releases of temps draining at the
		# same instruction (same-group temps release CONSECUTIVELY — the
		# multiplicity/grouping reality string_arc's own emission
		# produces; a gap containing ANY non-release instruction, e.g. a
		# later use or a later drain point, still rejects).
		placement_ok = (
			bool(occs)
			and drain is not None
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
			and temp not in term_used
			and temp not in term_consumed
			and all(d == DISPOSITION_USE for _i, d in occs)
		)
		if not in_contract:
			raise AssertionError(
				f"string_arc release-recognition tripwire "
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
	)
	return points


def recognize_materialized_releases(
	block: M.BasicBlock,
	*,
	local_types: Mapping[str, TypeId],
	fn_infos: Mapping[FunctionId, FnInfo],
	type_table: TypeTable,
	live_out_names: Set[str],
) -> Set[str]:
	"""TLR-2b handshake: the set of temps whose pre-materialized
	StringRelease is IN-CONTRACT (shape AND placement — see
	`_analyze_lastuse_block`), raising fail-closed on any out-of-contract
	input release.  string_arc's per-block prescan consults this BEFORE
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
	extracted verbatim from insert_string_arc's inline fixpoint (TLR-2b)
	so the materialization pass and string_arc compute liveness with ONE
	author.  In-contract pre-materialized releases cannot change the
	result: their temps are defined earlier in the same block (block-local
	family producer), so the release occurrence never reaches
	`block_use`, and defs are untouched — the pass (running on MIR without
	releases) and string_arc (running on MIR with them) see identical
	live-out sets by construction."""

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


def insert_string_arc(
	func: M.MirFunc,
	*,
	type_table: TypeTable,
	fn_infos: Mapping[FunctionId, FnInfo],
) -> M.MirFunc:
	# (`is_destructor_method` was the site-local destructor-self
	# skip flag; retired in Phase 4 site-3 sub-step 2.  The lattice
	# now models the destructor's runtime-owned `self` at the
	# Return terminator and the per-local ledger consultation in
	# the Return branch handles the skip.)
	# Phase 3B step 1 (`drop_before_overwrite` swap): the ledger is
	# attached unconditionally by the driver and consulted as the
	# authoritative drop verdict at site 4.  Site 3 (`string_arc_return`)
	# remains observational pending its own swap.  Sites read the
	# canonical `DropPolicy.needs_drop` via `_compute_drop_policy` —
	# NOT the raw `TypeTable.has_drop` query that the 3A reporter
	# uses (the quarantined approximation in driftc.py).
	# Pass-entry consumer site.  Use `maybe_fresh_ledger` (soft form)
	# because string_arc legitimately runs against MIR without an
	# attached ledger in pass-local testing.  A *stale* ledger still
	# asserts; that is the bug class we are catching.
	_ledger = maybe_fresh_ledger(func, "string_arc")
	# B-arch-0 differential stake audit (Scope B §11.2).  OFF by default:
	# `_audit is None` unless DRIFT_STRING_ARC_AUDIT=1, and every
	# recording site below is guarded on that — the disabled path is
	# behavior-identical (zero instructions, zero diagnostics, zero
	# allocations beyond this None).  `_audit_point` is the pre-MIR
	# program point of the instruction currently being rewritten (the
	# L_pre anchor); Return-boundary emissions use the established
	# site-3 convention (block, len(original_instructions)).
	_audit = (
		_ledger_reporter.StringArcAudit(func.name)
		if _ledger_reporter.string_arc_audit_enabled()
		else None
	)
	_audit_point: list = [("", 0)]
	def _ledger_needs_drop(local: str) -> bool:
		ty = local_types.get(local)
		if ty is None:
			return False
		try:
			return bool(type_table.has_drop(ty))
		except Exception:
			return False
	string_ty = type_table.ensure_string()
	local_types: Dict[str, TypeId] = func.local_types
	string_locals: Set[str] = {
		name for name in (list(func.params) + list(func.locals)) if local_types.get(name) == string_ty
	}
	storage_locals: Set[str] = set(func.params)
	for _blk in func.blocks.values():
		for _ins in _blk.instructions:
			local_name = getattr(_ins, "local", None)
			if isinstance(local_name, str):
				storage_locals.add(local_name)
	addr_taken_locals: Set[str] = set()
	for _blk in func.blocks.values():
		for _ins in _blk.instructions:
			if isinstance(_ins, M.AddrOfLocal):
				addr_taken_locals.add(_ins.local)
	_type_needs_drop_cache: Dict[TypeId, bool] = {}
	# Cycle guard for _type_needs_drop: tids whose by-value field closure is
	# still being computed up the call stack. A directly-recursive value type is
	# rejected earlier by validate_no_recursive_value_types, but malformed/legacy
	# package metadata could still present one here; this prevents a raw Python
	# RecursionError on the back-edge (the result cache is written only AFTER the
	# recursion returns, so it cannot break the cycle on its own).
	_type_needs_drop_active: Set[TypeId] = set()
	block_order = sorted(func.blocks.keys())

	used_ids: Set[str] = set(local_types.keys())
	arc_counter = 0

	def _new_temp() -> str:
		nonlocal arc_counter
		while True:
			arc_counter += 1
			name = f"__arc{arc_counter}"
			if name not in used_ids:
				used_ids.add(name)
				return name

	def _is_string_tid(tid: TypeId | None) -> bool:
		return tid == string_ty

	def _type_needs_drop(tid: TypeId) -> bool:
		cached = _type_needs_drop_cache.get(tid)
		if cached is not None:
			return cached
		if tid in _type_needs_drop_active:
			# Cycle back-edge: a directly-recursive value type should have been
			# rejected by validate_no_recursive_value_types; break the edge as
			# False (the correct least-fixpoint seed for the OR-of-fields below)
			# instead of recursing forever into a Python RecursionError. Do NOT
			# cache this provisional False — the outer in-progress call computes
			# and caches the real result.
			return False
		_type_needs_drop_active.add(tid)
		try:
			td = type_table.get(tid)
			if hasattr(type_table, "is_destructible"):
				try:
					if bool(type_table.is_destructible(tid)):
						_type_needs_drop_cache[tid] = True
						return True
				except Exception:
					pass
			if td.kind is TypeKind.SCALAR:
				needs = td.name == "String"
				_type_needs_drop_cache[tid] = needs
				return needs
			if td.kind is TypeKind.REF:
				_type_needs_drop_cache[tid] = False
				return False
			if td.kind is TypeKind.ERROR:
				_type_needs_drop_cache[tid] = True
				return True
			if td.kind is TypeKind.ARRAY and td.param_types:
				_type_needs_drop_cache[tid] = True
				return True
			if td.kind is TypeKind.STRUCT:
				inst = type_table.get_struct_instance(tid)
				if inst is not None:
					needs = any(_type_needs_drop(fty) for fty in inst.field_types)
					_type_needs_drop_cache[tid] = needs
					return needs
			if td.kind is TypeKind.VARIANT:
				inst = type_table.get_variant_instance(tid)
				if inst is not None:
					needs = any(_type_needs_drop(fty) for arm in inst.arms for fty in arm.field_types)
					_type_needs_drop_cache[tid] = needs
					return needs
			if td.param_types:
				needs = any(_type_needs_drop(pt) for pt in td.param_types)
				_type_needs_drop_cache[tid] = needs
				return needs
			_type_needs_drop_cache[tid] = False
			return False
		finally:
			_type_needs_drop_active.discard(tid)

	# __borrow_tmp: Stage 1 (borrow_materialize.py) normally materialises rvalue
	# borrows into __tmp_borrow* locals that get scope-based drops.  This Stage 2
	# inclusion is a defensive fallback in case that assumption breaks.
	array_locals: Set[str] = {
		name
		for name in (list(func.params) + list(func.locals))
		if (not name.startswith("__")) or name.startswith("__match_binder_") or name.startswith("__borrow_tmp")
		if (tid := local_types.get(name)) is not None
		and type_table.get(tid).kind is TypeKind.ARRAY
	}

	def _is_destructible_tid(tid: TypeId | None) -> bool:
		if tid is None:
			return False
		return _type_needs_drop(tid)

	def _is_error_tid(tid: TypeId | None) -> bool:
		if tid is None:
			return False
		return type_table.get(tid).kind is TypeKind.ERROR

	_dtor_fns = getattr(type_table, "destructor_fns", None) or {}
	_nullsafe_drop_cache: Dict[TypeId, bool] = {}
	# Cycle guard for _is_nullsafe_drop, mirroring _type_needs_drop_active: the
	# result cache is written only after recursion returns, so it cannot break a
	# self-loop in a malformed/legacy recursive value type on its own.
	_nullsafe_drop_active: Set[TypeId] = set()

	def _is_nullsafe_drop(tid: TypeId) -> bool:
		cached = _nullsafe_drop_cache.get(tid)
		if cached is not None:
			return cached
		if tid in _nullsafe_drop_active:
			# Cycle back-edge: a directly-recursive value type should have been
			# rejected before stage 2. True is the identity for the `all()`
			# aggregation below, so the back-edge does not alter the real
			# (non-cyclic) verdict; recurse no further (avoids RecursionError).
			# Not cached — the outer in-progress call caches the real result.
			return True
		_nullsafe_drop_active.add(tid)
		try:
			return _is_nullsafe_drop_body(tid)
		finally:
			_nullsafe_drop_active.discard(tid)

	def _is_nullsafe_drop_body(tid: TypeId) -> bool:
		if tid in _dtor_fns:
			_nullsafe_drop_cache[tid] = False
			return False
		td = type_table.get(tid)
		if td.kind is TypeKind.SCALAR:
			_nullsafe_drop_cache[tid] = True
			return True
		if td.kind is TypeKind.ARRAY:
			_nullsafe_drop_cache[tid] = True
			return True
		if td.kind is TypeKind.ERROR:
			_nullsafe_drop_cache[tid] = True
			return True
		if td.kind is TypeKind.INTERFACE:
			_nullsafe_drop_cache[tid] = True
			return True
		if td.kind is TypeKind.STRUCT:
			inst = type_table.get_struct_instance(tid)
			if inst is not None:
				safe = all(_is_nullsafe_drop(fty) for fty in inst.field_types if _type_needs_drop(fty))
				_nullsafe_drop_cache[tid] = safe
				return safe
		if td.kind is TypeKind.VARIANT:
			inst = type_table.get_variant_instance(tid)
			if inst is not None:
				safe = all(_is_nullsafe_drop(fty) for arm in inst.arms for fty in arm.field_types if _type_needs_drop(fty))
				_nullsafe_drop_cache[tid] = safe
				return safe
		_nullsafe_drop_cache[tid] = False
		return False

	# __borrow_tmp: defensive fallback — see comment above array_locals.
	destructible_locals: Set[str] = {
		name
		for name in (list(func.params) + list(func.locals))
		if (not name.startswith("__")) or name.startswith("__match_binder_") or name.startswith("__borrow_tmp") or _is_error_tid(local_types.get(name))
		if name not in string_locals
		if name not in array_locals
		if _is_destructible_tid(local_types.get(name))
	}
	nullsafe_destructible_locals: Set[str] = {name for name in destructible_locals if _is_nullsafe_drop(local_types[name])}

	def _is_string_value(val: str) -> bool:
		return _is_string_tid(local_types.get(val))

	def _is_local_name(val: str) -> bool:
		# Always False.  After the LoadLocal case in _iter_used_values
		# stopped yielding instr.local, only SSA value names ever flow
		# through the use/def/owned-defs analyses below.  Treating an SSA
		# value name as "a local" because the MIR temp counter happened
		# to issue a name string that also appears as a user storage
		# local was the root cause of a memcheck-visible leak: when a
		# user wrote `val t4 = call_returning_owned_string()` and the
		# call's SSA dest happened to be `t4`, the StoreLocal rewriter
		# excluded the dest from `owned_values` and inserted a spurious
		# retain via _ensure_owned, leaving the original +1 reference
		# from the call return unbalanced.
		return False

	def _ensure_owned(
		val: str,
		owned: Set[str],
		out: list[M.MInstr],
		site_class: str = _ledger_reporter.SITE_CLASS_VALUE_POSITION_RETAIN,
	) -> str:
		if not _is_string_value(val):
			return val
		if val in use_counts:
			use_counts[val] -= 1
			if use_counts[val] == 0 and val in owned and val not in live_out.get(block.name, set()):
				if _audit is not None:
					_audit.note(
						_ledger_reporter.STAKE_RELEASE, val,
						_ledger_reporter.SITE_CLASS_TEMP_LASTUSE_RELEASE,
						pre_point=_audit_point[0],
						post_point=(block.name, len(out)),
					)
				out.append(M.StringRelease(value=val))
				owned.discard(val)
				move_only_values.discard(val)
		# Slice 4b: the terminal late-retain arm — reached only for a
		# PROVEN-String value that no move/owned pre-check approved —
		# is corpus-zero for EVERY remaining site class funneling here
		# (call_arg_retain, value_position_retain, return_retain_site3;
		# store_value_retain was rerouted to its own tripwires in 4a)
		# and is now fail-closed.  The untyped pass-through above and
		# the last-use RELEASE bookkeeping (live, temp_lastuse_release)
		# are intentionally untouched — the 4a two-arm lesson.
		_dead_stake_tripwire(
			val,
			site_class=site_class,
			target=f"late-retain consume ({site_class})",
			block_name=block.name,
			idx=_audit_point[0][1],
		)
		return val  # unreachable — _dead_stake_tripwire always raises

	def _dead_stake_tripwire(
		val: str,
		*,
		site_class: str,
		target: str,
		block_name: str,
		idx: int,
	) -> None:
		"""Shared dead-stake tripwire (string-cleanup slices 4a/4b):
		every LATE-RETAIN stake class string_arc could still emit —
		store_value_retain (4a: rerouted store arms), call_arg_retain,
		value_position_retain, return_retain_site3 (4b: the central
		`_ensure_owned` retain arm) — is corpus-zero (B-arch drove the
		inventory 114,107 → 0; the C2 ZeroValue fix removed the last
		wild carrier) and fail-closed pending deletion after a clean
		cert cycle.  Only the PROVEN-String retain arm trips; untyped
		pass-through and move/owned pre-checks are untouched.  The
		AssertionError is converted to a clean `internal:` diagnostic
		at the driver's string_arc boundary — operators never see a
		Python traceback."""
		producer = "unknown"
		_blk = func.blocks.get(block_name)
		if _blk is not None:
			for _p_ins in _blk.instructions:
				if getattr(_p_ins, "dest", None) == val:
					producer = type(_p_ins).__name__
					break
		raise AssertionError(
			f"string_arc dead-stake tripwire [{site_class}]: "
			f"fn '{func.name}', block '{block_name}'[{idx}], "
			f"value '{val}' -> {target}, producer={producer}. "
			f"This stake class is corpus-zero and fail-closed pending "
			f"deletion (string-cleanup slices 4a/4b). A firing on real "
			f"source is a LANGUAGE_BUG: file "
			f"issues/string-arc-dead-stake-tripwire/ with the compiling "
			f"source and this full message."
		)

	def _param_is_string(tid: TypeId) -> bool:
		td = type_table.get(tid)
		return td.kind is TypeKind.SCALAR and td.name == "String"

	def _param_is_ref(tid: TypeId) -> bool:
		td = type_table.get(tid)
		return td.kind is TypeKind.REF

	def _release_local(local: str, out: list[M.MInstr], *, site_class: str) -> None:
		if local not in string_locals:
			return
		old = _new_temp()
		out.append(M.LoadLocal(dest=old, local=local))
		zero = _new_temp()
		out.append(M.ZeroValue(dest=zero, ty=string_ty))
		local_types[zero] = string_ty
		out.append(M.StoreLocal(local=local, value=zero))
		if _audit is not None:
			# Subject is the STORAGE LOCAL (what C1 quantifies over),
			# not the loaded temp.
			_audit.note(
				_ledger_reporter.STAKE_RELEASE, local, site_class,
				pre_point=_audit_point[0],
				post_point=(block.name, len(out)),
			)
		out.append(M.StringRelease(value=old))
		local_types[old] = string_ty

	def _release_all_locals(out: list[M.MInstr], *, skip_locals: Set[str] | None = None) -> None:
		skip = skip_locals or set()
		for local in sorted(string_locals):
			if local in skip:
				continue
			_release_local(local, out, site_class=_ledger_reporter.SITE_CLASS_SCOPE_EXIT_RELEASE)

	def _drop_array_local(local: str, out: list[M.MInstr]) -> None:
		if local not in array_locals:
			return
		arr_ty = local_types.get(local)
		if arr_ty is None:
			return
		td = type_table.get(arr_ty)
		if td.kind is not TypeKind.ARRAY or not td.param_types:
			return
		elem_ty = td.param_types[0]
		tmp = _new_temp()
		out.append(M.LoadLocal(dest=tmp, local=local))
		zero = _new_temp()
		out.append(M.ZeroValue(dest=zero, ty=arr_ty))
		local_types[zero] = arr_ty
		out.append(M.StoreLocal(local=local, value=zero))
		out.append(M.ArrayDrop(elem_ty=elem_ty, array=tmp))
		local_types[tmp] = arr_ty

	def _drop_all_arrays(out: list[M.MInstr], *, skip_locals: Set[str] | None = None) -> None:
		skip = skip_locals or set()
		for local in sorted(array_locals):
			if local in skip:
				continue
			_drop_array_local(local, out)

	def _drop_destructible_local(local: str, out: list[M.MInstr]) -> None:
		if local not in destructible_locals:
			return
		ty = local_types.get(local)
		if ty is None:
			return
		tmp = _new_temp()
		out.append(M.LoadLocal(dest=tmp, local=local))
		zero = _new_temp()
		out.append(M.ZeroValue(dest=zero, ty=ty))
		local_types[zero] = ty
		out.append(M.StoreLocal(local=local, value=zero))
		out.append(M.DropValue(value=tmp, ty=ty))
		local_types[tmp] = ty

	def _drop_all_destructibles(
		out: list[M.MInstr],
		*,
		skip_locals: Set[str] | None = None,
		only_locals: Set[str] | None = None,
	) -> None:
		skip = skip_locals or set()
		only = only_locals
		for local in sorted(destructible_locals):
			if local in skip:
				continue
			if only is not None and local not in only:
				continue
			_drop_destructible_local(local, out)

	# TLR-2a: single-source alias — the shared occurrence iterator
	# lives at module level (iter_used_values) so the release-point
	# calculator counts EXACTLY the occurrences this pass counts.
	_iter_used_values = iter_used_values

	def _copy_span(dst: M.MInstr, src: M.MInstr) -> None:
		if hasattr(src, "span"):
			setattr(dst, "span", getattr(src, "span"))

	def _iter_term_used(term: M.MTerminator) -> Iterable[str]:
		# Central MIR terminator value-use contract (stage2/cfg.py →
		# MTerminator.value_uses()): Return→value, IfTerminator→cond,
		# SwitchTerminator→scrutinee.  Keeping this central means a value consumed
		# only by a (new) terminator stays live without editing this scanner.
		yield from _cfg.terminator_value_uses(term)

	def _seed_dest_types() -> None:
		"""Pre-seed missing destination types before ARC liveness/use
		analysis.  Delegates to the shared module-level
		`seed_string_dest_types` (TLR-2a) — single source with the
		TLR-2b pass."""
		seed_string_dest_types(
			[func.blocks[bname] for bname in block_order],
			local_types,
			fn_infos=fn_infos,
			type_table=type_table,
		)

	def _block_succs(term: M.MTerminator | None) -> list[str]:
		# Central MIR CFG-successor contract (stage2/cfg.py).
		return _cfg.terminator_successors(term)

	def _block_preds() -> Dict[str, Set[str]]:
		preds: Dict[str, Set[str]] = {name: set() for name in block_order}
		for name in block_order:
			for succ in _block_succs(func.blocks[name].terminator):
				if succ in preds:
					preds[succ].add(name)
		return preds

	# Fill in missing destination types first so string-use liveness sees all
	# intermediate string temps (including conversion/call-produced values).
	_seed_dest_types()

	# Per-block live-out for string temps — delegates to the shared
	# module-level fixpoint (TLR-2b): single liveness author with the
	# materialization pass.  (`_is_local_name` is always False — see its
	# definition — so the historical name-exclusion is a no-op the shared
	# helper drops.)
	live_out = compute_string_temp_liveness(
		func.blocks,
		block_order,
		local_types=local_types,
		string_ty=string_ty,
	)

	# Definite local assignment across CFG.
	preds = _block_preds()
	store_defs: Dict[str, Set[str]] = {}
	for name in block_order:
		stores: Set[str] = set()
		for instr in func.blocks[name].instructions:
			if isinstance(instr, M.StoreLocal):
				stores.add(instr.local)
			elif isinstance(instr, M.MoveFromRef):
				# MoveFromRef is a definite assignment to `local` — the
				# transferred bytes land in the local's storage.
				stores.add(instr.local)
		store_defs[name] = stores
	assigned_in: Dict[str, Set[str]] = {name: set() for name in block_order}
	assigned_out: Dict[str, Set[str]] = {name: set() for name in block_order}
	assigned_in[func.entry] = set(func.params)
	assigned_out[func.entry] = set(func.params) | store_defs.get(func.entry, set())
	changed = True
	while changed:
		changed = False
		for name in block_order:
			if name == func.entry:
				new_in = set(func.params)
			else:
				ps = preds.get(name, set())
				if not ps:
					new_in = set()
				else:
					it = iter(ps)
					new_in = set(assigned_out[next(it)])
					for p in it:
						new_in &= assigned_out[p]
			new_out = new_in | store_defs.get(name, set())
			if new_in != assigned_in[name] or new_out != assigned_out[name]:
				assigned_in[name] = new_in
				assigned_out[name] = new_out
				changed = True

	# Track locals that are definitely moved-out at each block boundary so
	# successor return blocks do not re-drop moved values.
	moved_in: Dict[str, Set[str]] = {name: set() for name in block_order}
	moved_out: Dict[str, Set[str]] = {name: set() for name in block_order}
	changed = True
	while changed:
		changed = False
		for name in block_order:
			if name == func.entry:
				new_in = set()
			else:
				ps = preds.get(name, set())
				if not ps:
					new_in = set()
				else:
					it = iter(ps)
					new_in = set(moved_out[next(it)])
					for p in it:
						new_in &= moved_out[p]
			cur = set(new_in)
			for instr in func.blocks[name].instructions:
				if isinstance(instr, M.StoreLocal):
					cur.discard(instr.local)
				elif isinstance(instr, M.MoveFromRef):
					# Fresh assignment; local is no longer "moved-out."
					cur.discard(instr.local)
				elif isinstance(instr, M.MoveOut):
					cur.add(instr.local)
			new_out = cur
			if new_in != moved_in[name] or new_out != moved_out[name]:
				moved_in[name] = new_in
				moved_out[name] = new_out
				changed = True

	owned_defs: Set[str] = set()
	move_only_defs: Set[str] = set()
	for name in block_order:
		for instr in func.blocks[name].instructions:
			dest = getattr(instr, "dest", None)
			if dest is None or not _is_string_value(dest) or _is_local_name(dest):
				continue
			if isinstance(instr, (M.ConstString, M.StringConcat, M.StringFromInt, M.StringFromBool, M.StringFromUint, M.StringFromFloat)):
				owned_defs.add(dest)
			elif isinstance(instr, (M.StringRetain, M.CopyValue)):
				owned_defs.add(dest)
			elif isinstance(instr, M.ExcGetParamsJson):
				# `drift_error_get_params_json` returns a RETAINED
				# DriftString per ABI spec §2.3 — caller owns and is
				# responsible for releasing.  Tracked as an owned
				# string-result alongside the StringConcat / Call class.
				owned_defs.add(dest)
			elif isinstance(instr, M.ExcGetContextJson):
				# Same retained-string contract as ExcGetParamsJson.
				owned_defs.add(dest)
			elif isinstance(instr, M.ErrorEventFqn):
				# Slice 3 DV→JSON: codegen retains the extracted
				# event_fqn String so the dest is independently owned.
				owned_defs.add(dest)
			elif isinstance(instr, (M.Call, M.CallIndirect, M.CallIface)):
				# String-returning calls produce owned values that must be released
				# when their last use in the block is consumed.
				# TLR-4: this fn-wide prepass is the ONLY owned
				# registration for call dests (no rewrite-loop re-add
				# arm), so family suppression for recognized call temps
				# is fully covered by the per-block
				# `owned_values -= recognized_released` subtraction.
				owned_defs.add(dest)
			elif isinstance(instr, M.PtrRead):
				if _is_string_tid(instr.elem_ty):
					owned_defs.add(dest)
			elif isinstance(instr, M.RawBufferRead):
				# `mem.read<T>(&mut buf, i)` moves the slot's value out;
				# slot becomes uninitialized.  For refcount-bearing T
				# (String) the +1 stake travels with the read result —
				# treat it as already-owned, identical to `ArrayElemTake`
				# / `PtrRead`.  Without this, the subsequent
				# `StoreLocal(local, dest)` in
				# `val v = mem.read<String>(...)` inserts a spurious
				# `StringRetain` (refcount → 2), and the final drop
				# leaves refcount at 1 → the original allocation leaks
				# (carrier C7 in `test_typebox_take.py`; web-team
				# `ctx_take<T>` is the motivating use case).
				if _is_string_tid(instr.elem_ty):
					owned_defs.add(dest)
					move_only_defs.add(dest)
			elif isinstance(instr, M.MoveOut):
				owned_defs.add(dest)
				move_only_defs.add(dest)
			elif isinstance(instr, M.ArrayElemTake):
				owned_defs.add(dest)
				move_only_defs.add(dest)

	for name in block_order:
		block = func.blocks[name]
		new_instrs: list[M.MInstr] = []
		owned_values: Set[str] = set(owned_defs)
		move_only_values: Set[str] = set(move_only_defs)
		moved_out_locals: Set[str] = set(moved_in.get(block.name, set()))
		explicitly_dropped_locals: Set[str] = set()
		load_local_src: Dict[str, str] = {}

		# Initialize string locals in the entry block to avoid uninitialized releases.
		if block.name == func.entry:
			for local in func.locals:
				if local in func.params:
					continue
				if local_types.get(local) != string_ty:
					continue
				zero = _new_temp()
				new_instrs.append(M.ZeroValue(dest=zero, ty=string_ty))
				new_instrs.append(M.StoreLocal(local=local, value=zero))
				local_types[zero] = string_ty
			for local in func.locals:
				if local in func.params:
					continue
				if local not in array_locals:
					continue
				arr_ty = local_types.get(local)
				if arr_ty is None:
					continue
				zero = _new_temp()
				new_instrs.append(M.ZeroValue(dest=zero, ty=arr_ty))
				new_instrs.append(M.StoreLocal(local=local, value=zero))
				local_types[zero] = arr_ty
			for local in func.locals:
				if local in func.params:
					continue
				if local not in nullsafe_destructible_locals:
					continue
				dest_ty = local_types.get(local)
				if dest_ty is None:
					continue
				zero = _new_temp()
				new_instrs.append(M.ZeroValue(dest=zero, ty=dest_ty))
				new_instrs.append(M.StoreLocal(local=local, value=zero))
				local_types[zero] = dest_ty
		# TLR-2b recognition — BEFORE use counting (the load-bearing
		# ordering: StringRelease is a use per `_iter_used_values`, so an
		# unrecognized materialized release would inflate its temp's
		# count and move the last-use point).  In-contract
		# pre-materialized releases are excluded from occurrence counts
		# entirely; ANY out-of-contract input release fails closed inside
		# the shared analysis (`unexpected input release`).  Fast path:
		# blocks without input releases (every unit-test run without the
		# materialization pass; post-pass blocks with no qualified temps)
		# skip the analysis.
		recognized_released: Set[str] = (
			recognize_materialized_releases(
				block,
				local_types=local_types,
				fn_infos=fn_infos,
				type_table=type_table,
				live_out_names=live_out.get(block.name, set()),
			)
			if any(isinstance(_i, M.StringRelease) for _i in block.instructions)
			else set()
		)
		# TLR-2b suppression, fn-wide-prepass half: `owned_values` was
		# seeded above from the fn-wide `owned_defs` prepass, which
		# registers EVERY ConstString dest — an externally-released temp
		# must not be owned or `_note_use` emits a second release at the
		# drain.  (The ConstString rewrite arm below skips its re-add
		# symmetrically.)
		owned_values -= recognized_released
		# Count uses in this block for temp string values.
		use_counts: Dict[str, int] = {}
		producers: Dict[str, M.MInstr] = {}
		for instr in block.instructions:
			if isinstance(instr, M.StringRelease) and instr.value in recognized_released:
				# TLR-2b prescan-exclusion: contributes no occurrence
				# (symmetric with the rewrite loop's recognition arm,
				# which copies it through with no `_note_use`).
				continue
			dest = getattr(instr, "dest", None)
			if isinstance(dest, str):
				producers[dest] = instr
			# String ZeroValue and element-extraction dests carry their
			# type on the INSTRUCTION; register it before use counting so
			# the owned/single-use store path sees them even when
			# upstream metadata omitted the temp from func.local_types.
			# Without this, a metadata gap makes the slice-4a
			# store_value tripwire FIRE on an owned value (false
			# positive: `use_counts` skips untyped values, so
			# `_can_move_owned_once` can never approve the move) instead
			# of taking the no-stake/move route.
			if isinstance(instr, M.ZeroValue) and _is_string_tid(instr.ty) and instr.dest not in local_types:
				local_types[instr.dest] = instr.ty
			elif (
				isinstance(instr, (M.ArrayIndexLoad, M.ArrayIndexLoadUnchecked))
				and _is_string_tid(instr.elem_ty)
				and instr.dest not in local_types
			):
				local_types[instr.dest] = instr.elem_ty
			for val in _iter_used_values(instr):
				if _is_string_value(val) and not _is_local_name(val):
					use_counts[val] = use_counts.get(val, 0) + 1
		if block.terminator is not None:
			for val in _iter_term_used(block.terminator):
				if _is_string_value(val) and not _is_local_name(val):
					use_counts[val] = use_counts.get(val, 0) + 1

		def _note_use(val: str, *, consume: bool) -> None:
			if val not in use_counts:
				return
			use_counts[val] -= 1
			if consume and val in owned_values:
				owned_values.discard(val)
				move_only_values.discard(val)
				return
			if use_counts[val] == 0 and val in owned_values and val not in live_out.get(block.name, set()):
				if _audit is not None:
					# TLR-1 (option-B shim): classification split ONLY —
					# the SAME StringRelease is emitted on the SAME path
					# at the SAME position below.  Block-local
					# MATERIALIZED_RELEASE_FAMILY temps (the per-block
					# producers map implies co-block production; the
					# live_out guard above implies the temp is dead after
					# this block) are the string_releases pass's
					# ownership boundary — in production every such temp
					# is pre-materialized and recognized, so this branch
					# is dead there; it still classifies arc-only unit
					# runs, which is what the A/B pins compare.  Same
					# constant as the analysis/recognition: no drift.
					_tlr_cls = (
						_ledger_reporter.SITE_CLASS_MATERIALIZED_LASTUSE_RELEASE
						if is_materialized_release_family_producer(
							producers.get(val), fn_infos=fn_infos, type_table=type_table
						)
						else _ledger_reporter.SITE_CLASS_TEMP_LASTUSE_RELEASE
					)
					_audit.note(
						_ledger_reporter.STAKE_RELEASE, val,
						_tlr_cls,
						pre_point=_audit_point[0],
						post_point=(block.name, len(new_instrs)),
					)
				new_instrs.append(M.StringRelease(value=val))
				owned_values.discard(val)
				move_only_values.discard(val)

		def _can_move_owned_once(val: str) -> bool:
			return val in owned_values and use_counts.get(val, 0) == 1

		def _is_string_creator(instr: M.MInstr | None) -> bool:
			if instr is None:
				return False
			if isinstance(instr, (M.ConstString, M.StringConcat, M.StringRetain, M.StringFromInt, M.StringFromBool, M.StringFromUint, M.StringFromFloat)):
				return True
			if isinstance(instr, M.Call):
				info = fn_infos.get(instr.fn_id)
				if info is not None and info.signature is not None and not bool(getattr(info, "declared_can_throw", False)) and info.signature.return_type_id == string_ty and local_types.get(getattr(instr, "dest", "")) == string_ty:
					return True
				sym = function_symbol(instr.fn_id)
				return sym in DRIFT_STRING_HELPER_SYMBOLS
			return False

		def _can_move_creator_return(val: str) -> bool:
			return use_counts.get(val, 0) <= 1 and _is_string_creator(producers.get(val))

		def _collect_return_source_locals(v: str, seen: Set[str] | None = None) -> Set[str]:
			seen_vals = seen or set()
			if v in seen_vals:
				return set()
			seen_vals.add(v)
			for prev in reversed(new_instrs):
				if isinstance(prev, M.AssignSSA) and prev.dest == v:
					return _collect_return_source_locals(prev.src, seen_vals)
				if isinstance(prev, M.LoadLocal) and prev.dest == v:
					return {prev.local}
			prod = producers.get(v)
			if prod is None:
				return set()
			locals_used: Set[str] = set()
			if isinstance(prod, M.ConstructStruct):
				for a in prod.args:
					locals_used |= _collect_return_source_locals(a, seen_vals)
			elif isinstance(prod, M.ConstructVariant):
				for a in prod.args:
					locals_used |= _collect_return_source_locals(a, seen_vals)
			elif isinstance(prod, M.ConstructResultOk):
				if prod.value is not None:
					locals_used |= _collect_return_source_locals(prod.value, seen_vals)
			elif isinstance(prod, M.ConstructIfaceValue):
				locals_used |= _collect_return_source_locals(prod.value, seen_vals)
			return locals_used

		for _instr_idx, instr in enumerate(block.instructions):
			if _audit is not None:
				_audit_point[0] = (block.name, _instr_idx)
			if isinstance(instr, M.StringRelease) and instr.value in recognized_released:
				# TLR-2b recognition arm: copy the pre-materialized
				# release through verbatim with NO `_note_use` — its
				# occurrence was never counted (prescan-exclusion), so
				# no decrement may happen either; an uncounted decrement
				# would skew `_can_move_owned_once` decisions.  The
				# audit note here keeps `materialized_lastuse_release`
				# author-independent (same event, new author) and
				# `events` constant.
				if _audit is not None:
					_audit.note(
						_ledger_reporter.STAKE_RELEASE, instr.value,
						_ledger_reporter.SITE_CLASS_MATERIALIZED_LASTUSE_RELEASE,
						pre_point=_audit_point[0],
						post_point=(block.name, len(new_instrs)),
					)
				new_instrs.append(instr)
				continue
			if isinstance(instr, M.StoreLocal):
				moved_out_locals.discard(instr.local)
				explicitly_dropped_locals.discard(instr.local)
			if isinstance(instr, M.MoveFromRef):
				# MoveFromRef defines `local` (transferred ownership);
				# clear any prior moved/dropped state.
				moved_out_locals.discard(instr.local)
				explicitly_dropped_locals.discard(instr.local)
			if isinstance(instr, M.StoreLocal) and instr.local in array_locals:
				_drop_array_local(instr.local, new_instrs)
				new_instrs.append(instr)
				continue
			if isinstance(instr, M.StoreLocal) and instr.local in nullsafe_destructible_locals:
				_drop_destructible_local(instr.local, new_instrs)
				new_instrs.append(instr)
				continue
			if isinstance(instr, M.StoreLocal) and instr.local in destructible_locals:
				# Phase 4 (post-3c) — `drop_before_overwrite` promoted
				# to **Tier 1: pure ledger authority.**  The legacy
				# `initialized_destructibles` dataflow fallback is
				# RETIRED.  Site 4 authors nothing of its own; the
				# verdict comes from `_ledger.verdict_at(...)` with
				# `_compute_drop_policy(type_table, ty).needs_drop` as
				# the sole type-level needs_drop axis.
				#
				# Proof obligation: the ledger MUST resolve every
				# StoreLocal point here to either `MUST_DROP` or
				# `MUST_NOT_DROP`.  `PathDependent` (lattice
				# `MaybeUninit` at this point) is unreached at smoke
				# + e2e today (100 % verdict agreement across 1031
				# cases); if it ever appears, the `RuntimeError`
				# below fires loudly so the regression is investigated
				# before it silently reintroduces split authority.
				# Same behavior if the ledger is unset — any caller
				# that hits site 4 MUST attach a ledger first.
				#
				# Build-timing invariant: the ledger consulted here is
				# the POST-`drop_flags` ledger (driver rebuilds it
				# between `drop_flags` and `string_arc`).  drop_flags
				# inserts drop-flag init instructions (`ConstBool` +
				# `StoreLocal(__drop_flag_*)`) at block heads which
				# shift every subsequent index, so the
				# pre-drop_flags ledger's `(block, idx)` keys do not
				# line up with the indices string_arc walks here.
				# Pinned by
				# `lang/tests/driver/test_if_join_drop_destructor_uniform_move.py`.
				if _ledger is None:
					raise RuntimeError(
						f"drop_before_overwrite invoked without an "
						f"attached ownership ledger (fn={func.name}, "
						f"block={block.name}, local={instr.local}); "
						f"Tier-1 site requires `func._ownership_ledger` "
						f"to be set by the driver before `string_arc`."
					)
				_local_ty = local_types.get(instr.local)
				_needs_drop = (
					bool(_compute_drop_policy(type_table, _local_ty).needs_drop)
					if _local_ty is not None
					else False
				)
				_verdict = _ledger.verdict_at(
					(block.name, _instr_idx),
					instr.local,
					needs_drop=_needs_drop,
				)
				if _verdict is _DropVerdict.MUST_DROP:
					_should_drop = True
					_site_verdict_str = _ledger_events.VERDICT_MUST_DROP
					_site_reason = _ledger_events.REASON_NEEDS_DROP
				elif _verdict is _DropVerdict.MUST_NOT_DROP:
					_should_drop = False
					_site_verdict_str = _ledger_events.VERDICT_MUST_NOT_DROP
					_site_reason = _ledger_events.REASON_NOT_DROP_NEEDING
				else:
					# PathDependent — proof-obligation tripwire.  The
					# observe re-run said the lattice never yields
					# MaybeUninit at drop_before_overwrite points.  If
					# it ever does, this raise signals the regression.
					raise RuntimeError(
						f"drop_before_overwrite: ledger returned "
						f"PathDependent at (fn={func.name}, "
						f"block={block.name}, idx={_instr_idx}, "
						f"local={instr.local}).  Tier-1 promotion "
						f"retired the `initialized_destructibles` "
						f"fallback — if PathDependent is now reachable, "
						f"either tighten the lattice or restore a "
						f"flag-guarded path here before re-landing."
					)
				if drift_debug.enabled("ownership_ledger"):
					_ledger_reporter.check(
						_ledger,
						fn_name=func.name,
						site=_ledger_events.SITE_DROP_BEFORE_OVERWRITE,
						point=(block.name, _instr_idx),
						local=instr.local,
						site_verdict=_site_verdict_str,
						site_reason=_site_reason,
						needs_drop=_ledger_needs_drop,
						emit=_ledger_reporter.stderr_emit,
					)
				if _should_drop:
					if _audit is not None:
						_audit.note(
							_ledger_reporter.STAKE_RELEASE, instr.local,
							_ledger_reporter.SITE_CLASS_DROP_BEFORE_OVERWRITE_SITE4,
							pre_point=(block.name, _instr_idx),
							post_point=(block.name, len(new_instrs)),
						)
					_drop_destructible_local(instr.local, new_instrs)
				new_instrs.append(instr)
				continue
			if isinstance(instr, M.MoveOut):
				# Emit load + zero-store, but keep ownership of the moved value.
				if _audit is not None:
					# Snapshot the cleanup-drop pairing from the SOURCE
					# stream now — finalize runs after every block was
					# rewritten, so the pre_point index stops aligning.
					_nxt = (
						block.instructions[_instr_idx + 1]
						if _instr_idx + 1 < len(block.instructions)
						else None
					)
					_audit.note(
						_ledger_reporter.STAKE_MOVEOUT_EXPANSION, instr.local,
						_ledger_reporter.SITE_CLASS_MOVEOUT_EXPANSION,
						pre_point=(block.name, _instr_idx),
						post_point=(block.name, len(new_instrs)),
						moveout_feeds_drop=(
							isinstance(_nxt, M.DropValue)
							and _nxt.value == instr.dest
						),
					)
				new_instrs.append(M.LoadLocal(dest=instr.dest, local=instr.local))
				local_types[instr.dest] = instr.ty
				if _is_string_tid(instr.ty):
					owned_values.add(instr.dest)
					move_only_values.add(instr.dest)
				zero = _new_temp()
				new_instrs.append(M.ZeroValue(dest=zero, ty=instr.ty))
				new_instrs.append(M.StoreLocal(local=instr.local, value=zero))
				local_types[zero] = instr.ty
				moved_out_locals.add(instr.local)
				# Post-MoveOut the storage is zeroed.  A subsequent
				# StoreLocal must NOT re-drop the zero bytes (tag=0
				# can dispatch to a ctor whose drop reads zeroed
				# reference fields → SEGV).  Tier-1 site 4 handles
				# this correctly via the ledger: the MoveOut
				# transfers state to `MOVED_OUT`, so `verdict_at` at
				# the next StoreLocal returns `MUST_NOT_DROP` and no
				# drop-before-overwrite is emitted.
				continue

			if isinstance(instr, M.ConstString):
				# TLR-2b suppression: an externally-released temp never
				# enters `owned_values`, so `_note_use`'s release arm
				# structurally CANNOT emit a second release for it.
				if instr.dest not in recognized_released:
					owned_values.add(instr.dest)
			elif isinstance(instr, (M.StringFromInt, M.StringFromBool, M.StringFromUint, M.StringFromFloat, M.StringConcat)):
				# TLR-3: StringConcat joined the release family —
				# same recognized-guard as the ConstString arm (the
				# StringFrom* members are not in the family yet; the
				# recognized set is empty for them, so the guard is a
				# no-op there).
				if instr.dest not in recognized_released:
					owned_values.add(instr.dest)
			elif isinstance(instr, M.StringRetain):
				owned_values.add(instr.dest)
			elif isinstance(instr, M.ZeroValue):
				# Zeroed String bytes are a valid OWNED empty value —
				# retain AND release of a zeroed String are both runtime
				# no-ops — so a fresh input-stream ZeroValue dest is
				# owned/no-stake-needed.  Without this, the store paths'
				# `_ensure_owned` emitted a dead retain of zeroed bytes;
				# the single wild carrier was the `captures(move <String>)`
				# env-slot ZERO-BACK (`StoreRef(env_field, ZeroValue)`) in
				# hidden-lambda prologues — the last invisible-stake
				# residual (c2_invisible_stake / store_value_retain
				# singletons, reconciled 2026-07-13, PROGRESS).
				if _is_string_tid(instr.ty):
					owned_values.add(instr.dest)
			elif isinstance(instr, M.CopyValue):
				if _is_string_tid(instr.ty):
					owned_values.add(instr.dest)
			elif isinstance(instr, M.LoadLocal):
				load_local_src[instr.dest] = instr.local
				load_ty = local_types.get(instr.local)
				if load_ty is not None:
					local_types[instr.dest] = load_ty
				if _is_string_tid(local_types.get(instr.local)):
					owned_values.discard(instr.dest)
			elif isinstance(instr, M.LoadRef):
				local_types[instr.dest] = instr.inner_ty
				if _is_string_tid(instr.inner_ty):
					owned_values.discard(instr.dest)
			elif isinstance(instr, M.StructGetField):
				local_types[instr.dest] = instr.field_ty
				if _is_string_tid(instr.field_ty):
					owned_values.discard(instr.dest)
			elif isinstance(instr, M.VariantGetField):
				local_types[instr.dest] = instr.field_ty
				if _is_string_tid(instr.field_ty):
					# `VariantGetField` for a String field is lowered in
					# LLVM codegen as `load field + drift_string_retain`
					# (copy-semantic transfer — see
					# `_classify_payload_extract_transfer` in
					# `llvm_codegen.py`).  The `dest` carries that +1
					# reference, matching `CopyValue`'s ownership shape,
					# NOT a borrowed view.  Treating it as borrowed
					# caused the subsequent `StoreLocal(..., dest)` in
					# the match-binder path to retain AGAIN via
					# `_ensure_owned`, producing an extra +1 that never
					# got released — observed as a 22-byte leak from
					# `drift_string_concat` in
					# `om_match_bind_string_heap_concat`'s
					# `scenario_value_producing_match`.
					#
					# Only `owned_values.add` — NOT `move_only_values`.
					# `_can_move_owned_once(val)` already checks
					# `val in owned AND use_counts == 1`, so a single-
					# consumer VariantGetField temp is moved without
					# retain.  Multi-consumer shapes (if any arise)
					# then go through the normal `_ensure_owned`
					# retain-for-additional-consumers path, releasing
					# the original +1 at the final use.  Adding to
					# `move_only_values` would bypass the single-use
					# guard and let the first consumer move the ref
					# while later consumers observed a consumed value.
					owned_values.add(instr.dest)
			elif isinstance(instr, (M.ArrayIndexLoad, M.ArrayIndexLoadUnchecked)):
				# OWNED AT EXTRACTION (B-arch-1d contract; see the
				# `# owned-at-extraction:` markers in llvm_codegen.py and
				# string_stakes.py, enforced by
				# test_extraction_retain_contract.py): codegen lowers a
				# String element load as load + drift_string_retain, so
				# the dest arrives with its own +1 — string_arc must MOVE
				# it (single use) or retain-for-additional-consumers via
				# the fallback, exactly like VariantGetField above.  The
				# pre-contract `discard` view classification orphaned the
				# codegen +1 whenever a consumer re-staked (the 1d leak
				# shape); it was corpus-latent on the CLI path and
				# surfaced through the in-process pipeline's explicit
				# bounds-check shape (`idx_ok`/`__idx_tmp` stores of
				# ArrayIndexLoadUnchecked dests) when the slice-4a
				# tripwire made the fallback fail-closed.  NOT
				# move_only_values — same multi-consumer rationale as the
				# VariantGetField arm.
				local_types[instr.dest] = instr.elem_ty
				if _is_string_tid(instr.elem_ty):
					owned_values.add(instr.dest)
			elif isinstance(instr, M.ArrayElemTake):
				local_types[instr.dest] = instr.elem_ty
				if _is_string_tid(instr.elem_ty):
					owned_values.add(instr.dest)
					move_only_values.add(instr.dest)
			elif isinstance(instr, M.PtrRead):
				local_types[instr.dest] = instr.elem_ty
				if _is_string_tid(instr.elem_ty):
					owned_values.add(instr.dest)
					move_only_values.add(instr.dest)
			elif isinstance(instr, M.RawBufferRead):
				# See the matching `RawBufferRead` branch in the
				# `owned_defs` pass above for the rationale.  Mirrors
				# `ArrayElemTake` / `PtrRead` — `mem.read<T>` is a
				# move-out-of-storage primitive; the read result owns
				# the +1 stake for refcount-bearing element types.
				local_types[instr.dest] = instr.elem_ty
				if _is_string_tid(instr.elem_ty):
					owned_values.add(instr.dest)
					move_only_values.add(instr.dest)
			elif isinstance(instr, M.AssignSSA):
				if _is_string_value(instr.src):
					if instr.src in owned_values:
						owned_values.add(instr.dest)
					else:
						owned_values.discard(instr.dest)

			if isinstance(instr, M.StoreLocal) and _is_string_tid(local_types.get(instr.local)):
				_release_local(instr.local, new_instrs, site_class=_ledger_reporter.SITE_CLASS_OVERWRITE_RELEASE)
				val = instr.value
				if val in move_only_values or _can_move_owned_once(val):
					new_instrs.append(M.StoreLocal(local=instr.local, value=val))
					_note_use(val, consume=True)
				else:
					# Slice 4a: the fallback's RETAIN arm — reached only
					# for a PROVEN-String value (`_is_string_value`) —
					# is corpus-zero (string_stakes owns store staking)
					# and fail-closed.  Values WITHOUT String type
					# metadata keep the fallback's other historical
					# behavior: `_ensure_owned` early-returned and the
					# value passed through unchanged (live, exercised by
					# e.g. can-throw call Ok-payload holders).
					if _is_string_value(val):
						_dead_stake_tripwire(
							val,
							site_class=_ledger_reporter.SITE_CLASS_STORE_VALUE_RETAIN,
							target=f"StoreLocal '{instr.local}'",
							block_name=block.name,
							idx=_instr_idx,
						)
					new_instrs.append(M.StoreLocal(local=instr.local, value=val))
					_note_use(val, consume=True)
				continue

			if isinstance(instr, M.MoveFromRef) and _is_string_tid(instr.inner_ty):
				# Ownership-transfer store: the source `*ptr` stake
				# moves into `local` atomically (codegen handles the
				# load + tombstone-write + transfer).  string_arc must
				# NOT insert a `StringRetain` here — the value is
				# transferred, not copied.
				#
				# Release any prior owned value at the destination so
				# we don't leak it.  For a freshly-UNINIT local (the
				# canonical match-cleanup-authoring use case),
				# `_release_local` issues a release on the zero bytes
				# loaded from the slot — `drift_string_release(null)`
				# is a runtime no-op, so this is safe regardless of
				# whether the local was previously written.
				_release_local(instr.local, new_instrs, site_class=_ledger_reporter.SITE_CLASS_OVERWRITE_RELEASE)
				new_instrs.append(instr)
				_note_use(instr.ptr, consume=True)
				continue

			if isinstance(instr, M.StoreRef) and _is_string_tid(instr.inner_ty):
				old = _new_temp()
				new_instrs.append(M.LoadRef(dest=old, ptr=instr.ptr, inner_ty=instr.inner_ty))
				if _audit is not None:
					_audit.note(
						_ledger_reporter.STAKE_RELEASE, old,
						_ledger_reporter.SITE_CLASS_OVERWRITE_RELEASE,
						pre_point=(block.name, _instr_idx),
						post_point=(block.name, len(new_instrs)),
					)
				new_instrs.append(M.StringRelease(value=old))
				local_types[old] = string_ty
				val = instr.value
				if val in move_only_values or _can_move_owned_once(val):
					new_instrs.append(M.StoreRef(ptr=instr.ptr, value=val, inner_ty=instr.inner_ty))
					_note_use(val, consume=True)
				else:
					# Slice 4a: fail-closed on the PROVEN-String retain
					# arm only (see the StoreLocal arm); untyped values
					# keep the historical pass-through.
					if _is_string_value(val):
						_dead_stake_tripwire(
							val,
							site_class=_ledger_reporter.SITE_CLASS_STORE_VALUE_RETAIN,
							target=f"StoreRef via '{instr.ptr}'",
							block_name=block.name,
							idx=_instr_idx,
						)
					new_instrs.append(M.StoreRef(ptr=instr.ptr, value=val, inner_ty=instr.inner_ty))
					_note_use(val, consume=True)
				continue

			if isinstance(instr, M.ArrayIndexStore) and _is_string_tid(instr.elem_ty):
				old = _new_temp()
				new_instrs.append(
					M.ArrayIndexLoad(dest=old, elem_ty=instr.elem_ty, array=instr.array, index=instr.index)
				)
				if _audit is not None:
					_audit.note(
						_ledger_reporter.STAKE_RELEASE, old,
						_ledger_reporter.SITE_CLASS_OVERWRITE_RELEASE,
						pre_point=(block.name, _instr_idx),
						post_point=(block.name, len(new_instrs)),
					)
				new_instrs.append(M.StringRelease(value=old))
				local_types[old] = string_ty
				val = instr.value
				if val in move_only_values or _can_move_owned_once(val):
					new_instrs.append(
						M.ArrayIndexStore(elem_ty=instr.elem_ty, array=instr.array, index=instr.index, value=val)
					)
					_note_use(val, consume=True)
				else:
					# Slice 4a: fail-closed on the PROVEN-String retain
					# arm only (see the StoreLocal arm); untyped values
					# keep the historical pass-through.
					if _is_string_value(val):
						_dead_stake_tripwire(
							val,
							site_class=_ledger_reporter.SITE_CLASS_STORE_VALUE_RETAIN,
							target=f"ArrayIndexStore into '{instr.array}'",
							block_name=block.name,
							idx=_instr_idx,
						)
					new_instrs.append(
						M.ArrayIndexStore(elem_ty=instr.elem_ty, array=instr.array, index=instr.index, value=val)
					)
					_note_use(val, consume=True)
				continue

			if isinstance(instr, (M.ArrayElemInit, M.ArrayElemInitUnchecked, M.ArrayElemAssign)) and _is_string_tid(instr.elem_ty):
				val = instr.value
				if val in move_only_values or _can_move_owned_once(val):
					new_instr = type(instr)(
						elem_ty=instr.elem_ty,
						array=instr.array,
						index=instr.index,
						value=val,
					)
					_copy_span(new_instr, instr)
					new_instrs.append(new_instr)
					_note_use(val, consume=True)
				else:
					val = _ensure_owned(val, owned_values, new_instrs)
					new_instr = type(instr)(
						elem_ty=instr.elem_ty,
						array=instr.array,
						index=instr.index,
						value=val,
					)
					_copy_span(new_instr, instr)
					new_instrs.append(new_instr)
					_note_use(val, consume=True)
				continue

			if isinstance(instr, M.ArrayLit) and _is_string_tid(instr.elem_ty):
				elems: list[str] = []
				for e in instr.elements:
					if e in move_only_values or _can_move_owned_once(e):
						elems.append(e)
						_note_use(e, consume=True)
					else:
						elems.append(_ensure_owned(e, owned_values, new_instrs))
						_note_use(e, consume=True)
				new_instr = M.ArrayLit(dest=instr.dest, elem_ty=instr.elem_ty, elements=elems)
				_copy_span(new_instr, instr)
				new_instrs.append(new_instr)
				continue

			if isinstance(instr, M.ConstructStruct):
				inst = type_table.get_struct_instance(instr.struct_ty)
				if inst is not None:
					args: list[str] = []
					for field_ty, arg in zip(inst.field_types, instr.args):
						if _is_string_tid(field_ty):
							if arg in move_only_values or _can_move_owned_once(arg):
								args.append(arg)
								_note_use(arg, consume=True)
							else:
								args.append(_ensure_owned(arg, owned_values, new_instrs, site_class=_ledger_reporter.SITE_CLASS_VALUE_POSITION_RETAIN))
								_note_use(arg, consume=True)
						else:
							args.append(arg)
					new_instrs.append(M.ConstructStruct(dest=instr.dest, struct_ty=instr.struct_ty, args=args))
					continue

			if isinstance(instr, M.ConstructVariant):
				inst = type_table.get_variant_instance(instr.variant_ty)
				if inst is not None and instr.ctor in inst.arms_by_name:
					arm = inst.arms_by_name[instr.ctor]
					args: list[str] = []
					for field_ty, arg in zip(arm.field_types, instr.args):
						if _is_string_tid(field_ty):
							if arg in move_only_values or _can_move_owned_once(arg):
								args.append(arg)
								_note_use(arg, consume=True)
							else:
								args.append(_ensure_owned(arg, owned_values, new_instrs, site_class=_ledger_reporter.SITE_CLASS_VALUE_POSITION_RETAIN))
								_note_use(arg, consume=True)
						else:
							args.append(arg)
					new_instrs.append(
						M.ConstructVariant(dest=instr.dest, variant_ty=instr.variant_ty, ctor=instr.ctor, args=args)
					)
					continue
			if isinstance(instr, M.ConstructIfaceValue):
				val = instr.value
				if _is_string_tid(instr.value_ty):
					if val in move_only_values or _can_move_owned_once(val):
						_note_use(val, consume=True)
					else:
						val = _ensure_owned(val, owned_values, new_instrs)
						_note_use(val, consume=True)
				new_instrs.append(
					M.ConstructIfaceValue(
						dest=instr.dest,
						iface_ty=instr.iface_ty,
						value=val,
						value_ty=instr.value_ty,
					)
				)
				continue

			if isinstance(instr, M.ConstructResultOk) and instr.value is not None:
				val = instr.value
				if _is_string_value(val):
					if val in move_only_values or _can_move_owned_once(val):
						_note_use(val, consume=True)
					else:
						val = _ensure_owned(val, owned_values, new_instrs)
						_note_use(val, consume=True)
				new_instrs.append(M.ConstructResultOk(dest=instr.dest, value=val))
				continue

			if isinstance(instr, M.ConstructError):
				event_fqn = instr.event_fqn
				if _is_string_value(event_fqn):
					if event_fqn in move_only_values or _can_move_owned_once(event_fqn):
						_note_use(event_fqn, consume=True)
					else:
						event_fqn = _ensure_owned(event_fqn, owned_values, new_instrs)
						_note_use(event_fqn, consume=True)
				new_instrs.append(
					M.ConstructError(
						dest=instr.dest,
						code=instr.code,
						event_fqn=event_fqn,
						payload=instr.payload,
						attr_key=instr.attr_key,
					)
				)
				continue

			if isinstance(instr, M.ExcSetParamsJson):
				# `drift_error_set_params_json` takes ownership of
				# `json_text` per ABI spec §2.3 — runtime releases the
				# prior params_json and stores the input.  ARC must
				# treat `json_text` as consumed (not a non-consuming
				# read).
				json_val = instr.json_text
				if _is_string_value(json_val):
					if json_val in move_only_values or _can_move_owned_once(json_val):
						_note_use(json_val, consume=True)
					else:
						json_val = _ensure_owned(json_val, owned_values, new_instrs)
						_note_use(json_val, consume=True)
				new_instrs.append(M.ExcSetParamsJson(error=instr.error, json_text=json_val))
				continue

			if isinstance(instr, M.ExcAppendContextFrame):
				# `drift_error_append_context_frame` takes ownership of
				# `frame_json` per ABI spec §2.3 — runtime splices it
				# into the merged context_json.  Same consume pattern
				# as ExcSetParamsJson.
				frame_val = instr.frame_json
				if _is_string_value(frame_val):
					if frame_val in move_only_values or _can_move_owned_once(frame_val):
						_note_use(frame_val, consume=True)
					else:
						frame_val = _ensure_owned(frame_val, owned_values, new_instrs)
						_note_use(frame_val, consume=True)
				new_instrs.append(M.ExcAppendContextFrame(error=instr.error, frame_json=frame_val))
				continue

			if isinstance(instr, M.ErrorRaise):
				new_instrs.append(instr)
				continue

			if isinstance(instr, M.Call):
				if drift_debug.enabled("ssa") and getattr(instr.fn_id, "module", None) == "main":
					import sys
					print(f"[drift:debug][arc] pre call fn={instr.fn_id} span={getattr(instr, 'span', None)}", file=sys.stderr)
				info = fn_infos.get(instr.fn_id)
				if info is not None and info.signature and info.signature.param_type_ids is not None:
					args: list[str] = []
					for ty_id, arg in zip(info.signature.param_type_ids, instr.args):
						if _param_is_ref(ty_id):
							args.append(arg)
							continue
						if _param_is_string(ty_id):
							if arg in move_only_values or _can_move_owned_once(arg):
								args.append(arg)
								_note_use(arg, consume=True)
							else:
								args.append(_ensure_owned(arg, owned_values, new_instrs, site_class=_ledger_reporter.SITE_CLASS_CALL_ARG_RETAIN))
								_note_use(arg, consume=True)
						else:
							args.append(arg)
					new_call = M.Call(dest=instr.dest, fn_id=instr.fn_id, args=args, can_throw=instr.can_throw)
					_copy_span(new_call, instr)
					if drift_debug.enabled("ssa") and getattr(instr.fn_id, "module", None) == "main":
						import sys
						print(f"[drift:debug][arc] new call fn={new_call.fn_id} span={getattr(new_call, 'span', None)}", file=sys.stderr)
					new_instrs.append(new_call)
					continue
				if drift_debug.enabled("ssa") and getattr(instr.fn_id, "module", None) == "main":
					import sys
					print(f"[drift:debug][arc] keep call fn={instr.fn_id} span={getattr(instr, 'span', None)}", file=sys.stderr)
				new_instrs.append(instr)
				continue

			if isinstance(instr, M.CallIndirect):
				args: list[str] = []
				for ty_id, arg in zip(instr.param_types, instr.args):
					if _param_is_ref(ty_id):
						args.append(arg)
						continue
					if _param_is_string(ty_id):
						if arg in move_only_values or _can_move_owned_once(arg):
							args.append(arg)
							_note_use(arg, consume=True)
						else:
							args.append(_ensure_owned(arg, owned_values, new_instrs, site_class=_ledger_reporter.SITE_CLASS_CALL_ARG_RETAIN))
							_note_use(arg, consume=True)
					else:
						args.append(arg)
				new_call = M.CallIndirect(
					dest=instr.dest,
					callee=instr.callee,
					args=args,
					param_types=instr.param_types,
					user_ret_type=instr.user_ret_type,
					can_throw=instr.can_throw,
				)
				_copy_span(new_call, instr)
				new_instrs.append(new_call)
				continue
			if isinstance(instr, M.CallIface):
				args: list[str] = []
				for ty_id, arg in zip(instr.param_types, instr.args):
					if _param_is_ref(ty_id):
						args.append(arg)
						continue
					if _param_is_string(ty_id):
						if arg in move_only_values or _can_move_owned_once(arg):
							args.append(arg)
							_note_use(arg, consume=True)
						else:
							args.append(_ensure_owned(arg, owned_values, new_instrs, site_class=_ledger_reporter.SITE_CLASS_CALL_ARG_RETAIN))
							_note_use(arg, consume=True)
					else:
						args.append(arg)
				new_call = M.CallIface(
					dest=instr.dest,
					iface=instr.iface,
					args=args,
					param_types=instr.param_types,
					user_ret_type=instr.user_ret_type,
					can_throw=instr.can_throw,
					slot_index=instr.slot_index,
				)
				_copy_span(new_call, instr)
				new_instrs.append(new_call)
				continue

			if isinstance(instr, M.DropValue) and _is_string_tid(instr.ty):
				new_instrs.append(instr)
				val = instr.value
				if _is_string_value(val) and not _is_local_name(val):
					_note_use(val, consume=True)
				continue
			if isinstance(instr, M.DropValue):
				src_local = load_local_src.get(instr.value)
				if src_local is not None and src_local in destructible_locals:
					explicitly_dropped_locals.add(src_local)

			new_instrs.append(instr)
			for val in _iter_used_values(instr):
				if _is_string_value(val) and not _is_local_name(val):
					_note_use(val, consume=False)

		if isinstance(block.terminator, M.Return):
			term = block.terminator
			val = term.value
			skip_cleanup_locals: Set[str] = set()
			skip_cleanup_locals |= moved_out_locals
			skip_cleanup_locals |= explicitly_dropped_locals
			# Phase 4 site-3 sub-step 2 — destructor-method `self`
			# skip is now ledger-authored.  The lattice transitions
			# `self` to MOVED_OUT at the end of every Return-
			# terminator block in a destructor method, so the
			# per-local ledger consultation below folds it into
			# `skip_cleanup_locals` without any site-local guard.
			# The legacy
			# `if is_destructor_method and "self" in func.params:
			#     skip_cleanup_locals.add("self")` line is retired.
			# Phase 4 sub-step 1 — returned-value source suppression
			# is now ledger-authored.  The legacy alias-walk and
			# `_collect_return_source_locals` composite walk that
			# used to populate `skip_cleanup_locals` here are
			# retired; the lattice's Return-as-move (incl.
			# composite constructors: ConstructStruct /
			# ConstructVariant / ConstructResultOk /
			# ConstructIfaceValue) transitions the source locals
			# to `MOVED_OUT`, and the per-local ledger consultation
			# a few lines below folds every `MUST_NOT_DROP`
			# verdict into `skip_cleanup_locals`.
			#
			# The alias walk is still needed for
			# `can_move_from_skipped_local` (string-ownership
			# transfer at the return value — a separate concern
			# from cleanup).
			can_move_from_skipped_local = False
			if val is not None:
				alias = val
				while True:
					moved = False
					for prev in reversed(new_instrs):
						if isinstance(prev, M.AssignSSA) and prev.dest == alias:
							alias = prev.src
							moved = True
							break
					if not moved:
						break
				# For STRING / ARRAY return-source locals, the legacy
				# alias-walk skip is preserved here.  The Phase 4
				# sub-step 1 ledger consultation below is limited to
				# `destructible_locals`; strings and arrays have
				# their own parallel ownership-tracking machinery
				# (`_release_all_locals` / `_drop_all_arrays`) and
				# folding them into `skip_cleanup_locals` via the
				# generic consultation breaks that machinery in
				# subtle ways (caught by the package-consumer
				# memcheck regression `test_pkg_map_literal_string_leak`).
				# Once strings/arrays move to ledger authority on a
				# future track, this can collapse into the
				# consultation.
				for prev in reversed(new_instrs):
					if isinstance(prev, M.LoadLocal) and prev.dest == alias:
						can_move_from_skipped_local = True
						if prev.local in string_locals or prev.local in array_locals:
							skip_cleanup_locals.add(prev.local)
						break
			# Phase 4 sub-step 1 — ledger consultation for returned-
			# value source suppression on DESTRUCTIBLES.  Every
			# destructible local whose `verdict_at` at the return
			# cursor returns `MUST_NOT_DROP` joins
			# `skip_cleanup_locals`.  The lattice's Return-as-move
			# (including composite constructors) transitions the
			# source local(s) of the returned value to `MOVED_OUT`
			# at the LoadLocal index, so the verdict is MustNotDrop
			# and the return-source is correctly skipped without any
			# site-local alias walk.
			#
			# Scope note (2026-04-23, post-sub-step-3 memcheck
			# diagnosis): the consultation is intentionally
			# restricted to `destructible_locals`.  Strings and
			# arrays have their own parallel ownership-tracking
			# machinery (`_release_all_locals` /
			# `_drop_all_arrays`, plus `moved_out_locals` /
			# `owned_values`) that pre-dates the ledger; folding
			# string/array MUST_NOT_DROP verdicts into
			# `skip_cleanup_locals` interferes with that machinery
			# in subtle ways (caught by the 0.27.145 memcheck
			# regression).  Strings/arrays remain on legacy
			# authority on this track; their swap to ledger
			# authority is separate work.
			# Destructor `self` and variant zero-tag widening
			# remain site-local (sub-steps 2 and 3).
			if _ledger is not None:
				_ledger_point = (block.name, len(block.instructions))
				# **Authority boundary** (post-2026-04-25 site-3 String
				# migration ATTEMPT + revert).  This consultation
				# covers DESTRUCTIBLES only.  Strings and Arrays remain
				# under `string_arc.py`'s post-rewrite alias-walk
				# authority (lines 1486-1491 above).
				#
				# **Why strings/arrays are NOT here**: `string_arc` is a
				# late-rewrite pass that synthesises `StringRetain` /
				# `StringRelease` (and the `_drop_all_arrays`
				# equivalent) AFTER the ledger is built.  For Strings
				# specifically, the return-value handler retains-wraps
				# the returned value (caller gets a fresh +1 via
				# StringRetain; function still owns the local's
				# original +1).  The lattice — built on the pre-rewrite
				# MIR — sees a plain `LoadLocal+Return` chain and
				# correctly transitions the local to MOVED_OUT
				# (Return-as-move).  But that MOVED_OUT verdict is the
				# WRONG predicate for "site 3 should skip the
				# function-exit release": post-rewrite the function
				# still holds its +1 and MUST release.  The alias-walk
				# operates on the post-rewrite MIR (`new_instrs`) and
				# matches only plain `LoadLocal` chain endpoints —
				# `StringRetain` is not a `LoadLocal`, so the
				# retain-wrapped pattern correctly does NOT skip.
				#
				# Arc<T> and other refcounted types whose
				# clone/destroy are MIR-first (visible to the ledger
				# at build time) flow through this consultation
				# correctly via `destructible_locals`.
				#
				# Architectural rule (Share Slice 1 / 0.31.14 close-out;
				# see `doc/history.md` 2026-04-26): ledger authority
				# is valid only for ownership effects visible in the
				# MIR snapshot used to build the ledger.  Any late
				# pass that creates/releases refcount stakes remains
				# its own authority unless we rebuild/extend the
				# ledger after that pass or move those effects
				# earlier.  `string_arc` is the canonical late-rewrite
				# authority for refcounted-builtin return-source
				# cleanup; future shared-owner types whose `Share`
				# impl synthesises late refcount mutations must
				# follow the same containment pattern.
				for _local in destructible_locals:
					if _local in skip_cleanup_locals:
						continue
					_local_ty = local_types.get(_local)
					if _local_ty is None:
						continue
					_needs_drop_axis = bool(
						_compute_drop_policy(type_table, _local_ty).needs_drop
					)
					_verdict = _ledger.verdict_at(
						_ledger_point,
						_local,
						needs_drop=_needs_drop_axis,
					)
					if _verdict is _DropVerdict.MUST_NOT_DROP:
						skip_cleanup_locals.add(_local)
			if _audit is not None:
				# Return-boundary emissions anchor at the established
				# site-3 convention point.
				_audit_point[0] = (block.name, len(block.instructions))
			if val is not None and (_is_string_value(val) or _can_move_creator_return(val)):
				if val in move_only_values or _can_move_owned_once(val) or _can_move_creator_return(val) or can_move_from_skipped_local:
					_note_use(val, consume=True)
				else:
					val = _ensure_owned(val, owned_values, new_instrs, site_class=_ledger_reporter.SITE_CLASS_RETURN_RETAIN_SITE3)
					_note_use(val, consume=True)
			# RELEASE ELISION (2026-07-11 slice; B-arch-1 prerequisite):
			# String locals whose return-boundary ledger verdict is
			# MUST_NOT_DROP are elided from the scope-exit release sweep.
			# This is the strings analog of the Phase 4 destructible
			# consultation above, unblocked by B-arch-1: with every
			# copy-stake ledger-visible (C2 = 0), the 0.27.145 failure
			# class — a WRONG MOVED_OUT verdict on a retain-wrapped
			# return source — is structurally gone, and every
			# MUST_NOT_DROP string slot at this boundary holds ZEROED
			# bytes at runtime (UNINIT: never written on the path;
			# MOVED_OUT: the MoveOut expansion zero-stores; TOMBSTONED:
			# `_emit_tombstone_value` for String IS `_emit_zero_value`,
			# proven 2026-07-11) — the elided release was a null-safe
			# no-op quad (Load+Zero+Store+Release).
			#
			# Guardrails (review-pinned):
			# - DropPolicy-backed needs_drop axis (String is
			#   needs_drop=True despite structural Copy — cheap-copy,
			#   NOT drop-free; verified against the Copy-shortcut
			#   hazard before landing).
			# - PATH_DEPENDENT keeps today's unconditional null-safe
			#   release (no string drop-flag machinery in this slice).
			# - No attached ledger → legacy behavior (loop guarded).
			# - Arrays, site 4/drop_before_overwrite, and C3
			#   flag-guarded cleanup MoveOuts untouched.
			if _ledger is not None:
				_string_needs_drop = bool(
					_compute_drop_policy(type_table, string_ty).needs_drop
				)
				_ledger_point_str = (block.name, len(block.instructions))
				for _sl in sorted(string_locals):
					if _sl in skip_cleanup_locals:
						continue
					_sv = _ledger.verdict_at(
						_ledger_point_str,
						_sl,
						needs_drop=_string_needs_drop,
					)
					if _sv is _DropVerdict.MUST_NOT_DROP:
						skip_cleanup_locals.add(_sl)
			# ARRAY RELEASE ELISION (2026-07-13 slice; Slice 3
			# measurement GO — SLICE3-ARRAY-MEASUREMENT.md): Array
			# locals whose return-boundary ledger verdict is
			# MUST_NOT_DROP are elided from the sweep.  The measurement
			# proved the sweep is a legacy backstop over dead storage:
			# 156,308 swept drops corpus-wide with ZERO live and ZERO
			# must_drop — 141,391 uninit + 10,297 moved_out (both
			# provably nothing-owned; MOVED_OUT storage is zero-backed
			# by the MoveOut expansion) + 4,620 maybe_uninit.
			# PATH_DEPENDENT keeps today's unconditional null-safe drop
			# (first-slice discipline, exactly mirroring the String
			# elision above).  Live arrays never reach the sweep
			# (cleanup_authoring owns their drops; return sources are
			# alias-walk skipped) — and if one ever did, MUST_DROP is
			# not elided.  The 0.27.145-class hazard does not apply to
			# arrays: there is no late retain-wrap at the array return
			# boundary (return-by-move only), so the lattice's
			# MOVED_OUT verdict is not invalidated post-rewrite (pinned
			# by the heap-Array<String> memcheck rows in
			# lang/tests/memcheck/test_array_release_elision.py).
			# Strings untouched — the separate fold above.
			if _ledger is not None:
				_ledger_point_arr = (block.name, len(block.instructions))
				for _al in sorted(array_locals):
					if _al in skip_cleanup_locals:
						continue
					_al_ty = local_types.get(_al)
					if _al_ty is None:
						continue
					try:
						_al_nd = bool(_compute_drop_policy(type_table, _al_ty).needs_drop)
					except Exception:
						# Unknown policy → conservative: keep the drop.
						continue
					_av = _ledger.verdict_at(
						_ledger_point_arr,
						_al,
						needs_drop=_al_nd,
					)
					if _av is _DropVerdict.MUST_NOT_DROP:
						skip_cleanup_locals.add(_al)
			if _audit is not None:
				# Slice 3 measurement (report-only): record each Array
				# local the return-boundary sweep is about to drop, with
				# its DropPolicy needs_drop axis — the reporter derives
				# the raw-state/verdict mix that sizes the Array
				# release-elision win.  Same boundary-point convention
				# as note_return_boundary.  The drop-before-overwrite
				# array drop (StoreLocal path) is deliberately OUT of
				# this measurement's scope.
				_ad_point = (block.name, len(block.instructions))
				for _adl in sorted(array_locals):
					if _adl in skip_cleanup_locals:
						continue
					_ad_ty = local_types.get(_adl)
					try:
						_ad_nd = bool(_compute_drop_policy(type_table, _ad_ty).needs_drop) if _ad_ty is not None else False
					except Exception:
						_ad_nd = False
					_audit.note_array_drop(_adl, point=_ad_point, needs_drop=_ad_nd)
			_drop_all_arrays(new_instrs, skip_locals=skip_cleanup_locals)
			_release_all_locals(new_instrs, skip_locals=skip_cleanup_locals)
			if _audit is not None:
				_audit.note_return_boundary(
					(block.name, len(block.instructions)),
					string_locals=string_locals,
					skipped=(skip_cleanup_locals & string_locals),
				)
			initialized_at_return = assigned_in.get(block.name, set()) | store_defs.get(block.name, set()) | store_defs.get(func.entry, set())
			# Phase 4 site-3 sub-step 3 — variant zero-tag widening,
			# now ledger-driven.  The legacy widening used site-3
			# dataflow (`assigned_out` / `store_defs` / `moved_out`)
			# to detect "assigned on some predecessor paths but not
			# all"; the lattice answers the same question via
			# `MAYBE_UNINIT → PathDependent`.  Site 3 keeps one
			# explicit policy axis: `variant_zero_tag_drop_safe(ty,
			# table)` — variant types whose tag-0 destructor is a
			# no-op.  Live paths get their drop; uninit paths drop
			# the PHI-zero storage harmlessly.
			# Carrier (0.27.145 fix): pinned by
			# `lang/tests/codegen/e2e/scope_drop_conditional_move/`
			# + `lang/tests/memcheck/test_scope_drop_conditional_move.py`.
			if _ledger is not None:
				for _local in destructible_locals:
					if _local in initialized_at_return or _local in skip_cleanup_locals:
						continue
					_local_ty = local_types.get(_local)
					if _local_ty is None or not variant_zero_tag_drop_safe(_local_ty, type_table):
						continue
					_verdict = _ledger.verdict_at(
						_ledger_point,
						_local,
						needs_drop=True,
					)
					if _verdict is _DropVerdict.PATH_DEPENDENT:
						initialized_at_return.add(_local)
			# Phase 3B step 2 — `string_arc_return` swap (option 2:
			# site-3 skips locals managed by Phase 3C drop-flag
			# plumbing).  3C is the sole authority on scope-exit drops
			# for flagged locals: it has already inserted a
			# flag-guarded `MoveOut+DropValue` block reachable via the
			# original Return block's `IfTerminator(flag)`.  If site 3
			# also emitted a drop here, the flagged local would be
			# double-dropped on the path through 3C's drop block.
			# Filter flagged locals out of site-3's cleanup universe by
			# adding them to `skip_cleanup_locals`.
			#
			# Build-timing note (see `work/ownership-ledger/3b-invariants.md`):
			# the ledger we consulted in observe mode was the PRE-
			# `drop_flags` ledger, but the skip itself does not depend
			# on the ledger — it depends on the post-`drop_flags`
			# `func.locals` (where the `__drop_flag_<L>` markers
			# appear).  Detection via `_is_flag_managed`, which encodes
			# the flag-naming convention behind one helper rather than
			# scattering string matches.
			_flag_managed_at_return: Set[str] = {
				_dl for _dl in destructible_locals if _is_flag_managed(func, _dl)
			}
			skip_cleanup_locals |= _flag_managed_at_return
			if _ledger is not None and drift_debug.enabled("ownership_ledger"):
				# Site 3 observation: per-destructible-local verdict
				# at the return boundary.  Program point is
				# (block, len(original_instructions)) — the index a
				# hypothetical drop would land at if appended after the
				# block's last original instruction; the ledger reads
				# pre-state (post-state of that last instruction),
				# which is what `_drop_all_destructibles` is implicitly
				# deciding against.  Locals that 3C owns get a distinct
				# `REASON_DROP_FLAG_OWNED` record so observe triage can
				# see the responsibility split (without it, the missing
				# site-3 emission would surface as a bucket-5/6
				# regression in the next observe re-run).
				_ledger_point = (block.name, len(block.instructions))
				for _dl in sorted(destructible_locals):
					if _dl in _flag_managed_at_return:
						_ledger_reporter.check(
							_ledger,
							fn_name=func.name,
							site=_ledger_events.SITE_STRING_ARC_RETURN,
							point=_ledger_point,
							local=_dl,
							site_verdict=_ledger_events.VERDICT_MUST_NOT_DROP,
							site_reason=_ledger_events.REASON_DROP_FLAG_OWNED,
							needs_drop=_ledger_needs_drop,
							emit=_ledger_reporter.stderr_emit,
						)
						continue
					if _dl in skip_cleanup_locals:
						continue
					_dl_in_init = _dl in initialized_at_return
					_dl_verdict = (
						_ledger_events.VERDICT_MUST_DROP
						if _dl_in_init
						else _ledger_events.VERDICT_MUST_NOT_DROP
					)
					_dl_reason = (
						_ledger_events.REASON_NEEDS_DROP
						if _dl_in_init
						else _ledger_events.REASON_NOT_DROP_NEEDING
					)
					_ledger_reporter.check(
						_ledger,
						fn_name=func.name,
						site=_ledger_events.SITE_STRING_ARC_RETURN,
						point=_ledger_point,
						local=_dl,
						site_verdict=_dl_verdict,
						site_reason=_dl_reason,
						needs_drop=_ledger_needs_drop,
						emit=_ledger_reporter.stderr_emit,
					)
			_drop_all_destructibles(new_instrs, skip_locals=skip_cleanup_locals, only_locals=initialized_at_return)
			new_term = M.Return(value=val)
			if hasattr(term, "span"):
				setattr(new_term, "span", getattr(term, "span"))
			block.terminator = new_term
			mark_ledger_dirty(func, "string_arc.rewrite_return_terminator")
		elif block.terminator is not None:
			for val in _iter_term_used(block.terminator):
				if _is_string_value(val) and not _is_local_name(val):
					_note_use(val, consume=False)

		block.instructions = new_instrs
		mark_ledger_dirty(func, "string_arc.rewrite_block")

	if _audit is not None:
		# L_post: a fresh ledger over the pass OUTPUT.  Built directly
		# (never attached) so the driver's ledger-cache sequencing and
		# dirty-bit state are exactly as in the non-audit path.
		from .ownership_ledger import build_ledger as _build_ledger
		try:
			_l_post = _build_ledger(func, drop_policy=lambda tid: _compute_drop_policy(type_table, tid))
		except Exception:
			# Audit must never break a compile (observational contract),
			# but a missing L_post must never pass silently either:
			# finalize hard-counts it as post_ledger_build_failed and
			# force-emits the per-fn record; the corpus gate fails on
			# any nonzero count (review finding, B-arch-0 acceptance).
			_l_post = None
		_audit.finalize(
			l_pre=_ledger,
			l_post=_l_post,
			needs_drop=_ledger_needs_drop,
			# C3 agree-class ladder inputs (Slice 2 Part 2): the func for
			# STRUCTURAL flag-guard verification (terminators + pred
			# LoadLocals survive this pass's rewrites) and the same
			# zero-safety predicate cleanup_authoring used to author the
			# unguarded cleanup arm.
			func=func,
			zero_safe_ty=lambda _tid: variant_zero_tag_drop_safe(_tid, type_table),
		)

	return func


__all__ = ["insert_string_arc"]
