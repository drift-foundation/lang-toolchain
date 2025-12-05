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

from lang2.stage2 import (
	MirFunc,
	LoadLocal,
	StoreLocal,
	MInstr,
	Phi,
	Goto,
	IfTerminator,
)
from lang2.stage4.dom import DominatorAnalysis, DominanceFrontierAnalysis


@dataclass
class SsaFunc:
	"""
	Wrapper for an SSA-ified MIR function.

	Tracks:
	  - func: the underlying MIR function
	  - local_versions: how many SSA definitions each local has (x -> n)
	  - current_value: the latest SSA name for each local (x -> "x_n")
	  - value_for_instr: SSA name defined/used by each instruction (by (block, idx))
	"""

	func: MirFunc
	local_versions: Dict[str, int]
	current_value: Dict[str, str]
	value_for_instr: Dict[tuple[str, int], str]


class MirToSSA:
	"""
	Convert MIR to SSA form.

	First cut: only supports straight-line MIR (single block, no branches), with
	a simple version map for locals. This sets up the stage API; full SSA
	(dominance, φ insertion, renaming) will be added incrementally.
	"""

	def run(self, func: MirFunc) -> SsaFunc:
		"""
		Entry point for the SSA stage.

		Contract for this skeleton:
		  - Only single-block MIR functions are supported (no branches/φ).
		  - Enforces load-after-store for locals.
		  - Records SSA-style version info for locals (x -> x_1, x_2, ...).

		Returns an SsaFunc wrapper carrying the original MirFunc plus version
		tables. Later iterations will rewrite instructions and handle multi-block
		SSA with φ nodes.
		"""
		# Guardrails: keep the first iteration simple and explicit.
		if len(func.blocks) != 1:
			raise NotImplementedError("SSA: only single-block functions are supported in this skeleton")

		block = func.blocks[func.entry]
		version: Dict[str, int] = {}
		current_value: Dict[str, str] = {}
		new_instrs: list[MInstr] = []
		value_for_instr: Dict[tuple[str, int], str] = {}

		for idx, instr in enumerate(block.instructions):
			if isinstance(instr, StoreLocal):
				version_idx = version.get(instr.local, 0) + 1
				version[instr.local] = version_idx
				ssa_name = f"{instr.local}_{version_idx}"
				current_value[instr.local] = ssa_name
				value_for_instr[(block.name, idx)] = ssa_name
				# For now, we do not rewrite the instruction; we just record versions.
				# Later, stores/loads will be rewritten to SSA temps.
				new_instrs.append(instr)
			elif isinstance(instr, LoadLocal):
				if instr.local not in version:
					raise RuntimeError(f"SSA: load before store for local '{instr.local}'")
				# Load sees the current SSA value for the local.
				value_for_instr[(block.name, idx)] = current_value[instr.local]
				new_instrs.append(instr)
			else:
				new_instrs.append(instr)

		block.instructions = new_instrs
		return SsaFunc(
			func=func,
			local_versions=version,
			current_value=current_value,
			value_for_instr=value_for_instr,
		)

	def run_experimental_multi_block(self, func: MirFunc) -> SsaFunc:
		"""
		Experimental multi-block SSA scaffold.

		Uses dominators + dominance frontiers to place Φ nodes for locals with
		definitions in multiple blocks. This is intentionally limited and only
		intended for test-driven bring-up on simple CFGs (e.g., diamonds). The
		main run() entry point remains single-block-only until this path is
		mature.
		"""
		# Control-flow helpers.
		dom_info = DominatorAnalysis().compute(func)
		df_info = DominanceFrontierAnalysis().compute(func, dom_info)

		# Predecessor map for incoming edges.
		preds: Dict[str, set[str]] = {b: set() for b in func.blocks}
		for bname, block in func.blocks.items():
			term = block.terminator
			if isinstance(term, Goto):
				preds[term.target].add(bname)
			elif isinstance(term, IfTerminator):
				preds[term.then_target].add(bname)
				preds[term.else_target].add(bname)

		# Definition sites and values per local.
		def_sites: Dict[str, set[str]] = {}
		def_values: Dict[str, Dict[str, str]] = {}
		for bname, block in func.blocks.items():
			for instr in block.instructions:
				if isinstance(instr, StoreLocal):
					def_sites.setdefault(instr.local, set()).add(bname)
					def_values.setdefault(instr.local, {})[bname] = instr.value

		placed: set[tuple[str, str]] = set()  # (local, block) pairs with φ already placed

		for local, blocks_with_def in def_sites.items():
			if len(blocks_with_def) < 2:
				continue  # no join needed
			worklist = list(blocks_with_def)
			while worklist:
				b = worklist.pop()
				for y in df_info.df.get(b, set()):
					if (local, y) in placed:
						continue
					# Build incoming map from predecessors; default to the local name if unknown.
					incoming: Dict[str, str] = {}
					for p in preds.get(y, ()):
						incoming[p] = def_values.get(local, {}).get(p, local)
					phi = Phi(dest=f"{local}_phi", incoming=incoming)
					func.blocks[y].instructions.insert(0, phi)
					placed.add((local, y))
		# For now we do not iterate further for newly added φ blocks; this is enough for simple diamonds.

		return SsaFunc(
			func=func,
			local_versions={},
			current_value={},
			value_for_instr={},
		)
