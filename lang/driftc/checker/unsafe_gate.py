# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.core.span import Span


def check_unsafe_call(
	*,
	allow_unsafe: bool,
	allow_unsafe_without_block: bool,
	unsafe_context: bool,
	trusted_module: bool,
	rawbuffer_only: bool,
	diagnostics: list,
	tc_diag,
	span: Span | None,
) -> bool:
	if rawbuffer_only and not trusted_module:
		diagnostics.append(tc_diag(message="raw buffer intrinsics are restricted to toolchain-trusted modules", severity="error", span=span or Span()))
		return False
	if trusted_module:
		return True
	if not allow_unsafe and not allow_unsafe_without_block:
		diagnostics.append(tc_diag(message="unsafe call requires --allow-unsafe", severity="error", span=span or Span()))
		return False
	if not unsafe_context and not allow_unsafe_without_block:
		diagnostics.append(tc_diag(message="unsafe call requires unsafe block", severity="error", span=span or Span()))
		return False
	return True
