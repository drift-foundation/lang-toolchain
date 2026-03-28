# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Structured errors from MIR lowering that should be surfaced as compiler diagnostics."""


class MirLoweringError(Exception):
	"""An error during MIR lowering that should be reported as a user-facing diagnostic."""
	pass
