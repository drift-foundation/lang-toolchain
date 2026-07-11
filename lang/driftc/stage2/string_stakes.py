# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""B-arch-1a: materialize by-value String CALL-ARGUMENT copy stakes as
ledger-visible MIR, before the ledger build that feeds `string_arc`.

Problem (B-arch-0 inventory, C2 `call_arg_retain`, 58,680 corpus
occurrences): passing a String local by value keeps the caller's copy
alive, so a +1 stake for the callee must exist — but today `string_arc`
invents it LATE (`_ensure_owned` → `StringRetain`) on MIR the ownership
ledger never re-reads. The stake is invisible: the ledger cannot
distinguish "arg copied, local still owned" from "arg moved", which is
the root of the C2 inventory and (via return-reaching composites) the C4
allowlist.

This pass rewrites, per call site and per by-value String parameter:

    %t = LoadLocal(s)            %t = LoadLocal(s)
    Call(..., %t, ...)     →     %stake<n> = CopyValue(%t, String)
                                 Call(..., %stake<n>, ...)

`CopyValue` is the canonical MIR-visible copy event: the ledger models
the source local as still-owned (copy, not consume), and codegen lowers
String CopyValue to `drift_string_retain` — the SAME runtime op
string_arc's late retain produced, so the refcount sequence is
byte-identical. Downstream, `string_arc` classifies the CopyValue dest
as an owned single-use value and MOVES it into the call
(`_can_move_owned_once`), emitting no `call_arg_retain` — the stake now
predates the ledger snapshot instead of trailing it.

