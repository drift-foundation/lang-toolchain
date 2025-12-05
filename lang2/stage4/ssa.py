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
	AssignSSA,
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
		  - Straight-line MIR is fully rewritten into SSA (AssignSSA) with
		    version tracking and load-after-store checks.
		  - Multi-block MIR support is being brought up incrementally; loops
		    (backedges) are still rejected, and only acyclic CFGs are allowed.

		Returns an SsaFunc wrapper carrying the original MirFunc plus version
		tables. Multi-block SSA will be expanded gradually (φ placement and
		renaming) using the CFG utilities in stage4/dom.py.
		"""
		# Guardrail: reject loops/backedges until loop SSA is implemented.
		if self._has_backedge(func):
			raise NotImplementedError("SSA: loops/backedges not supported yet")

		# Single-block fast path: rewrite loads/stores to AssignSSA with versions.
		if len(func.blocks) == 1:
			return self._run_single_block(func)

		# Multi-block acyclic SSA (if/else CFGs, no loops) with φ placement + renaming.
		return self._run_multi_block_acyclic(func)

	def _run_single_block(self, func: MirFunc) -> SsaFunc:
		"""Rewrite a single-block MIR function into SSA using AssignSSA moves."""
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
				new_instrs.append(AssignSSA(dest=ssa_name, src=instr.value))
			elif isinstance(instr, LoadLocal):
				if instr.local not in version:
					raise RuntimeError(f"SSA: load before store for local '{instr.local}'")
				# Load sees the current SSA value for the local.
				value_for_instr[(block.name, idx)] = current_value[instr.local]
				new_instrs.append(AssignSSA(dest=instr.dest, src=current_value[instr.local]))
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

	# --- helpers ---

	def _has_backedge(self, func: MirFunc) -> bool:
		"""Detect backedges (cycles) via DFS; used to reject loops for now."""
		succs: Dict[str, set[str]] = {b: set() for b in func.blocks}
		for bname, block in func.blocks.items():
			term = block.terminator
			if isinstance(term, Goto):
				succs[bname].add(term.target)
			elif isinstance(term, IfTerminator):
				succs[bname].add(term.then_target)
				succs[bname].add(term.else_target)

		visited: set[str] = set()
		stack: set[str] = set()

		def dfs(node: str) -> bool:
			visited.add(node)
			stack.add(node)
			for s in succs.get(node, ()):
				if s not in visited:
					if dfs(s):
						return True
				elif s in stack:
					return True
			stack.remove(node)
			return False

		return dfs(func.entry)

	def _run_multi_block_acyclic(self, func: MirFunc) -> SsaFunc:
		"""
		SSA for acyclic CFGs (if/else diamonds). Places φ nodes and rewrites
		LoadLocal/StoreLocal to AssignSSA using a dominator-tree renaming pass.
		Loops are still rejected by the caller.
		"""
		dom_info = DominatorAnalysis().compute(func)
		df_info = DominanceFrontierAnalysis().compute(func, dom_info)

		# CFG maps
		preds: Dict[str, set[str]] = {b: set() for b in func.blocks}
		succs: Dict[str, set[str]] = {b: set() for b in func.blocks}
		for bname, block in func.blocks.items():
			term = block.terminator
			if isinstance(term, Goto):
				preds[term.target].add(bname)
				succs[bname].add(term.target)
			elif isinstance(term, IfTerminator):
				preds[term.then_target].add(bname)
				preds[term.else_target].add(bname)
				succs[bname].add(term.then_target)
				succs[bname].add(term.else_target)

		# Definition sites and values per local.
		def_sites: Dict[str, set[str]] = {}
		def_values: Dict[str, Dict[str, str]] = {}
		for bname, block in func.blocks.items():
			for instr in block.instructions:
				if isinstance(instr, StoreLocal):
					def_sites.setdefault(instr.local, set()).add(bname)
					def_values.setdefault(instr.local, {})[bname] = instr.value

		# Place φ nodes using dominance frontiers (simple Cytron iteration).
		placed: set[tuple[str, str]] = set()
		for local, def_blocks in def_sites.items():
			if len(def_blocks) < 2:
				continue
			worklist = list(def_blocks)
			while worklist:
				b = worklist.pop()
				for y in df_info.df.get(b, set()):
					if (local, y) in placed:
						continue
					phi = Phi(dest=local, incoming={})
					setattr(phi, "local", local)  # remember the logical local name
					func.blocks[y].instructions.insert(0, phi)
					placed.add((local, y))
					if y not in def_blocks:
						def_blocks.add(y)
						worklist.append(y)

		# Dominator-tree children.
		children: Dict[str, set[str]] = {b: set() for b in func.blocks}
		for b, i in dom_info.idom.items():
			if i is not None:
				children[i].add(b)

		# SSA renaming stacks/counters.
		counters: Dict[str, int] = {}
		stacks: Dict[str, list[str]] = {}
		value_for_instr: Dict[tuple[str, int], str] = {}

		def new_name(local: str) -> str:
			counters[local] = counters.get(local, 0) + 1
			name = f"{local}_{counters[local]}"
			stacks.setdefault(local, []).append(name)
			return name

		def current(local: str) -> str:
			if local not in stacks or not stacks[local]:
				raise RuntimeError(f"SSA: load before store for local '{local}' in multi-block rename")
			return stacks[local][-1]

		def rename_block(block_name: str) -> None:
			block = func.blocks[block_name]
			locals_defined: list[str] = []
			new_instrs: list[MInstr] = []

			for idx, instr in enumerate(block.instructions):
				if isinstance(instr, Phi):
					local = getattr(instr, "local", instr.dest)
					dest_name = new_name(local)
					instr.dest = dest_name
					value_for_instr[(block_name, len(new_instrs))] = dest_name
					new_instrs.append(instr)
					locals_defined.append(local)
				elif isinstance(instr, StoreLocal):
					local = instr.local
					dest_name = new_name(local)
					value_for_instr[(block_name, len(new_instrs))] = dest_name
					new_instrs.append(AssignSSA(dest=dest_name, src=instr.value))
					locals_defined.append(local)
				elif isinstance(instr, LoadLocal):
					local = instr.local
					src_name = current(local)
					value_for_instr[(block_name, len(new_instrs))] = src_name
					new_instrs.append(AssignSSA(dest=instr.dest, src=src_name))
				else:
					new_instrs.append(instr)

			block.instructions = new_instrs

			# Patch phi incoming values in successors using current stacks.
			for succ in succs.get(block_name, ()):
				for succ_instr in func.blocks[succ].instructions:
					if isinstance(succ_instr, Phi):
						local = getattr(succ_instr, "local", succ_instr.dest)
						if local in stacks and stacks[local]:
							succ_instr.incoming[block_name] = stacks[local][-1]

			# Recurse dominator-tree children.
			for child in children[block_name]:
				rename_block(child)

			# Pop locals defined in this block to restore stacks.
			for local in reversed(locals_defined):
				stacks[local].pop()

		rename_block(func.entry)

		return SsaFunc(
			func=func,
			local_versions=counters,
			current_value={k: v[-1] for k, v in stacks.items() if v},
			value_for_instr=value_for_instr,
		)
