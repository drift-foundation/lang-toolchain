# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

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


@pytest.fixture(scope="session")
def pex_scie_base(tmp_path_factory: pytest.TempPathFactory) -> Path:
	"""Shared scie extraction cache for PEX deploy tests.

	The scie launcher extracts its embedded Python interpreter (~440 MB)
	on first invocation.  Sharing the cache across all PEX tests within
	a worker avoids duplicating this extraction per test.
	"""
	return tmp_path_factory.mktemp("pex_scie_base")
