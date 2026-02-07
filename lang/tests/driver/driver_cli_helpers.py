# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.codegen.llvm.test_utils import host_word_bits


def with_target_word_bits(argv: list[str]) -> list[str]:
	"""
	Inject --target-word-bits for driftc CLI invocations in tests.

	Production requires explicit target layout; tests default to host width.
	"""
	if "--target-word-bits" in argv:
		return argv
	return [*argv, "--target-word-bits", str(host_word_bits())]
