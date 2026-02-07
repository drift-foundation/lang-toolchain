# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Pure-Python xxHash64 (seed=0) used for stable event-code derivation.

Copied from lang/ to avoid cross-package dependencies. ABI note: do not change
without coordinating the exception event-code ABI.
"""

from __future__ import annotations

PRIME1 = 11400714785074694791
PRIME2 = 14029467366897019727
PRIME3 = 1609587929392839161
PRIME4 = 9650029242287828579
PRIME5 = 2870177450012600261
MASK64 = 0xFFFFFFFFFFFFFFFF


def _rotl(x: int, r: int) -> int:
	return (((x << r) & MASK64) | (x >> (64 - r))) & MASK64


def hash64(data: bytes, seed: int = 0) -> int:
	"""Compute xxHash64 of the given bytes with the provided seed (default 0)."""
	length = len(data)
	idx = 0
	# --- Main loop (32-byte chunks) ---
	if length >= 32:
		v1 = (seed + PRIME1 + PRIME2) & MASK64
		v2 = (seed + PRIME2) & MASK64
		v3 = seed & MASK64
		v4 = (seed - PRIME1) & MASK64
		while idx <= length - 32:
			v1 = (v1 + int.from_bytes(data[idx:idx + 8], "little") * PRIME2) & MASK64
			v1 = _rotl(v1, 31)
			v1 = (v1 * PRIME1) & MASK64
			idx += 8

			v2 = (v2 + int.from_bytes(data[idx:idx + 8], "little") * PRIME2) & MASK64
			v2 = _rotl(v2, 31)
			v2 = (v2 * PRIME1) & MASK64
			idx += 8

			v3 = (v3 + int.from_bytes(data[idx:idx + 8], "little") * PRIME2) & MASK64
			v3 = _rotl(v3, 31)
			v3 = (v3 * PRIME1) & MASK64
			idx += 8

			v4 = (v4 + int.from_bytes(data[idx:idx + 8], "little") * PRIME2) & MASK64
			v4 = _rotl(v4, 31)
			v4 = (v4 * PRIME1) & MASK64
			idx += 8

		acc = (_rotl(v1, 1) + _rotl(v2, 7) + _rotl(v3, 12) + _rotl(v4, 18)) & MASK64
	else:
		acc = (seed + PRIME5) & MASK64

	acc = (acc + length) & MASK64

	# --- Tail: 8-byte lanes ---
	while idx + 8 <= length:
		k1 = int.from_bytes(data[idx:idx + 8], "little")
		k1 = (k1 * PRIME2) & MASK64
		k1 = _rotl(k1, 31)
		k1 = (k1 * PRIME1) & MASK64
		acc ^= k1
		acc &= MASK64
		acc = (_rotl(acc, 27) * PRIME1 + PRIME4) & MASK64
		idx += 8

	# --- Tail: 4-byte lane ---
	if idx + 4 <= length:
		k1 = int.from_bytes(data[idx:idx + 4], "little")
		acc ^= (k1 * PRIME1) & MASK64
		acc &= MASK64
		acc = (_rotl(acc, 23) * PRIME2 + PRIME3) & MASK64
		idx += 4

	# --- Tail: remaining bytes ---
	while idx < length:
		k1 = data[idx]
		acc ^= (k1 * PRIME5) & MASK64
		acc &= MASK64
		acc = (_rotl(acc, 11) * PRIME1) & MASK64
		idx += 1

	# --- Final mix ---
	acc ^= acc >> 33
	acc = (acc * PRIME2) & MASK64
	acc ^= acc >> 29
	acc = (acc * PRIME3) & MASK64
	acc ^= acc >> 32
	return acc
