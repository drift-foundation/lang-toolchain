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
from lang2.types_protocol import TypeEnv


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

	This is a conservative structural check (not type-driven and intentionally
	over-strict for now): it scans the MIR instructions for ConstructResultOk/Err
	with dest matching the returned ValueId. If none is found anywhere in the
	function, we flag it. This keeps us honest that can-throw functions actually
	produce a FnResult on all return paths.

	Limitations (by design for now):
	  - Returning a FnResult parameter or local that aliases a FnResult will fail
	    this check until a type-aware pass replaces it.
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


def enforce_fnresult_returns_typeaware(
	func_infos: Dict[str, FuncThrowInfo],
	ssa_funcs: Dict[str, "SsaFunc"],  # type: ignore[name-defined]
	type_env: TypeEnv,
) -> None:
	"""
	Minimal type-aware FnResult return check.

	This is the type-aware counterpart to the structural
	`enforce_fnresult_returns_for_can_throw`: when SSA + TypeEnv are supplied we
	assert that every returned SSA value in a can-throw function has type
	`FnResult<_, Error>` according to the type environment. Error/Ok parts are
	not inspected yet.
	"""
	for fname, info in func_infos.items():
		if not info.declared_can_throw:
			continue
		ssa_fn = ssa_funcs.get(fname)
		if ssa_fn is None:
			continue
		fn_type_error = None
		# We assume SSA layer can expose returns; in this skeleton we scan MIR
		# terminators in the underlying MIR function carried by SsaFunc.
		for block in ssa_fn.func.blocks.values():
			term = block.terminator
			if isinstance(term, Return) and term.value is not None:
				ty = type_env.type_of_ssa_value(fname, term.value)
				if not type_env.is_fnresult(ty):
					fn_type_error = (
						f"function {fname} is declared can-throw but return in block "
						f"{block.name} has non-FnResult type {ty!r}"
					)
					break
		if fn_type_error is not None:
			raise RuntimeError(fn_type_error)


def run_throw_checks(
	funcs: Dict[str, MirFunc],
	summaries: Dict[str, ThrowSummary],
	declared_can_throw: Dict[str, bool],
	ssa_funcs: Dict[str, "SsaFunc"] | None = None,  # type: ignore[name-defined]
	type_env: TypeEnv | None = None,
) -> Dict[str, FuncThrowInfo]:
	"""
	Convenience wrapper to build FuncThrowInfo and run all stage4 throw invariants.

	This keeps the pipeline driver simple: given MIR functions, throw summaries
	from stage3, and the checker-supplied `declared_can_throw` map, we:
	  1. build FuncThrowInfo,
	  2. enforce can-throw invariants,
	  3. enforce return-shape invariants for can-throw functions (structural),
	  4. enforce FnResult shape:
	     - structural guard for untyped/unit tests,
	     - type-aware FnResult check when SSA + TypeEnv are provided (structural
	       guard is skipped in that case to allow forwarding/aliasing),
	  5. return the FuncThrowInfo map for further stages to consume.
	"""
	func_infos = build_func_throw_info(summaries, declared_can_throw)
	enforce_can_throw_invariants(func_infos)
	enforce_return_shape_for_can_throw(func_infos, funcs)
	# FnResult shape: run either the structural guard (untyped/unit tests) or
	# the type-aware guard (typed pipeline). Avoid double-errors so that typed
	# paths can return/forward FnResult values freely.
	if ssa_funcs is not None and type_env is not None:
		enforce_fnresult_returns_typeaware(func_infos, ssa_funcs, type_env)
	else:
		enforce_fnresult_returns_for_can_throw(func_infos, funcs)
	return func_infos
