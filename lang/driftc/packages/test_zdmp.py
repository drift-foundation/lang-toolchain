# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Tests for the zdmp compression/decompression/cache module.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from lang.driftc.packages.zdmp import (
	clear_cache,
	compress_to_zdmp,
	decompress_zdmp,
	load_zdmp_cached,
	zdmp_cache_path,
)


class TestCompressDecompress:
	def test_round_trip(self) -> None:
		"""compress → decompress → identical bytes."""
		raw = b"DMIRPKG\0" + b"\x00" * 200 + b"hello world payload"
		compressed = compress_to_zdmp(raw)
		assert compressed != raw
		assert len(compressed) < len(raw)
		result = decompress_zdmp(compressed)
		assert result == raw

	def test_empty_round_trip(self) -> None:
		raw = b""
		compressed = compress_to_zdmp(raw)
		assert decompress_zdmp(compressed) == raw

	def test_deterministic_output(self) -> None:
		"""Same input always produces same compressed bytes."""
		raw = b"deterministic test data" * 100
		c1 = compress_to_zdmp(raw)
		c2 = compress_to_zdmp(raw)
		assert c1 == c2


class TestCache:
	def test_cache_path_default(self) -> None:
		with patch.dict("os.environ", {}, clear=True):
			# Remove DRIFT_CACHE_DIR so default is used.
			import os
			env = {k: v for k, v in os.environ.items() if k != "DRIFT_CACHE_DIR"}
			with patch.dict("os.environ", env, clear=True):
				p = zdmp_cache_path("aabb")
				assert p.name == "aabb.dmp"
				assert "pkg" in str(p) and "v0" in str(p)

	def test_cache_path_respects_env(self) -> None:
		with patch.dict("os.environ", {"DRIFT_CACHE_DIR": "/tmp/test-drift-cache"}):
			p = zdmp_cache_path("aabb")
			assert str(p).startswith("/tmp/test-drift-cache")
			assert p.name == "aabb.dmp"

	def test_load_zdmp_cached_populates_cache(self) -> None:
		"""First load decompresses and writes cache; second reads from cache."""
		raw = b"test package data " * 50
		sha = hashlib.sha256(raw).hexdigest()
		compressed = compress_to_zdmp(raw)

		with tempfile.TemporaryDirectory() as tmpdir:
			cache_dir = Path(tmpdir) / "cache"
			zdmp_file = Path(tmpdir) / "test.zdmp"
			zdmp_file.write_bytes(compressed)

			with patch.dict("os.environ", {"DRIFT_CACHE_DIR": str(cache_dir)}):
				# First load — cache miss, decompresses.
				result = load_zdmp_cached(zdmp_file, expected_sha256=sha)
				assert result == raw

				# Cache file should exist.
				cached = zdmp_cache_path(sha)
				assert cached.exists()
				assert cached.read_bytes() == raw

				# Remove zdmp to prove second load uses cache.
				zdmp_file.unlink()
				result2 = load_zdmp_cached(zdmp_file, expected_sha256=sha)
				assert result2 == raw

	def test_load_zdmp_cached_no_expected_sha(self) -> None:
		"""Without expected_sha256, always decompresses (no cache lookup)."""
		raw = b"no-hash data " * 30
		compressed = compress_to_zdmp(raw)

		with tempfile.TemporaryDirectory() as tmpdir:
			cache_dir = Path(tmpdir) / "cache"
			zdmp_file = Path(tmpdir) / "test.zdmp"
			zdmp_file.write_bytes(compressed)

			with patch.dict("os.environ", {"DRIFT_CACHE_DIR": str(cache_dir)}):
				result = load_zdmp_cached(zdmp_file, expected_sha256=None)
				assert result == raw
				# Cache should still be populated (keyed by actual hash).
				sha = hashlib.sha256(raw).hexdigest()
				assert zdmp_cache_path(sha).exists()

	def test_sha256_mismatch_raises(self) -> None:
		raw = b"some data"
		compressed = compress_to_zdmp(raw)

		with tempfile.TemporaryDirectory() as tmpdir:
			cache_dir = Path(tmpdir) / "cache"
			zdmp_file = Path(tmpdir) / "test.zdmp"
			zdmp_file.write_bytes(compressed)

			with patch.dict("os.environ", {"DRIFT_CACHE_DIR": str(cache_dir)}):
				with pytest.raises(ValueError, match="sha256 mismatch"):
					load_zdmp_cached(zdmp_file, expected_sha256="0000bad")

	def test_clear_cache(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			with patch.dict("os.environ", {"DRIFT_CACHE_DIR": str(tmpdir)}):
				# Populate some cache files.
				cache_base = Path(tmpdir) / "pkg" / "v0"
				cache_base.mkdir(parents=True)
				(cache_base / "aabb.dmp").write_bytes(b"data1")
				(cache_base / "ccdd.dmp").write_bytes(b"data2")

				count = clear_cache()
				assert count == 2
				assert not any(cache_base.iterdir())

	def test_clear_cache_empty(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			with patch.dict("os.environ", {"DRIFT_CACHE_DIR": str(tmpdir)}):
				count = clear_cache()
				assert count == 0
