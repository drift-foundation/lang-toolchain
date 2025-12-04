# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage 3: MIR pre-analyses (address-taken, may-fail flags).

Pipeline placement:
  stage0 (AST) → stage1 (HIR) → stage2 (MIR) → stage3 (pre-analysis) → SSA → LLVM/obj

This module walks a MIR function and computes side tables that later stages
(SSA construction, storage allocation) can consult. The intent is to keep MIR→SSA
purely structural: all semantic flags are computed here.

Current analyses:
  - address_taken: locals whose address is observed (AddrOfLocal)
Planned:
  - may_fail: instructions/terminators that can fail/throw
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Set

from lang2.stage2 import (
	MirFunc,
	MInstr,
	MTerminator,
	AddrOfLocal,
	Call,
	MethodCall,
	ConstructDV,
)


@dataclass
class MirAnalysisResult:
	"""Holds pre-analysis results for a single MirFunc."""

	address_taken: Set[str]
	may_fail_instrs: Set[int]  # placeholder for future use (e.g., instruction indices)


class MirPreAnalysis:
	"""
	Run MIR pre-analyses:
	  - address_taken: which locals have their address observed
	  - may_fail: stubbed for now

	Entry point:
	  analyze(func: MirFunc) -> MirAnalysisResult
	"""

	def analyze(self, func: MirFunc) -> MirAnalysisResult:
		addr_taken: Set[str] = set()
		may_fail: Set[int] = set()  # not used yet; reserved for future

		for block in func.blocks.values():
			for instr in block.instructions:
				self._visit_instr(instr, addr_taken, may_fail)
			if block.terminator is not None:
				self._visit_term(block.terminator, may_fail)

		return MirAnalysisResult(address_taken=addr_taken, may_fail_instrs=may_fail)

	def _visit_instr(
		self, instr: MInstr, addr_taken: Set[str], may_fail: Set[int]
	) -> None:
		"""Inspect a MIR instruction and update analysis sets."""
		if isinstance(instr, AddrOfLocal):
			addr_taken.add(instr.local)
		# may_fail stubs: calls / DV construction can be marked later
		if isinstance(instr, (Call, MethodCall, ConstructDV)):
			# Placeholder: assume calls may fail; refine when you add richer flags.
			# We do not record IDs yet; may_fail remains empty until SSA needs it.
			pass

	def _visit_term(self, term: MTerminator, may_fail: Set[int]) -> None:
		"""Inspect a MIR terminator. Currently no-op; reserved for future."""
		return
