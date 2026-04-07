# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import os
import struct


def host_word_bits() -> int:
	"""Return the host pointer width for tests until targets are plumbed."""
	return struct.calcsize("P") * 8


def sanitizer_timeout(base: int) -> int:
	"""Inflate a subprocess timeout when a sanitizer mode is active.

	Use this only for the concrete subprocess invocations that wrap driftc or
	driftc-built binaries and have been observed to exceed their default budget
	under DRIFT_ASAN=1 / DRIFT_UBSAN=1. Do NOT apply blanket inflation to
	unrelated timeouts.
	"""
	if os.environ.get("DRIFT_ASAN") in ("1", "true", "True"):
		return base * 3
	if os.environ.get("DRIFT_UBSAN") in ("1", "true", "True"):
		return base * 3
	return base