SCOPE: B-arch-1a covered call arguments (`Call`, `CallIndirect`,
`CallIface` — shared argument-boundary logic: positional
`param_types`/signature ids, by-ref params skipped, by-value String
params candidates). B-arch-1b extends the SAME criterion to VALUE
POSITIONS: ctor fields (`ConstructStruct`/`ConstructVariant`), array
literals/element writes (`ArrayLit`/`ArrayElemInit`/
`ArrayElemInitUnchecked`/`ArrayElemAssign`), interface boxing
(`ConstructIfaceValue`), Result payloads (`ConstructResultOk`),
exception-ABI strings (`ConstructError.event_fqn`,
`ExcSetParamsJson.json_text`, `ExcAppendContextFrame.frame_json`).
For value positions no signature is needed: the candidate test —
producer chain ends at a `LoadLocal` of a semantically-String local —
already implies the operand is a String in a by-value position.
B-arch-1d completes the inventory: StoreLocal/StoreRef/
ArrayIndexStore VALUE operands are staked positions too, and the view
set gains the `ResultOk` Ok-payload projection (NOT the
`ConstructResultOk` constructor).  `ArrayIndexLoad`/
`ArrayIndexLoadUnchecked` were briefly classified as views in 1d and
REVERTED: their codegen lowering retains the extracted element, so
the dest is owned at extraction (VariantGetField's sibling) — see the
terminal-producer note in `_is_string_value_view`.

B-arch-1c extends the PRODUCER side: the residual after 1a/1b was not
surface field syntax (user `self.field`/`obj.name` copies materialize
upstream of string_arc — checkpoint-probed) but MIR FIELD/VIEW
producers, dominated by `StructGetField` inside compiler-synthesized
`Throw::throw_self` envelope builders and stdlib cursor internals.
The candidate rule generalizes from "producer is LoadLocal of a String
local" to "producer is a PROVEN semantically-String VALUE VIEW that
string_arc would retain today": LoadLocal, StructGetField with a
String field_ty, LoadRef with a String inner_ty, LoadField whose dest
is typed String, and bare storage operands (params/locals referenced
directly as SSA ids). `VariantGetField` is NOT in the set — its dest
is already owned (codegen retains at extraction; the ledger marks the
field moved-out), so string_arc moves it. Producer
identity is resolved FN-WIDE (sound under SSA single-assignment);
AddrOfField/AddrOfArrayElem and other address producers are NOT
staked — an address is not a String value — and remain itemized
residuals, per the 1c review guardrails.

DECISION CRITERION — mirror `string_arc`, copy only where it would
RETAIN: string_arc moves an operand iff it is an owned single-use
value (creator results: ConstString/StringConcat/StringFrom*/
StringRetain/CopyValue dests; MoveOut dests are move-only;
VariantGetField dests are owned at extraction). It retains iff the
operand's producer chain (through AssignSSA, resolved FN-WIDE — the
pre-1c per-block map made cross-block operands silent residuals) ends
at a PROVEN semantically-String VALUE VIEW: a plain `LoadLocal`, a
`StructGetField`/`LoadField` field read, a `LoadRef` through a String
reference, or a bare storage operand — a borrowed view of still-owned
storage. We materialize exactly those cases (`_is_string_value_view`).
Operands whose producer is anything else — address producers, unknown
shapes — are left to string_arc and surface as itemized C2 residuals
in the audit rather than risk a behavior change here.
"""

from __future__ import annotations

from dataclasses import replace as _dc_replace
from typing import Dict, Mapping, Optional

from lang.driftc.checker import FnInfo
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeId, TypeKind, TypeTable

from . import mir_nodes as M
from .ledger_cache import mark_ledger_dirty


def materialize_call_arg_stakes(
	func: M.MirFunc,
	*,
	type_table: TypeTable,
	fn_infos: Mapping[FunctionId, FnInfo],
) -> bool:
	"""Insert `CopyValue` stakes for String operands at call-argument
	and value positions whose producer chain resolves (fn-wide, through
	AssignSSA) to a PROVEN semantically-String VALUE VIEW — see
	`_is_string_value_view`: LoadLocal, StructGetField, LoadRef,
	LoadField, bare storage operands; NOT VariantGetField (already
	owned) and never address producers. Returns True if the MIR was
	mutated (and marks the ledger dirty per the cache contract)."""
	string_ty = type_table.ensure_string()

	def _param_is_string(ty_id: TypeId) -> bool:
		# MIRROR string_arc's semantic predicate (`_param_is_string`):
		# kind/name check, NOT tid equality — package-loaded or
		# non-canonical String TypeIds must materialize too, or they
		# linger as call_arg_retain residuals for the wrong reason
		# (review finding, B-arch-1a round 1).
		try:
			td = type_table.get(ty_id)
		except Exception:
			return False
		return td.kind is TypeKind.SCALAR and td.name == "String"

	def _param_is_ref(ty_id: TypeId) -> bool:
		try:
			return type_table.get(ty_id).kind is TypeKind.REF
		except Exception:
			return False

	def _call_param_types(instr: M.MInstr) -> Optional[list]:
		if isinstance(instr, M.Call):
			info = fn_infos.get(instr.fn_id)
			if info is None or not info.signature:
				return None
			return info.signature.param_type_ids
		if isinstance(instr, (M.CallIndirect, M.CallIface)):
			return instr.param_types
		return None

	local_types: Dict[str, TypeId] = func.local_types

	def _local_is_string(name: str) -> bool:
		ty = local_types.get(name)
		return ty is not None and _param_is_string(ty)

	# B-arch-1c: FN-WIDE producer map. Sound under SSA — every value id
	# has at most one defining instruction, so resolution does not
	# depend on block order. (The pre-1c per-block map made cross-block
	# operands silent residuals; see the revised comment at the walk.)
	fn_producers: Dict[str, M.MInstr] = {}
	for _blk in func.blocks.values():
		for _ins in _blk.instructions:
			_d = getattr(_ins, "dest", None)
			if isinstance(_d, str):
				fn_producers[_d] = _ins
	storage_names = set(func.params) | set(func.locals)

	def _is_string_value_view(prod: "M.MInstr | None", alias: str) -> bool:
		"""True iff `alias` (with defining instr `prod`, possibly None)
		is a PROVEN semantically-String VALUE VIEW — a borrowed alias
		string_arc would retain at a staked position today. Address
		producers (AddrOfField/AddrOfArrayElem/...) never qualify: an
		address is not a String value (1c guardrail); unknown shapes
		stay residual and are itemized by the audit."""
		if prod is None:
			# Bare storage operand: a param/local referenced directly
			# as an SSA id (no defining instruction fn-wide). This is
			# the LoadLocal-equivalent direct storage read.
			return alias in storage_names and _local_is_string(alias)
		if isinstance(prod, M.LoadLocal):
			return _local_is_string(prod.local)
		if isinstance(prod, M.StructGetField):
			fty = getattr(prod, "field_ty", None)
			return fty is not None and _param_is_string(fty)
		# `VariantGetField` is deliberately NOT a view (review finding,
		# 2026-07-10): its String dest is ALREADY OWNED — codegen lowers
		# it as load + drift_string_retain (string_arc documents the
		# match-binder extra-retain leak that treating it as borrowed
		# caused), and the ledger marks the variant field moved-out
		# (by-value extraction). string_arc MOVES it; staking it would
		# add a duplicate retain. It stays terminal with the other
		# owned/fresh producers.
		if isinstance(prod, M.LoadRef):
			ity = getattr(prod, "inner_ty", None)
			return ity is not None and _param_is_string(ity)
		if isinstance(prod, M.LoadField):
			dty = local_types.get(prod.dest)
			return dty is not None and _param_is_string(dty)
		# B-arch-1d view kind (ResultOk below).  ArrayIndexLoad /
		# ArrayIndexLoadUnchecked are NOT views — they stay TERMINAL,
		# the exact sibling of VariantGetField above: the codegen
		# lowering already retains the extracted element
		# (`_lower_array_index_load[_unchecked]` calls
		# `_emit_copy_value` -> `drift_string_retain`), so the MIR dest
		# is OWNED at extraction and its single consumer moves that +1.
		# Staking it copies from the dest and orphans the codegen +1 —
		# one leaked ref per element load (caught by the heap-string
		# e2e fixtures `main_argv_content` /
		# `array_extend_borrowed_source_string_no_uaf`; static-literal
		# elements mask the imbalance because retain/release are no-ops
		# on DRIFT_STRING_FLAG_STATIC).  `ArrayElemTake` — the
		# move-out-of-storage transfer — is likewise terminal.
		if isinstance(prod, M.ResultOk):
			# Ok-payload PROJECTION (`dest = result.ok`) — a read of the
			# FnResult temp's payload, NOT the `ConstructResultOk`
			# constructor (which stays terminal as a fresh producer).
			# The copy leaves the Result temp's ownership untouched, so
			# the 0.33.46 Ok-payload-holder machinery still releases the
			# payload exactly once (pinned Ok+Err paths in the 1d tests).
			dty = local_types.get(prod.dest)
			return dty is not None and _param_is_string(dty)
		return False

	# B-arch-1b value positions: (node type, operand attr, is_list).
	_VALUE_POSITIONS: tuple = (
		(M.ConstructStruct, "args", True),
		(M.ConstructVariant, "args", True),
		(M.ArrayLit, "elements", True),
		(M.ArrayElemInit, "value", False),
		(M.ArrayElemInitUnchecked, "value", False),
		(M.ArrayElemAssign, "value", False),
		(M.ConstructIfaceValue, "value", False),
		(M.ConstructResultOk, "value", False),
		(M.ConstructError, "event_fqn", False),
		(M.ExcSetParamsJson, "json_text", False),
		(M.ExcAppendContextFrame, "frame_json", False),
		# B-arch-1d store positions. The stake (CopyValue) lands BEFORE
		# the store instruction — and therefore before string_arc's
		# old-destination release expansion — which is the strictly
		# safer order (the +1 is taken while the source is provably
		# alive; today's retain-after-release order has a latent
		# self-aliased-store window). The destination-side release /
		# site-4 drop_before_overwrite logic is untouched: this pass
		# only rewrites the SOURCE operand. For an out-of-bounds
		# ArrayIndexStore the pre-store copy cannot leak under any
		# cleanup contract: `drift_bounds_check_fail` is
		# `__attribute__((noreturn))` and by its own documented
		# contract "ends in abort(); cleanup never fires on a noreturn
		# frame" (array_runtime.c) — pinned by the 1d OOB test row.
		(M.StoreLocal, "value", False),
		(M.StoreRef, "value", False),
		(M.ArrayIndexStore, "value", False),
	)

	changed = False
	stake_counter = 0
	for block in func.blocks.values():
		# Producer resolution is FN-WIDE (B-arch-1c) via `fn_producers`;
		# newly inserted CopyValues register there too so later operands
		# resolve them as owned (terminal) producers.
		new_instrs: list[M.MInstr] = []

		def _resolve_load(arg: str) -> bool:
			"""The producer-chain criterion shared by call args and
			value positions: follow AssignSSA fn-wide to the producer;
			a stake is materialized only for a PROVEN String value view
			(see `_is_string_value_view`) — mirroring string_arc's
			move-vs-retain decision: creators/MoveOut/call-result dests
			move and stay terminal; views retain and are staked."""
			alias = arg
			seen = 0
			while seen < 64:
				prod = fn_producers.get(alias)
				if isinstance(prod, M.AssignSSA):
					alias = prod.src
					seen += 1
					continue
				break
			return _is_string_value_view(fn_producers.get(alias), alias)

		def _stake(arg: str, anchor: M.MInstr) -> str:
			nonlocal stake_counter, changed
			stake_counter += 1
			tmp = f".stake{stake_counter}"
			while tmp in local_types:
				stake_counter += 1
				tmp = f".stake{stake_counter}"
			copy = M.CopyValue(dest=tmp, value=arg, ty=string_ty)
			if hasattr(anchor, "span"):
				setattr(copy, "span", getattr(anchor, "span"))
			new_instrs.append(copy)
			fn_producers[tmp] = copy
			local_types[tmp] = string_ty
			changed = True
			return tmp

		for instr in block.instructions:
			replaced = None
			param_tys = _call_param_types(instr)
			if param_tys is not None and getattr(instr, "args", None):
				new_args: list[str] = []
				call_changed = False
				for ty_id, arg in zip(param_tys, instr.args):
					if (
						not _param_is_string(ty_id)
						or _param_is_ref(ty_id)
						or not isinstance(arg, str)
						or not _resolve_load(arg)
					):
						new_args.append(arg)
						continue
					new_args.append(_stake(arg, instr))
					call_changed = True
				if call_changed:
					# Extra args beyond the signature (should not
					# happen; zip is defensive) are preserved verbatim.
					if len(instr.args) > len(new_args):
						new_args.extend(instr.args[len(new_args):])
					replaced = _dc_replace(instr, args=new_args)
			else:
				for node_ty, attr, is_list in _VALUE_POSITIONS:
					if not isinstance(instr, node_ty):
						continue
					if is_list:
						ops = list(getattr(instr, attr) or [])
						pos_changed = False
						for i, op in enumerate(ops):
							if isinstance(op, str) and _resolve_load(op):
								ops[i] = _stake(op, instr)
								pos_changed = True
						if pos_changed:
							replaced = _dc_replace(instr, **{attr: ops})
					else:
						op = getattr(instr, attr, None)
						if isinstance(op, str) and _resolve_load(op):
							replaced = _dc_replace(instr, **{attr: _stake(op, instr)})
					break
			if replaced is not None:
				if hasattr(instr, "span"):
					setattr(replaced, "span", getattr(instr, "span"))
				new_instrs.append(replaced)
				dest = getattr(replaced, "dest", None)
				if isinstance(dest, str):
					fn_producers[dest] = replaced
				continue
			dest = getattr(instr, "dest", None)
			if isinstance(dest, str):
				fn_producers[dest] = instr
			new_instrs.append(instr)
		block.instructions = new_instrs
	if changed:
		mark_ledger_dirty(func, "string_stakes.materialize_stakes")
	return changed


__all__ = ["materialize_call_arg_stakes"]
