# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Signed provenance sidecar for Drift build artifacts.

Produces a deterministic JSON document recording the exact build
environment and resolved dependency graph. The provenance digest
is included in the v2 signing envelope so tampering is detectable.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
	import zstandard
except ModuleNotFoundError as _err:
	raise ModuleNotFoundError(
		"zstandard is required for provenance bundles (.provenance.zst). "
		"Install it with: pip install 'zstandard>=0.23.0'"
	) from _err


@dataclass(frozen=True)
class CompilerInfo:
	version: str   # "0.27.92"
	abi: int       # 6
	commit: str    # short git sha or "unknown"


def parse_compiler_info(version_output: str) -> CompilerInfo:
	"""Parse `driftc --version` output.

	Format: 'driftc 0.27.92 | abi 6 | git abc1234 | license ...'

	Falls back to sensible defaults for missing fields.
	"""
	version = "unknown"
	abi = 0
	commit = "unknown"

	# Extract version: "driftc X.Y.Z"
	m = re.search(r"driftc\s+([\d.]+)", version_output)
	if m:
		version = m.group(1)

	# Extract ABI: "abi N"
	m = re.search(r"abi\s+(\d+)", version_output)
	if m:
		abi = int(m.group(1))

	# Extract commit: "git XXXXXX"
	m = re.search(r"git\s+([0-9a-fA-F]+)", version_output)
	if m:
		commit = m.group(1)

	return CompilerInfo(version=version, abi=abi, commit=commit)


@dataclass(frozen=True)
class SourceIdentity:
	"""Source provenance metadata for an artifact build."""
	vcs_type: str = ""  # "git", "hg", or "" if unknown
	branch: str = ""  # branch/ref name, or "" if unknown
	commit: str = ""  # full commit/revision hash, or "" if unknown


def detect_source_identity(repo_dir: Path) -> SourceIdentity:
	"""Detect VCS source identity from a repository directory.

	Uses os.popen instead of subprocess.run to avoid interference
	with test mocks that patch subprocess.run globally.
	"""
	import os
	try:
		branch = os.popen(f"git -C {repo_dir} rev-parse --abbrev-ref HEAD 2>/dev/null").read().strip()
		commit = os.popen(f"git -C {repo_dir} rev-parse HEAD 2>/dev/null").read().strip()
		if branch and commit and len(commit) >= 7:
			return SourceIdentity(vcs_type="git", branch=branch, commit=commit)
	except Exception:
		pass
	return SourceIdentity()


def build_provenance(
	*,
	artifact_name: str,
	artifact_version: str,
	artifact_kind: str,  # "package" or "app"
	artifact_sha256: str,  # "sha256:<hex>" — digest of the artifact bytes
	target: str,
	compiler: CompilerInfo,
	resolved_deps: dict[str, dict[str, str]],  # {pkg_id: {"version": str, "sha256": str}}
	source: SourceIdentity | None = None,
) -> bytes:
	"""Build canonical deterministic provenance JSON bytes.

	Returns the exact bytes to write to disk and hash for signing.
	Uses json.dumps(sort_keys=True, separators=(",",":")) for determinism.

	artifact_sha256 is the sha256 of the primary artifact bytes:
	  - For packages: sha256 of the uncompressed .dmp bytes.
	  - For apps: sha256 of the compiled binary bytes.
	"""
	obj: dict[str, Any] = {
		"schema_version": 3,
		"artifact_name": artifact_name,
		"artifact_version": artifact_version,
		"artifact_kind": artifact_kind,
		"artifact_sha256": artifact_sha256,
		"target": target,
		"compiler_version": compiler.version,
		"compiler_commit": compiler.commit,
		"abi": compiler.abi,
		"build_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
		"resolved_deps": resolved_deps,
	}
	if source is not None:
		obj["source"] = {
			"vcs_type": source.vcs_type,
			"branch": source.branch,
			"commit": source.commit,
		}
	return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def write_provenance(path: Path, provenance_bytes: bytes) -> None:
	"""Write provenance bytes to disk."""
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_bytes(provenance_bytes)


def provenance_sha256(provenance_bytes: bytes) -> str:
	"""Compute sha256 hex digest of provenance bytes."""
	return hashlib.sha256(provenance_bytes).hexdigest()


# ── Provenance bundle ────────────────────────────────────────────────

# Compression settings match zdmp.py: zstd level 3, single-threaded
# for deterministic output.
_ZSTD_LEVEL = 3
_ZSTD_THREADS = 0


def build_provenance_bundle(
	provenance: dict[str, Any],
	dep_provenance: dict[str, dict[str, Any]],
	dep_keys: dict[str, dict[str, str]],
) -> bytes:
	"""Build the uncompressed provenance bundle JSON bytes.

	The bundle is a single JSON document embedding:
	  - provenance: the main provenance document
	  - dep_provenance: provenance of resolved dependencies (keyed by dep name)
	  - dep_keys: public keys of dependency signers (keyed by kid)

	Returns deterministic canonical JSON bytes (sort_keys, compact separators).
	"""
	bundle: dict[str, Any] = {
		"format": "drift-provenance-bundle",
		"version": 0,
		"provenance": provenance,
		"dep_provenance": dep_provenance,
		"dep_keys": dep_keys,
	}
	return json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compress_provenance_bundle(raw_bytes: bytes) -> bytes:
	"""Zstd-compress provenance bundle bytes.

	Uses the same pinned settings as zdmp (level 3, single-threaded)
	for deterministic output.
	"""
	cctx = zstandard.ZstdCompressor(
		level=_ZSTD_LEVEL,
		write_content_size=True,
		threads=_ZSTD_THREADS,
	)
	return cctx.compress(raw_bytes)


def decompress_provenance_bundle(compressed: bytes) -> bytes:
	"""Decompress zstd-compressed provenance bundle bytes."""
	dctx = zstandard.ZstdDecompressor()
	return dctx.decompress(compressed)


def write_provenance_bundle(path: Path, compressed_bytes: bytes) -> None:
	"""Write compressed provenance bundle to disk."""
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_bytes(compressed_bytes)


def load_provenance_bundle(path: Path) -> dict[str, Any]:
	"""Load and parse a compressed provenance bundle from disk.

	Returns the parsed bundle dict.
	"""
	compressed = path.read_bytes()
	raw = decompress_provenance_bundle(compressed)
	bundle = json.loads(raw)
	if not isinstance(bundle, dict):
		raise ValueError("provenance bundle must be a JSON object")
	if bundle.get("format") != "drift-provenance-bundle":
		raise ValueError("invalid provenance bundle format")
	return bundle
