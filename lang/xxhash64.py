"""Pure-Python xxHash64 (seed=0) for deterministic event-code derivation.

This is a minimal implementation sufficient for hashing small strings in the
compiler. It follows the reference xxhash64 algorithm with 64-bit modular
arithmetic.

ABI NOTE: This implementation and seed are part of the Drift ABI for exception
codes. Do not change without an ABI version bump.
"""

from __future__ import annotations

PRIME1 = 11400714785074694791
PRIME2 = 14029467366897019727
PRIME3 = 1609587929392839161
PRIME4 = 9650029242287828579
PRIME5 = 2870177450012600261
MASK64 = 0xFFFFFFFFFFFFFFFF


def _rotl(x: int, r: int) -> int:
    return ((x << r) & MASK64) | (x >> (64 - r))


def hash64(data: bytes, seed: int = 0) -> int:
    """Compute xxHash64 of the given bytes with the provided seed (default 0)."""
    length = len(data)
    idx = 0
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

    while idx + 8 <= length:
        lane = int.from_bytes(data[idx:idx + 8], "little")
        acc ^= (_rotl((lane * PRIME2) & MASK64, 31) * PRIME1) & MASK64
        acc = (_rotl(acc, 27) * PRIME1 + PRIME4) & MASK64
        idx += 8

    if idx + 4 <= length:
        lane = int.from_bytes(data[idx:idx + 4], "little")
        acc ^= (lane * PRIME1) & MASK64
        acc = (_rotl(acc, 23) * PRIME2 + PRIME3) & MASK64
        idx += 4

    while idx < length:
        lane = data[idx]
        acc ^= (lane * PRIME5) & MASK64
        acc = (_rotl(acc, 11) * PRIME1) & MASK64
        idx += 1

    acc ^= acc >> 33
    acc = (acc * PRIME2) & MASK64
    acc ^= acc >> 29
    acc = (acc * PRIME3) & MASK64
    acc ^= acc >> 32
    return acc
