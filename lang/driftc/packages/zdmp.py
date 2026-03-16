# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Compressed package distribution (.zdmp).

A .zdmp file is a zstd-compressed wrapper around the existing DMIR-PKG v0
container bytes.  Compression is the standard distribution form; the canonical
signed payload identity is always the *uncompressed* bytes.

Pinned compression settings:
- Algorithm:  zstd
- Level:      3
- Threads:    0  (single-threaded, deterministic output)
- Content size written into frame header (write_content_size=True)

Cache:
- Content-addressed by sha256 of the *uncompressed* bytes.
- Location: $DRIFT_CACHE_DIR/pkg/v0/<sha256>.dmp
  (default: ~/.cache/drift/pkg/v0/)
- Cache is populated after successful decompression + hash verification.
- Cache hit returns raw bytes without re-decompressing; signature
  verification still happens in the caller (load_package_v0_with_policy).
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

try:
	import zstandard
except ModuleNotFoundError as _err:
	raise ModuleNotFoundError(
		"zstandard is required to load compressed packages (.zdmp). "
		"Install it with: pip install 'zstandard>=0.23.0'"
	) from _err

# ── Pinned compression settings ──────────────────────────────────────

ZSTD_LEVEL = 3
ZSTD_THREADS = 0  # single-threaded for deterministic output


def compress_to_zdmp(raw_bytes: bytes) -> bytes:
	"""Compress raw DMIR-PKG bytes to .zdmp format (zstd)."""
	cctx = zstandard.ZstdCompressor(
		level=ZSTD_LEVEL,
		write_content_size=True,
		threads=ZSTD_THREADS,
	)
	return cctx.compress(raw_bytes)


def decompress_zdmp(compressed: bytes) -> bytes:
	"""Decompress .zdmp bytes to raw DMIR-PKG bytes."""
	dctx = zstandard.ZstdDecompressor()
	return dctx.decompress(compressed)


# ── Cache ─────────────────────────────────────────────────────────────


def _cache_base() -> Path:
	env = os.environ.get("DRIFT_CACHE_DIR")
	if env:
		return Path(env)
	return Path.home() / ".cache" / "drift"


def zdmp_cache_path(sha256_hex: str) -> Path:
	"""Return the cache path for uncompressed bytes with the given sha256."""
	return _cache_base() / "pkg" / "v0" / f"{sha256_hex}.dmp"


def load_zdmp_cached(zdmp_path: Path, expected_sha256: str | None = None) -> bytes:
	"""
	Load a .zdmp file, using the local cache when possible.

	1. If expected_sha256 is provided, check cache → return on hit.
	2. Decompress the .zdmp file.
	3. Verify sha256 matches expected (if provided).
	4. Write decompressed bytes to cache.
	5. Return raw bytes.

	Cache semantics: the cache is populated only after a successful
	decompress + hash verification.  On cache hit, the raw bytes are
	returned directly.  Signature verification is the caller's
	responsibility and still runs on every load (cache does not weaken
	trust).
	"""
	# 1. Cache hit.
	if expected_sha256:
		cached = zdmp_cache_path(expected_sha256)
		if cached.exists():
			return cached.read_bytes()

	# 2. Decompress.
	raw = decompress_zdmp(zdmp_path.read_bytes())

	# 3. Verify hash.
	actual_sha = hashlib.sha256(raw).hexdigest()
	if expected_sha256 and actual_sha != expected_sha256:
		raise ValueError(
			f"zdmp sha256 mismatch: expected {expected_sha256}, got {actual_sha}"
		)

	# 4. Write to cache (atomic: temp file + rename to avoid partial reads).
	cache_path = zdmp_cache_path(actual_sha)
	cache_path.parent.mkdir(parents=True, exist_ok=True)
	fd, tmp_path = tempfile.mkstemp(dir=cache_path.parent, suffix=".tmp")
	closed = False
	try:
		os.write(fd, raw)
		os.close(fd)
		closed = True
		os.replace(tmp_path, cache_path)
	except BaseException:
		if not closed:
			os.close(fd)
		try:
			os.unlink(tmp_path)
		except OSError:
			pass
		raise

	return raw


def clear_cache() -> int:
	"""Remove all cached decompressed packages.  Returns count of files removed."""
	cache_dir = _cache_base() / "pkg" / "v0"
	if not cache_dir.exists():
		return 0
	count = 0
	for f in cache_dir.iterdir():
		if f.is_file():
			f.unlink()
			count += 1
	return count
