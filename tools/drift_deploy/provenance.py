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
	"""Toolchain identity for provenance records. Populated from
	`driftc --version --json` (drift-toolchain-info/v1) via
	`lang.driftc.build_info.parse_toolchain_info` — the pipe-format
	parser was removed in the 0.33.93 clean break."""
	version: str   # "0.33.93"
	abi: int       # 22
	commit: str    # short git sha or "unknown"


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
	source_content_id: str,  # "sha256:<hex>" — v4: REQUIRED, the third SCI leg
	target: str,
	compiler: CompilerInfo,
	resolved_deps: dict[str, dict[str, str]],  # {pkg_id: {"version": str, "sha256": str}}
	source: SourceIdentity | None = None,
) -> bytes:
	"""Build canonical deterministic provenance JSON bytes (schema v4).

	Returns the exact bytes to write to disk and hash for signing.
	Uses json.dumps(sort_keys=True, separators=(",",":")) for determinism.

	artifact_sha256 is the sha256 of the primary artifact bytes:
	  - For packages: sha256 of the uncompressed .dmp bytes.
	  - For apps: sha256 of the compiled binary bytes.

	v4 (clean break): `source_content_id` is REQUIRED — it is the provenance
	leg of the three-way SCI equality (author == cert == provenance) that
	replaces the manifest leg for apps and reinforces it for packages.
	`artifact_kind` / `artifact_sha256` / `source_content_id` are
	cross-checked against the signed author + cert claims at verify time
	(no two-way fallback).
	"""
	# Provenance is now a SIGNED record whose `artifact_kind` /
	# `artifact_sha256` / `source_content_id` are cross-checked against the
	# author/cert claims at verify time.  Hold all three to the SAME canonical
	# policy the claims use, and fail early at the producer rather than emit a
	# signed-but-malformed bundle.
	from lang.driftc.packages.source_content_id import validate_sci
	if artifact_kind not in ("package", "app"):
		raise ValueError(
			f"build_provenance: artifact_kind must be 'package' or 'app' (v2); "
			f"got {artifact_kind!r}"
		)
	validate_sci(artifact_sha256, field="build_provenance artifact_sha256")
	validate_sci(source_content_id, field="build_provenance source_content_id")
	obj: dict[str, Any] = {
		"schema_version": 4,
		"artifact_name": artifact_name,
		"artifact_version": artifact_version,
		"artifact_kind": artifact_kind,
		"artifact_sha256": artifact_sha256,
		"source_content_id": source_content_id,
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
