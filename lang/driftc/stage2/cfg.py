# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Central MIR control-flow-graph successor contract.

Single source of truth for "given a block / terminator, which basic blocks may it
branch to".  Every MIR pass that walks the CFG (drop & liveness dataflow in
`ownership_ledger`/`string_arc`, cleanup authoring, SSA, dominance) consults these
helpers (which delegate to `MTerminator.successors()` / `.successor_edges()`)
instead of hand-rolling `isinstance(term, Goto) → … ; isinstance(term, IfTerminator)
→ …` dispatch.

Why this exists: each hand-written successor walker is a place a new or changed
terminator can be silently missed, making a pass treat reachable code as dead or
leave cleanup/liveness/phi-placement incomplete — a class of silent miscompile this
codebase has hit repeatedly. Routing every CFG user through one contract means a new
terminator is handled everywhere the moment it implements `successors()`.
"""

from __future__ import annotations

from typing import Optional

from . import mir_nodes as M


def terminator_successors(term: Optional[M.MTerminator]) -> list[str]:
	"""Block names *term* may branch to (stable order). `None` → `[]`.

	This is the authoritative per-terminator dispatch; passes should call this
	rather than re-deriving successors from the terminator type."""
	if term is None:
		return []
	return term.successors()


def terminator_successor_edges(term: Optional[M.MTerminator]) -> list[tuple[str, str]]:
	"""`(target_block, edge_label)` pairs for *term* (stable order). `None` → `[]`.

	Use this where the outgoing-edge identity matters (e.g. cleanup edge-splitting
	distinguishing the if-then vs if-else edge)."""
	if term is None:
		return []
	return term.successor_edges()


def terminator_value_uses(term: Optional[M.MTerminator]) -> list[str]:
	"""ValueIds *term* reads (if-cond, switch-scrutinee, return-value). `None` → `[]`.

	Liveness/use scanners call this so a value consumed only by a terminator stays
	live to the block end — and so block-name targets are never mistaken for
	values."""
	if term is None:
		return []
	return term.value_uses()


def block_successors(block: "M.BasicBlock") -> list[str]:
	"""Successor block names of *block* (via its terminator)."""
	return terminator_successors(getattr(block, "terminator", None))


def compute_successors(func: "M.MirFunc") -> dict[str, list[str]]:
	"""Map every block name → its successor block names."""
	return {name: terminator_successors(blk.terminator) for name, blk in func.blocks.items()}


def compute_predecessors(func: "M.MirFunc") -> dict[str, list[str]]:
	"""Map every block name → the block names that branch to it.

	Predecessors are listed once per CFG edge (a block that reaches a target via
	two edges — e.g. an `if` whose both arms target the same block — appears
	twice), matching the edge multiplicity callers expect."""
	preds: dict[str, list[str]] = {name: [] for name in func.blocks}
	for name, blk in func.blocks.items():
		for succ in terminator_successors(blk.terminator):
			preds.setdefault(succ, []).append(name)
	return preds
