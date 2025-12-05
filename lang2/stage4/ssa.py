# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage 4: MIR → SSA skeleton.

Pipeline placement:
  stage0 (AST) → stage1 (HIR) → stage2 (MIR) → stage3 (pre-analysis) → stage4 (SSA) → LLVM/obj

This module defines a minimal SSA conversion pass over MIR. To keep the
architecture clean and incremental, the first version only handles straight-line
functions (single basic block, no branches/φ). It establishes the stage API and
will be extended to full SSA (dominators, φ insertion, renaming) later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from lang2.stage2 import MirFunc, LoadLocal, StoreLocal, MInstr


@dataclass
class SsaFunc:
	"""
	Wrapper for an SSA-ified MIR function.

	Currently just holds the MirFunc; later can carry version tables or SSA-only
	metadata if needed.
	"""

	func: MirFunc


class MirToSSA:
	"""
	Convert MIR to SSA form.

	First cut: only supports straight-line MIR (single block, no branches), with
	a simple version map for locals. This sets up the stage API; full SSA
	(dominance, φ insertion, renaming) will be added incrementally.
	"""

	def run(self, func: MirFunc) -> SsaFunc:
		# Guardrails: keep the first iteration simple and explicit.
		if len(func.blocks) != 1:
			raise NotImplementedError("SSA: only single-block functions are supported in this skeleton")

		block = func.blocks[func.entry]
		version: Dict[str, int] = {}
		new_instrs: list[MInstr] = []

		for instr in block.instructions:
			if isinstance(instr, StoreLocal):
				idx = version.get(instr.local, 0) + 1
				version[instr.local] = idx
				# For now, we do not rewrite the instruction; we just record versions.
				# Later, stores/loads will be rewritten to SSA temps.
				new_instrs.append(instr)
			elif isinstance(instr, LoadLocal):
				if instr.local not in version:
					raise RuntimeError(f"SSA: load before store for local '{instr.local}'")
				new_instrs.append(instr)
			else:
				new_instrs.append(instr)

		block.instructions = new_instrs
		return SsaFunc(func=func)
