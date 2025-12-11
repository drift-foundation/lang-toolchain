# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Event-code hashing helpers shared by the lang2 front end.

We reuse the lang v1 scheme: xxHash64 of the fully-qualified exception name
module:path plus a 4-bit domain tag in the high bits. Collisions are treated as
fatal diagnostics by callers; this module only computes codes.
"""

from __future__ import annotations

from lang.xxhash64 import hash64  # ABI-stable hash used in lang/

# High 4 bits encode the "exception" domain; low 60 bits carry the hash payload.
EVENT_DOMAIN_TAG = 0b0001 << 60
PAYLOAD_MASK = (1 << 60) - 1


def event_code(fqn: str) -> int:
	"""
	Compute the stable event code for a fully-qualified exception name.

	Args:
	  fqn: fully-qualified name, e.g., "module.path:ExceptionName".

	Returns:
	  64-bit int with domain tag in the high nibble and xxhash64 payload in the
	  low 60 bits.
	"""
	payload = hash64(fqn.encode("utf-8")) & PAYLOAD_MASK
	return EVENT_DOMAIN_TAG | payload
