# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import struct


def host_word_bits() -> int:
	"""Return the host pointer width for tests until targets are plumbed."""
	return struct.calcsize("P") * 8
