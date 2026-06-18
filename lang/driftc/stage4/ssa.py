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

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List

from lang.driftc import debug as drift_debug

from lang.driftc.stage2 import (
	MirFunc,
	AddrOfLocal,
	LoadLocal,
	StoreLocal,
	MInstr,
	AssignSSA,
	Phi,
	ZeroValue,
	Goto,
	IfTerminator,
	MoveFromRef,
)
from lang.driftc.stage2 import cfg as _cfg
from lang.driftc.stage4.dom import DominatorAnalysis, DominanceFrontierAnalysis


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
	block_order: List[str] = field(default_factory=list)
	cfg_kind: "CfgKind" | None = None


class CfgKind(Enum):
	"""Shape of the CFG as seen by SSA/codegen."""

	STRAIGHT_LINE = auto()
	ACYCLIC = auto()
	GENERAL = auto()


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

		Contract for this stage:
		  - Straight-line MIR is fully rewritten into SSA (AssignSSA) with
		    version tracking and load-after-store checks.
		  - Multi-block MIR is rewritten into SSA using dominators + dominance
		    frontiers (φ placement) + renaming.
		  - Loops/backedges are supported: the same dominance-frontier algorithm
		    works for cyclic CFGs, and the renaming pass patches φ incomings when
		    visiting backedge predecessors.

		Returns an SsaFunc wrapper carrying the original MirFunc plus version
		tables. Multi-block SSA will be expanded gradually (φ placement and
		renaming) using the CFG utilities in stage4/dom.py.
		"""
		# Single-block fast path: rewrite loads/stores to AssignSSA with versions.
		if len(func.blocks) == 1:
			return self._run_single_block(func)

		# Multi-block SSA with φ placement + renaming (supports loops/backedges).
		has_cycle = self._has_backedge(func)
		ssa = self._run_multi_block_acyclic(func)
		ssa.cfg_kind = CfgKind.GENERAL if has_cycle else CfgKind.ACYCLIC
		return ssa

	def _assign_with_span(self, dest: str, src: str, src_instr: MInstr, local: str | None = None) -> AssignSSA:
		"""Create AssignSSA while preserving source span metadata when present."""
		new_instr = AssignSSA(dest=dest, src=src)
		if hasattr(src_instr, "span"):
			setattr(new_instr, "span", getattr(src_instr, "span"))
		if local is not None:
			setattr(new_instr, "local", local)
		return new_instr

	def _run_single_block(self, func: MirFunc) -> SsaFunc:
		"""Rewrite a single-block MIR function into SSA using AssignSSA moves."""
		# Locals whose address is taken must remain as real storage (loads/stores),
		# not SSA aliases. SSA renaming would sever pointer identity: `&x` must
		# continue to refer to a stable storage slot for `x`.
		# `MoveFromRef(local=L, ...)` is also addr-taken-equivalent: codegen
		# writes the transferred bytes into L's alloca-backed storage; SSA
		# renaming the LoadLocal/StoreLocal of L would silently miss the
		# MoveFromRef-written value and read whatever the SSA pass last
		# tracked for L.
		addr_taken: set[str] = set()
		for block in func.blocks.values():
			for instr in block.instructions:
				if isinstance(instr, AddrOfLocal):
					addr_taken.add(instr.local)
				elif isinstance(instr, MoveFromRef):
					addr_taken.add(instr.local)

		block = func.blocks[func.entry]
		version: Dict[str, int] = {}
		current_value: Dict[str, str] = {}
		# Seed parameter locals so loads are valid without an explicit store.
		for param in func.params:
			if param in addr_taken:
				continue
			version[param] = 1
			current_value[param] = param
		new_instrs: list[MInstr] = []
		value_for_instr: Dict[tuple[str, int], str] = {}
		for idx, instr in enumerate(block.instructions):
			if isinstance(instr, StoreLocal):
				if instr.local in addr_taken:
					new_instrs.append(instr)
					continue
				version_idx = version.get(instr.local, 0) + 1
				version[instr.local] = version_idx
				ssa_name = f"{instr.local}_{version_idx}"
				current_value[instr.local] = ssa_name
				value_for_instr[(block.name, idx)] = ssa_name
				new_instr = self._assign_with_span(dest=ssa_name, src=instr.value, src_instr=instr, local=instr.local)
				if hasattr(instr, "debug_name"):
					setattr(new_instr, "debug_name", getattr(instr, "debug_name"))
				new_instrs.append(new_instr)
			elif isinstance(instr, LoadLocal):
				if instr.local in addr_taken:
					new_instrs.append(instr)
					continue
				if instr.local not in version:
					if drift_debug.enabled("ssa"):
						print(f"[drift:ssa] load-before-store local={instr.local} block={block.name} fn={func.fn_id}")
					raise RuntimeError(f"SSA: load before store for local '{instr.local}'")
				# Load sees the current SSA value for the local.
				value_for_instr[(block.name, idx)] = current_value[instr.local]
				new_instr = self._assign_with_span(dest=instr.dest, src=current_value[instr.local], src_instr=instr)
				new_instrs.append(new_instr)
			else:
				new_instrs.append(instr)

		block.instructions = new_instrs
		return SsaFunc(
			func=func,
			local_versions=version,
			current_value=current_value,
			value_for_instr=value_for_instr,
			block_order=[func.entry],
			cfg_kind=CfgKind.STRAIGHT_LINE,
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
			# Central MIR CFG-successor contract (stage2/cfg.py).
			for succ_name in _cfg.terminator_successors(block.terminator):
				preds[succ_name].add(bname)

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
			# Central MIR CFG-successor contract (stage2/cfg.py).
			for succ_name in _cfg.terminator_successors(block.terminator):
				succs[bname].add(succ_name)

		# Iterative DFS with an explicit work stack. The recursive form here
		# (`def dfs(node)` with `dfs(s)` inside) overflowed Python's
		# default recursion limit on deep linear CFGs (~1000 blocks).
		# Linear chains arise from large match expressions and similar
		# shapes; the depth is user-controlled, so the recursive form
		# was unbounded in practice.
		#
		# The work stack holds (node, iterator-over-successors) pairs. When a
		# node's successors are exhausted, it is popped from `on_path`; an
		# unvisited successor is pushed; an `on_path` successor is the
		# backedge we're looking for.
		visited: set[str] = set()
		on_path: set[str] = set()
		work: list[tuple[str, "iter[str]"]] = []

		root = func.entry
		visited.add(root)
		on_path.add(root)
		work.append((root, iter(succs.get(root, ()))))

		while work:
			node, it = work[-1]
			next_succ = None
			for s in it:
				if s not in visited:
					next_succ = s
					break
				if s in on_path:
					return True
			if next_succ is None:
				on_path.discard(node)
				work.pop()
			else:
				visited.add(next_succ)
				on_path.add(next_succ)
				work.append((next_succ, iter(succs.get(next_succ, ()))))
		return False

	def _run_multi_block_acyclic(self, func: MirFunc) -> SsaFunc:
		"""
		SSA for acyclic CFGs (if/else diamonds). Places φ nodes and rewrites
		LoadLocal/StoreLocal to AssignSSA using a dominator-tree renaming pass.
		Loops are still rejected by the caller.
		"""
		# Prune unreachable blocks so we do not create φ nodes or CFG edges for
		# dead code produced by earlier lowering (e.g., unreachable try-cont
		# blocks after an always-throwing try). This keeps the predecessor lists
		# and φ incomings consistent for LLVM.
		def _reachable(entry: str) -> set[str]:
			succs: Dict[str, list[str]] = {}
			for name, block in func.blocks.items():
				# Central MIR CFG-successor contract (stage2/cfg.py); order preserved.
				succs[name] = list(_cfg.terminator_successors(block.terminator))
			seen: set[str] = set()
			stack: list[str] = [entry]
			while stack:
				b = stack.pop()
				if b in seen:
					continue
				seen.add(b)
				stack.extend(succs.get(b, ()))
			return seen

		reachable = _reachable(func.entry)
		if len(reachable) != len(func.blocks):
			func.blocks = {name: block for name, block in func.blocks.items() if name in reachable}

		dom_info = DominatorAnalysis().compute(func)
		df_info = DominanceFrontierAnalysis().compute(func, dom_info)

		# Locals whose address is taken must remain as real storage (loads/stores),
		# not SSA aliases; see _run_single_block for rationale.  `MoveFromRef`
		# destinations are addr-taken-equivalent for the same reason
		# (codegen writes into alloca-backed storage; SSA renaming would
		# elide the write).
		addr_taken: set[str] = set()
		for block in func.blocks.values():
			for instr in block.instructions:
				if isinstance(instr, AddrOfLocal):
					addr_taken.add(instr.local)
				elif isinstance(instr, MoveFromRef):
					addr_taken.add(instr.local)

		# CFG maps
		preds: Dict[str, set[str]] = {b: set() for b in func.blocks}
		succs: Dict[str, set[str]] = {b: set() for b in func.blocks}
		for bname, block in func.blocks.items():
			# Central MIR CFG-successor contract (stage2/cfg.py).
			for succ_name in _cfg.terminator_successors(block.terminator):
				preds[succ_name].add(bname)
				succs[bname].add(succ_name)

		# Definition sites and values per local.
		def_sites: Dict[str, set[str]] = {}
		def_values: Dict[str, Dict[str, str]] = {}
		use_sites: Dict[str, set[str]] = {}
		for bname, block in func.blocks.items():
			for instr in block.instructions:
				if isinstance(instr, StoreLocal):
					if instr.local in addr_taken:
						continue
					def_sites.setdefault(instr.local, set()).add(bname)
					def_values.setdefault(instr.local, {})[bname] = instr.value
				elif isinstance(instr, LoadLocal):
					if instr.local in addr_taken:
						continue
					use_sites.setdefault(instr.local, set()).add(bname)

		# Place φ nodes using dominance frontiers (simple Cytron iteration).
		placed: set[tuple[str, str]] = set()
		for local, def_blocks in def_sites.items():
			if len(def_blocks) < 2:
				continue
			# If a local is never read, we do not need any φ nodes for it.
			#
			# Note: do *not* try to optimize away φ placement based on whether uses
			# appear only inside defining blocks. That heuristic is wrong for loops:
			# a loop-carried variable can be "used in the same block it is defined"
			# and still require a φ at the loop header to model the backedge.
			use_blocks = use_sites.get(local, set())
			if not use_blocks:
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
		zero_defs: Dict[tuple[str, str], str] = {}

		def new_name(local: str) -> str:
			counters[local] = counters.get(local, 0) + 1
			name = f"{local}_{counters[local]}"
			stacks.setdefault(local, []).append(name)
			return name

		def current(local: str) -> str:
			if local not in stacks or not stacks[local]:
				if drift_debug.enabled("ssa"):
					print(f"[drift:ssa] load-before-store local={local} block={block.name} fn={func.fn_id}")
				raise RuntimeError(f"SSA: load before store for local '{local}' in multi-block rename (fn={func.fn_id} block={block.name})")
			return stacks[local][-1]

		# Seed parameter versions so loads in entry can read them.
		#
		# Address-taken params stay as stable storage locals; SSA renaming would
		# break `&param` identity.
		new_params: list[str] = []
		for param in func.params:
			if param in addr_taken:
				new_params.append(param)
				continue
			new_name(param)
			new_params.append(current(param))
		# Update the function params to the SSA-renamed symbols so headers and
		# body stay consistent for non-address-taken params.
		func.params = new_params

		# Per-block locals-defined list, retained for the deferred post-order
		# stack-pop step in the iterative dominator-tree walk below.
		per_block_locals_defined: Dict[str, list[str]] = {}

		def rename_block_body(block_name: str) -> list[str]:
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
					if local in addr_taken:
						new_instrs.append(instr)
						continue
					dest_name = new_name(local)
					value_for_instr[(block_name, len(new_instrs))] = dest_name
					new_instr = self._assign_with_span(dest=dest_name, src=instr.value, src_instr=instr, local=local)
					if hasattr(instr, "debug_name"):
						setattr(new_instr, "debug_name", getattr(instr, "debug_name"))
					new_instrs.append(new_instr)
					locals_defined.append(local)
				elif isinstance(instr, LoadLocal):
					local = instr.local
					if local in addr_taken:
						new_instrs.append(instr)
						continue
					src_name = current(local)
					value_for_instr[(block_name, len(new_instrs))] = src_name
					new_instr = self._assign_with_span(dest=instr.dest, src=src_name, src_instr=instr)
					new_instrs.append(new_instr)
				else:
					if drift_debug.enabled("ssa"):
						import sys
						from lang.driftc.stage2.mir_nodes import Call as MCall
						if isinstance(instr, MCall) and getattr(instr.fn_id, "module", None) == "main":
							print(f"[drift:ssa] call instr pre fn={instr.fn_id} span={getattr(instr, 'span', None)}", file=sys.stderr)
					new_instrs.append(instr)

			block.instructions = new_instrs
			if drift_debug.enabled("ssa"):
				import sys
				from lang.driftc.stage2.mir_nodes import Call as MCall
				for instr in block.instructions:
					if isinstance(instr, MCall) and getattr(instr.fn_id, "module", None) == "main":
						print(f"[drift:ssa] call instr post fn={instr.fn_id} span={getattr(instr, 'span', None)} block={block_name}", file=sys.stderr)

			# Patch phi incoming values in successors using current stacks.
			for succ in succs.get(block_name, ()):
				for succ_instr in func.blocks[succ].instructions:
					if isinstance(succ_instr, Phi):
						local = getattr(succ_instr, "local", succ_instr.dest)
						if local in stacks and stacks[local]:
							succ_instr.incoming[block_name] = stacks[local][-1]
							continue
						key = (block_name, local)
						zero_name = zero_defs.get(key)
						if zero_name is None:
							ty = func.local_types.get(local)
							if ty is None:
								raise RuntimeError(f"SSA: missing type for zero-init local '{local}' in block '{block_name}'")
							zero_name = new_name(local)
							zero_defs[key] = zero_name
							locals_defined.append(local)
							block.instructions.append(ZeroValue(dest=zero_name, ty=ty))
						succ_instr.incoming[block_name] = zero_name

			# Return the per-block locals_defined; the iterative driver below
			# uses this to restore stacks in post-order.
			return locals_defined

		# Iterative pre/post-order walk of the dominator tree.  The
		# recursive form blew Python's recursion limit on deep linear
		# CFGs (huge match shapes).  Each work-stack entry
		# is (block_name, expanded). On the first visit (`expanded == False`)
		# we run the pre-order body (renaming) and schedule a deferred
		# post-order entry plus children. On the second visit (`expanded ==
		# True`) we restore the local-version stacks.
		_walk: list[tuple[str, bool]] = [(func.entry, False)]
		while _walk:
			block_name, expanded = _walk.pop()
			if expanded:
				for local in reversed(per_block_locals_defined[block_name]):
					stacks[local].pop()
				continue
			per_block_locals_defined[block_name] = rename_block_body(block_name)
			_walk.append((block_name, True))
			for child in children[block_name]:
				_walk.append((child, False))

		# Prune trivial φ nodes (single incoming); replace with AssignSSA to keep IR verifiable.
		for block in func.blocks.values():
			new_instrs: list[MInstr] = []
			for instr in block.instructions:
				if isinstance(instr, Phi) and len(instr.incoming) == 1:
					src = next(iter(instr.incoming.values()))
					new_instr = self._assign_with_span(dest=instr.dest, src=src, src_instr=instr, local=getattr(instr, "local", None))
					new_instrs.append(new_instr)
					continue
				new_instrs.append(instr)
			block.instructions = new_instrs

		return SsaFunc(
			func=func,
			local_versions=counters,
			current_value={k: v[-1] for k, v in stacks.items() if v},
			value_for_instr=value_for_instr,
			block_order=self._compute_block_order(func),
			cfg_kind=CfgKind.ACYCLIC,
		)

	def _compute_block_order(self, func: MirFunc) -> list[str]:
		"""
		Compute a deterministic reverse-postorder block order from entry.

		Unreachable blocks (if any) are appended after reachable blocks.
		"""
		succs: Dict[str, list[str]] = {}
		for name, block in func.blocks.items():
			# Central MIR CFG-successor contract (stage2/cfg.py); order preserved.
			succs[name] = list(_cfg.terminator_successors(block.terminator))

		# Iterative post-order DFS.  Recursive form overflowed Python's
		# recursion limit on deep linear CFGs (huge match shapes).
		#
		# Determinism: a LIFO work stack visits successors in reverse order
		# of how they were pushed. To preserve the *exact* visitation order
		# of the prior recursive `for succ in succs[b]: dfs(succ)` walk on
		# branched CFGs (which matters for downstream block-order-sensitive
		# passes), we push successors in reverse so the pop order matches.
		visited: set[str] = set()
		post: list[str] = []
		_walk: list[tuple[str, bool]] = [(func.entry, False)]
		while _walk:
			b, expanded = _walk.pop()
			if expanded:
				post.append(b)
				continue
			if b in visited:
				continue
			visited.add(b)
			_walk.append((b, True))
			for succ in reversed(succs.get(b, [])):
				if succ not in visited:
					_walk.append((succ, False))
		rpo = list(reversed(post))
		unreachable = [name for name in func.blocks if name not in visited]
		return rpo + unreachable
