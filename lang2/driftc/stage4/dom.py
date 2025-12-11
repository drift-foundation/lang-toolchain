# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage 4: MIR dominator analysis.

Pipeline placement:
  stage0 (AST) → stage1 (HIR) → stage2 (MIR) → stage3 (pre-analysis) → stage4 (SSA, dominators) → LLVM/obj

This module computes immediate dominators for a MIR function's CFG. It is kept
separate from SSA so that SSA construction can consume a clean, well-tested
dominator table instead of reimplementing graph algorithms inline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Set, Optional

from lang2.driftc.stage2 import MirFunc, Goto, IfTerminator, MTerminator


@dataclass
class DominatorInfo:
	"""
	Immediate dominator table: idom[block] = immediate dominator block or None for entry.
	"""

	idom: Dict[str, Optional[str]] = field(default_factory=dict)


@dataclass
class DominanceFrontierInfo:
	"""
	Dominance frontier table: df[block] = set of blocks in its dominance frontier.

	Definition: b is in DF(x) iff x dominates a predecessor of b but does not
	strictly dominate b. Used for SSA φ-placement.
	"""

	df: Dict[str, Set[str]] = field(default_factory=dict)


class DominatorAnalysis:
	"""
	Compute immediate dominators for a MIR CFG.

	Algorithm: classic iterative dataflow:
	  - dom(entry) = {entry}
	  - dom(b) = all blocks initially
	  - dom(b) = {b} ∪ (⋂_{p ∈ preds(b)} dom(p)) until fixed point
	Then idom(b) is chosen as a dominator of b (other than b) that does not
	strictly dominate any other dominator candidate.
	"""

	def compute(self, func: MirFunc) -> DominatorInfo:
		"""Compute immediate dominators for the given MIR function."""
		blocks = list(func.blocks.keys())
		entry = func.entry

		# 1. Build predecessor map.
		preds: Dict[str, Set[str]] = {b: set() for b in blocks}
		for bname, block in func.blocks.items():
			term = block.terminator
			if isinstance(term, Goto):
				preds[term.target].add(bname)
			elif isinstance(term, IfTerminator):
				preds[term.then_target].add(bname)
				preds[term.else_target].add(bname)
			# Other terminators (Return, etc.) do not add outgoing edges.

		# 2. Initialize dom sets.
		dom: Dict[str, Set[str]] = {b: set(blocks) for b in blocks}
		dom[entry] = {entry}

		changed = True
		while changed:
			changed = False
			for b in blocks:
				if b == entry:
					continue
				if not preds[b]:
					# Unreachable block: conservatively, only itself.
					new_dom = {b}
				else:
					# Intersection of predecessors' dom sets, union with self.
					p_iter = iter(preds[b])
					inter = dom[next(p_iter)].copy()
					for p in p_iter:
						inter &= dom[p]
					new_dom = inter | {b}
				if new_dom != dom[b]:
					dom[b] = new_dom
					changed = True

		# 3. Compute immediate dominators via the standard definition:
		# idom(b) is the unique dominator of b (other than b) that dominates all other dominators of b.
		idom: Dict[str, Optional[str]] = {entry: None}
		for b in blocks:
			if b == entry:
				continue
			# Candidates are all dominators of b except b itself.
			candidates = dom[b] - {b}
			id_candidate: Optional[str] = None
			for c in candidates:
				# c is the immediate dominator if no other candidate dominates c
				# (i.e., c is the lowest dominator of b).
				if all((c == d) or (c not in dom[d]) for d in candidates):
					id_candidate = c
					break
			idom[b] = id_candidate

		return DominatorInfo(idom=idom)


class DominanceFrontierAnalysis:
	"""
	Compute dominance frontiers for a MIR CFG, given immediate dominators.

	Algorithm (standard two-phase):
	  1) DF_local: for each block b, add each successor s of b where idom[s] != b
	  2) DF_up: propagate from children in the dominator tree:
	     for each child c of b, for each w in DF[c], if idom[w] != b then add w to DF[b]
	"""

	def compute(self, func: MirFunc, dom_info: DominatorInfo) -> DominanceFrontierInfo:
		"""Compute dominance frontiers for all blocks in func."""
		blocks = list(func.blocks.keys())
		entry = func.entry

		# Build predecessor/successor maps (shared approach with dominator analysis).
		preds: Dict[str, Set[str]] = {b: set() for b in blocks}
		succ: Dict[str, Set[str]] = {b: set() for b in blocks}
		for bname, block in func.blocks.items():
			term = block.terminator
			if isinstance(term, Goto):
				preds[term.target].add(bname)
				succ[bname].add(term.target)
			elif isinstance(term, IfTerminator):
				preds[term.then_target].add(bname)
				preds[term.else_target].add(bname)
				succ[bname].add(term.then_target)
				succ[bname].add(term.else_target)

		idom = dom_info.idom

		# Build dominator-tree children from idom.
		children: Dict[str, Set[str]] = {b: set() for b in blocks}
		for b, i in idom.items():
			if i is not None:
				children[i].add(b)

		df: Dict[str, Set[str]] = {b: set() for b in blocks}

		# 1) Local frontiers.
		for b in blocks:
			for s in succ[b]:
				if idom.get(s) != b:
					df[b].add(s)

		# 2) Upwards propagation along the dominator tree.
		def _dfs(node: str) -> None:
			for child in children[node]:
				_dfs(child)
				for w in df[child]:
					if idom.get(w) != node:
						df[node].add(w)

		_dfs(entry)

		return DominanceFrontierInfo(df=df)
