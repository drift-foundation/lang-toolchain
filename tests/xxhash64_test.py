from lang.xxhash64 import hash64


def test_xxhash64_known_value() -> None:
    # ABI guard: hash of a known FQN must stay stable.
    assert hash64(b"drift.lang:Example") == 0x96c3346e5058b4d3
