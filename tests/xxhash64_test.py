from lang.xxhash64 import hash64


def test_xxhash64_known_value() -> None:
	# ABI guard: hash of a known FQN must stay stable.
	assert hash64(b"drift.lang:Example") == 0xAD1CB9AE2AF72564
