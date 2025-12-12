# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-08
"""
HIR utilities that are shared across stage1/driver tests.

Right now we only expose catch-arm collection so the checker can validate
try/catch shapes using real HIR, not synthetic test shims.
"""

from __future__ import annotations

from typing import List

from lang2.driftc.stage1 import hir_nodes as H
from lang2.driftc.checker.catch_arms import CatchArmInfo
from lang2.driftc.core.span import Span


def collect_catch_arms_from_block(block: H.HBlock) -> List[CatchArmInfo]:
	"""
	Walk an HIR block and collect all catch arms (including nested try/catch).

	This keeps the checker aware of catch-arm shapes when validating against
	the exception catalog. Only syntactic info (event FQN) is gathered here.
	"""
	arms: List[CatchArmInfo] = []

	def collect_block(b: H.HBlock) -> None:
		for stmt in b.statements:
			collect_stmt(stmt)

	def collect_stmt(stmt: H.HStmt) -> None:
		if isinstance(stmt, H.HTry):
			for arm in stmt.catches:
				assert isinstance(arm.loc, Span)
				arms.append(CatchArmInfo(event_fqn=arm.event_fqn, span=arm.loc))
				collect_block(arm.block)
			collect_block(stmt.body)
		elif isinstance(stmt, H.HIf):
			collect_block(stmt.then_block)
			if stmt.else_block is not None:
				collect_block(stmt.else_block)
		elif isinstance(stmt, H.HLoop):
			collect_block(stmt.body)
		# Other statements do not contain nested blocks that can host catch arms.

	collect_block(block)
	return arms


__all__ = ["collect_catch_arms_from_block"]
