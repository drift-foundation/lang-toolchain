# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Stage1 HIR normalization helpers.

At this stage the only normalization we perform is desugaring result-driven
try sugar (`HTryResult`) into explicit HIR using the TryResultRewriter.

This helper is meant to be called by the driver after AST→HIR and before
HIR→MIR so that sugar does not leak into later stages.

Type-awareness: the current rewrite is structural. Once the checker/type
environment is wired in, this helper should only be applied to HTryResult
nodes whose operand is known to be `FnResult<_, Error>` and where the
`is_err`/`unwrap`/`unwrap_err` methods exist. Keeping the helper here
makes the integration point explicit.
"""

from __future__ import annotations

from . import hir_nodes as H
from .borrow_materialize import BorrowMaterializeRewriter
from .try_result_rewrite import TryResultRewriter


def normalize_hir(block: H.HBlock) -> H.HBlock:
	"""
	Normalize an HIR block by desugaring result-driven try sugar.

	Currently runs TryResultRewriter to eliminate HTryResult nodes.
	Additional normalization passes can be added here as needed.
	"""
	# Order matters:
	# 1) Expand try-result sugar first so later passes only see core nodes.
	# 2) Materialize shared borrows of rvalues into temps so borrow checking and
	#    MIR lowering can treat borrow operands as places.
	block = TryResultRewriter().rewrite_block(block)
	block = BorrowMaterializeRewriter().rewrite_block(block)
	return block
