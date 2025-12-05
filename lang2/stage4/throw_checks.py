# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage 4 helper: consume throw summaries and enforce basic can-throw invariants.

Pipeline placement:
  stage0 (AST) → stage1 (HIR) → stage2 (MIR) → stage3 (pre-analysis/throw summary)
  → stage4 (SSA + invariants) → LLVM/obj

This module combines stage3 ThrowSummary facts with type-level intent
(`declared_can_throw`) and performs simple checks. It keeps invariants out of
lowering/SSA so those passes stay structural.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Set

from lang2.stage3 import ThrowSummary
from lang2.stage2 import MirFunc, Return, ConstructResultOk, ConstructResultErr


@dataclass
class FuncThrowInfo:
	"""
	Aggregated throw facts for a function, combining summary + declaration.

	constructs_error: does this function contain any ConstructError at all?
	exception_types: DV names inferred from event codes via code_to_exc
	may_fail_sites: raw copy of ThrowSummary.may_fail_sites
	declared_can_throw: does the signature/annotation say this fn returns FnResult or throws?
	"""

	constructs_error: bool
	exception_types: Set[str]
	may_fail_sites: Set[tuple[str, int]]
	declared_can_throw: bool


def build_func_throw_info(
	summaries: Dict[str, ThrowSummary],
	declared_can_throw: Dict[str, bool],
) -> Dict[str, FuncThrowInfo]:
	"""
	Combine ThrowSummary facts with declaration intent.

	`summaries`: output of ThrowSummaryBuilder (per-function throw facts)
	`declared_can_throw`: function name -> whether its signature allows throwing (FnResult/throws)
	"""
	out: Dict[str, FuncThrowInfo] = {}
	for fname, summary in summaries.items():
		out[fname] = FuncThrowInfo(
			constructs_error=summary.constructs_error,
			exception_types=set(summary.exception_types),
			may_fail_sites=set(summary.may_fail_sites),
			declared_can_throw=declared_can_throw.get(fname, False),
		)
	return out


def enforce_can_throw_invariants(func_infos: Dict[str, FuncThrowInfo]) -> None:
	"""
	Basic invariants:
	  - If a function is not declared can-throw, it must not construct errors.
	More invariants (e.g., Returns carry FnResult) can be layered on later.
	"""
	for fname, info in func_infos.items():
		if info.constructs_error and not info.declared_can_throw:
			raise RuntimeError(f"function {fname} constructs an Error but is not declared can-throw")


def enforce_return_shape_for_can_throw(
	func_infos: Dict[str, FuncThrowInfo],
	funcs: Dict[str, MirFunc],
) -> None:
	"""
	Additional invariant:
	  - If a function is declared can-throw (returns FnResult/throws), every Return
	    terminator must carry a value (no bare `return;`).

	This is a lightweight check; a richer type-aware check can later ensure that the
	returned value is actually a FnResult constructed via ConstructResultOk/Err.
	"""
	for fname, info in func_infos.items():
		if not info.declared_can_throw:
			continue
		fn = funcs.get(fname)
		if fn is None:
			continue
		for block in fn.blocks.values():
			term = block.terminator
			if isinstance(term, Return) and term.value is None:
				raise RuntimeError(
					f"function {fname} is declared can-throw but has a bare return in block {block.name}"
				)


def enforce_fnresult_returns_for_can_throw(
	func_infos: Dict[str, FuncThrowInfo],
	funcs: Dict[str, MirFunc],
) -> None:
	"""
	Stronger return-shape invariant for can-throw functions:
	  - Every Return value in a can-throw function must come from a ConstructResultOk/Err.

	This is a conservative structural check (not type-driven): it scans the MIR
	instructions for ConstructResultOk/Err with dest matching the returned ValueId.
	If none is found anywhere in the function, we flag it. This keeps us honest
	that can-throw functions actually produce a FnResult on all return paths.
	"""
	for fname, info in func_infos.items():
		if not info.declared_can_throw:
			continue
		fn = funcs.get(fname)
		if fn is None:
			continue
		for block in fn.blocks.values():
			term = block.terminator
			if not isinstance(term, Return) or term.value is None:
				continue

			return_val = term.value
			found = False
			for b in fn.blocks.values():
				for instr in b.instructions:
					if isinstance(instr, (ConstructResultOk, ConstructResultErr)):
						if getattr(instr, "dest", None) == return_val:
							found = True
							break
				if found:
					break
			if not found:
				raise RuntimeError(
					f"function {fname} is declared can-throw but return in block {block.name} "
					f"does not return a FnResult (no ConstructResultOk/Err defines {return_val})"
				)
