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
`store_value_retain` (StoreLocal/StoreRef/ArrayIndexStore values)
remains a later slice.

DECISION CRITERION — mirror `string_arc`, copy only where it would
RETAIN: string_arc moves an arg iff it is an owned single-use value
(creator results: ConstString/StringConcat/StringFrom*/StringRetain/
CopyValue dests; MoveOut dests are move-only). It retains iff the arg's
producer chain (through AssignSSA) ends at a plain `LoadLocal` — a
borrowed view of a still-owned local. We materialize exactly that case.
Args with cross-block producers or non-load producers are LEFT ALONE
(string_arc keeps handling them; they surface as explainable C2
residuals in the audit rather than risk a behavior change here).
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
	"""Insert `CopyValue` stakes for by-value String call args whose
	producer chain ends at a plain LoadLocal. Returns True if the MIR
	was mutated (and marks the ledger dirty per the cache contract)."""
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
	)

	changed = False
	stake_counter = 0
	for block in func.blocks.values():
		# Within-block producer map for the AssignSSA→LoadLocal chain
		# resolution. Cross-block SSA values simply have no entry and
		# are left alone (conservative residual).
		producers: Dict[str, M.MInstr] = {}
		new_instrs: list[M.MInstr] = []

		def _resolve_load(arg: str) -> Optional[M.LoadLocal]:
			"""The producer-chain criterion shared by call args and
			value positions: follow AssignSSA to the producer; a stake
			is materialized only for a plain LoadLocal of a
			semantically-String local (mirrors string_arc's
			move-vs-retain decision — creators/MoveOut dests move)."""
			alias = arg
			seen = 0
			while seen < 64:
				prod = producers.get(alias)
				if isinstance(prod, M.AssignSSA):
					alias = prod.src
					seen += 1
					continue
				break
			prod = producers.get(alias)
			if isinstance(prod, M.LoadLocal) and _local_is_string(prod.local):
				return prod
			return None

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
			producers[tmp] = copy
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
						or _resolve_load(arg) is None
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
							if isinstance(op, str) and _resolve_load(op) is not None:
								ops[i] = _stake(op, instr)
								pos_changed = True
						if pos_changed:
							replaced = _dc_replace(instr, **{attr: ops})
					else:
						op = getattr(instr, attr, None)
						if isinstance(op, str) and _resolve_load(op) is not None:
							replaced = _dc_replace(instr, **{attr: _stake(op, instr)})
					break
			if replaced is not None:
				if hasattr(instr, "span"):
					setattr(replaced, "span", getattr(instr, "span"))
				new_instrs.append(replaced)
				dest = getattr(replaced, "dest", None)
				if isinstance(dest, str):
					producers[dest] = replaced
				continue
			dest = getattr(instr, "dest", None)
			if isinstance(dest, str):
				producers[dest] = instr
			new_instrs.append(instr)
		block.instructions = new_instrs
	if changed:
		mark_ledger_dirty(func, "string_stakes.materialize_stakes")
	return changed


__all__ = ["materialize_call_arg_stakes"]
