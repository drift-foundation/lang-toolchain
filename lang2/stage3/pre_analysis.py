# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage 3: MIR pre-analyses (address-taken, may-fail flags, throw summary).

Pipeline placement:
  stage0 (AST) → stage1 (HIR) → stage2 (MIR) → stage3 (pre-analysis) → SSA → LLVM/obj

This module walks a MIR function and computes side tables that later stages
(SSA construction, storage allocation) can consult. The intent is to keep MIR→SSA
purely structural: all semantic flags are computed here.

Current analyses:
  - address_taken: locals whose address is observed (AddrOfLocal)
  - may_fail: instruction sites that can fail/throw (calls, method calls,
    ConstructDV, ConstructError)
  - throw summary: per-function flags/sets for error construction and the
    exception DV types involved (when a code→exception mapping is provided)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Set, Dict, Mapping, Optional

from lang2.stage2 import (
	MirFunc,
	MInstr,
	MTerminator,
	AddrOfLocal,
	Call,
	MethodCall,
	ConstructDV,
	ConstructError,
	ConstInt,
)


@dataclass
class MirAnalysisResult:
	"""Holds pre-analysis results for a single MirFunc."""

	address_taken: Set[str]
	may_fail: Set[tuple[str, int]]  # (block_name, instruction_index)
	construct_error_sites: Set[tuple[str, int]]  # where ConstructError appears
	exception_types: Set[str] = field(default_factory=set)  # DV type names seen in ConstructError


class MirPreAnalysis:
	"""
	Run MIR pre-analyses:
	  - address_taken: which locals have their address observed
	  - may_fail: which instruction sites can fail (conservative)
	  - construct_error_sites: where Errors are constructed (throw sites)
	  - exception_types: DV type names used in ConstructError

	Entry point:
	  analyze(func: MirFunc) -> MirAnalysisResult
	"""

	def __init__(self, code_to_exc: Optional[Mapping[int, str]] = None) -> None:
		"""
		Optionally accept a mapping from event code -> exception DV name.
		This allows exception_types to be populated when ConstructError uses
		a constant event code produced from exception metadata.
		"""
		self._code_to_exc = dict(code_to_exc) if code_to_exc is not None else {}

	def analyze(self, func: MirFunc) -> MirAnalysisResult:
		addr_taken: Set[str] = set()
		may_fail: Set[tuple[str, int]] = set()
		construct_error_sites: Set[tuple[str, int]] = set()
		exception_types: Set[str] = set()

		for block_name, block in func.blocks.items():
			const_ints: Dict[str, int] = {}
			for idx, instr in enumerate(block.instructions):
				if isinstance(instr, ConstInt):
					const_ints[instr.dest] = instr.value
				self._visit_instr(
					block_name,
					idx,
					instr,
					addr_taken,
					may_fail,
					construct_error_sites,
					exception_types,
					const_ints,
				)
			if block.terminator is not None:
				self._visit_term(block_name, block.terminator, may_fail)

		return MirAnalysisResult(
			address_taken=addr_taken,
			may_fail=may_fail,
			construct_error_sites=construct_error_sites,
			exception_types=exception_types,
		)

	def _visit_instr(
		self,
		block_name: str,
		idx: int,
		instr: MInstr,
		addr_taken: Set[str],
		may_fail: Set[tuple[str, int]],
		construct_error_sites: Set[tuple[str, int]],
		exception_types: Set[str],
		const_ints: Dict[str, int],
	) -> None:
		"""Inspect a MIR instruction and update analysis sets."""
		if isinstance(instr, AddrOfLocal):
			addr_taken.add(instr.local)

		may_fail_ops = (
			Call,
			MethodCall,
			ConstructDV,
			ConstructError,
			# Conservatively include field/index ops if you treat them as potentially failing.
			# This can be refined later.
			# LoadField, StoreField, LoadIndex, StoreIndex, ...
		)
		if isinstance(instr, may_fail_ops):
			may_fail.add((block_name, idx))

		if isinstance(instr, ConstructError):
			construct_error_sites.add((block_name, idx))
			# If the event code operand is a literal const_int, map it to an exception DV name.
			code_name = getattr(instr, "code", None)
			if code_name in const_ints:
				code_val = const_ints[code_name]
				if code_val in self._code_to_exc:
					exception_types.add(self._code_to_exc[code_val])

	def _visit_term(self, block_name: str, term: MTerminator, may_fail: Set[tuple[str, int]]) -> None:
		"""Inspect a MIR terminator. Currently no-op; reserved for future."""
		return
