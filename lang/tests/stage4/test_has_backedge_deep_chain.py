# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Robustness regression: MirToSSA._has_backedge must not RecursionError on
deep linear CFGs.

Surfaced by `work/robustness/robustness-matrix.md` row #6: a Drift source with
~1000 match arms produces a CFG deep enough to overflow Python's recursion
limit during stage4 SSA backedge detection.

This test is the minimal regression. It builds an N-block linear chain
directly as MIR (no parser/HIR/stage2 dependence), where N is well past
Python's default recursion limit (1000). The pre-fix recursive DFS dies with
`RecursionError`; the iterative DFS completes and correctly reports
`has_backedge == False` for an acyclic chain.
"""
from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.stage2 import BasicBlock, MirFunc, Goto, Return
from lang.driftc.stage4 import MirToSSA


def _build_linear_chain(n: int) -> MirFunc:
	"""Build a CFG with `n` blocks: b0 -> b1 -> ... -> b(n-1), terminating in Return."""
	assert n >= 2
	blocks: dict[str, BasicBlock] = {}
	for i in range(n - 1):
		blocks[f"b{i}"] = BasicBlock(
			name=f"b{i}",
			instructions=[],
			terminator=Goto(target=f"b{i+1}"),
		)
	blocks[f"b{n-1}"] = BasicBlock(
		name=f"b{n-1}",
		instructions=[],
		terminator=Return(value=None),
	)
	return MirFunc(
		fn_id=FunctionId(module="main", name="deep_chain", ordinal=0),
		name="deep_chain",
		params=[],
		locals=[],
		blocks=blocks,
		entry="b0",
	)


def test_has_backedge_deep_linear_chain_no_recursion_error() -> None:
	"""Linear chain of 5000 blocks must classify as acyclic without crashing."""
	func = _build_linear_chain(5000)
	lowering = MirToSSA()
	# Should return False (no backedge) and must not raise RecursionError.
	assert lowering._has_backedge(func) is False


def test_has_backedge_deep_self_loop_detected() -> None:
	"""A backedge at the end of a deep chain must still be detected."""
	func = _build_linear_chain(5000)
	# Replace the terminal Return with a Goto back to b0 to create a single
	# huge cycle.
	last = f"b{len(func.blocks) - 1}"
	func.blocks[last] = BasicBlock(
		name=last,
		instructions=[],
		terminator=Goto(target="b0"),
	)
	lowering = MirToSSA()
	assert lowering._has_backedge(func) is True


def test_compute_block_order_diamond_visitation_is_deterministic() -> None:
	"""Pin RPO on a branched CFG to the same order the recursive DFS produced.

	When `_compute_block_order` was converted from recursive to iterative DFS
	(robustness matrix row #6), a naive LIFO push reversed successor visitation
	order on branched CFGs. The original recursive walk's order is now
	explicitly preserved by pushing successors in reverse onto the stack.
	This test pins that behavior so it cannot silently regress.

	Diamond shape:
	    entry → then → join
	          ↘ else ↗
	With successors in declaration order [then, else], the recursive walk
	does:
	  dfs(entry) → dfs(then) → dfs(join) → post=[join]; post=[join,then];
	  dfs(else) → join already visited; post=[join,then,else];
	  post=[join,then,else,entry]; rpo=reversed=[entry,else,then,join].
	The iterative walker must produce the same RPO.
	"""
	from lang.driftc.stage2 import IfTerminator

	entry = BasicBlock(
		name="entry", instructions=[],
		terminator=IfTerminator(cond="c", then_target="then", else_target="else"),
	)
	then = BasicBlock(name="then", instructions=[], terminator=Goto(target="join"))
	else_block = BasicBlock(name="else", instructions=[], terminator=Goto(target="join"))
	join = BasicBlock(name="join", instructions=[], terminator=Return(value=None))
	func = MirFunc(
		fn_id=FunctionId(module="main", name="diamond", ordinal=0),
		name="diamond",
		params=[],
		locals=[],
		blocks={"entry": entry, "then": then, "else": else_block, "join": join},
		entry="entry",
	)
	rpo = MirToSSA()._compute_block_order(func)
	assert rpo == ["entry", "else", "then", "join"], f"unexpected RPO: {rpo}"


def test_dominance_frontier_deep_chain_no_recursion_error() -> None:
	"""DominanceFrontierAnalysis must not RecursionError on a deep linear CFG.

	Same root cause family as the SSA backedge test above: stage4 has multiple
	recursive DFS walkers, each must independently handle user-controlled
	depth without overflowing the Python recursion limit. This test pins the
	`dom.py` post-order walker.
	"""
	from lang.driftc.stage4.dom import DominanceFrontierAnalysis, DominatorAnalysis

	func = _build_linear_chain(5000)
	dom_info = DominatorAnalysis().compute(func)
	df_info = DominanceFrontierAnalysis().compute(func, dom_info)
	# Linear chain has no joins, so every block's frontier is empty.
	for b, frontier in df_info.df.items():
		assert frontier == set(), f"block {b} unexpected frontier {frontier}"
