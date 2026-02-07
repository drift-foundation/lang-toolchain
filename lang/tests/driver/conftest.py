# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
import pytest

from lang.codegen.llvm.test_utils import host_word_bits
from lang.driftc import driftc


@pytest.fixture(scope="session", autouse=True)
def _inject_target_word_bits_for_tests() -> None:
	"""
	Driver tests default to host word size unless explicitly specified.

	This keeps production code strict about target layout while allowing tests
	to avoid passing --target-word-bits everywhere.
	"""
	driftc._TEST_TARGET_WORD_BITS = host_word_bits()
