# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Unsigned Drift package format (v0).

This implements a minimal, deterministic container for bundling modules into a
single artifact. It is designed to be:
- deterministic (byte-for-byte stable when inputs are the same),
- offline (no network I/O),
- forward-compatible (payload kind/version can evolve without changing the
  container shape).

Milestone 4 uses a provisional payload:
- payload_kind = "provisional-dmir"
- payload_version = 0
- unstable_format = true

The *payload* is intentionally unstable. The *container and manifest* aim to be
stable so the package ecosystem can solidify before canonical DMIR lands.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping


def sha256_hex(data: bytes) -> str:
	"""Return sha256 hex digest for `data`."""
	return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(obj: Any) -> bytes:
	"""
	Render JSON deterministically.

Rules:
	- UTF-8
	- no insignificant whitespace
	- stable key ordering
	"""
	return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class PackageBlob:
	"""
	A content-addressed blob included in a package.

	`sha256` is hex digest over the raw bytes.
	"""

	sha256: str
	data: bytes
	kind: str  # e.g. "json"


@dataclass(frozen=True)
class PackageModuleEntry:
	"""Manifest entry for a single module inside a package."""

	module_id: str
	interface_sha256: str
	payload_sha256: str


def _zipinfo(name: str) -> zipfile.ZipInfo:
	"""
	Create a ZipInfo with deterministic metadata.

	- fixed timestamp (Zip's earliest representable time)
	- no extra fields
	"""
	zi = zipfile.ZipInfo(filename=name)
	zi.date_time = (1980, 1, 1, 0, 0, 0)
	return zi


def write_unsigned_package(path: Path, *, manifest: Mapping[str, Any], blobs: Iterable[PackageBlob]) -> None:
	"""
	Write an unsigned package artifact as a single .zip-like file.

The archive is deterministic:
- entries are written in sorted order,
- entries use fixed timestamps,
- no compression (STORE) to avoid platform-dependent compression outputs.
	"""
	blob_list = list(blobs)
	blob_by_sha: Dict[str, PackageBlob] = {b.sha256: b for b in blob_list}
	if len(blob_by_sha) != len(blob_list):
		raise ValueError("duplicate blob sha256 in package")

	manifest_bytes = canonical_json_bytes(dict(manifest))

	# Create parent directory if needed.
	path.parent.mkdir(parents=True, exist_ok=True)

	with zipfile.ZipFile(path, mode="w") as zf:
		# Manifest first, then blobs.
		zf.writestr(_zipinfo("manifest.json"), manifest_bytes, compress_type=zipfile.ZIP_STORED)

		for sha in sorted(blob_by_sha.keys()):
			blob = blob_by_sha[sha]
			ext = "json" if blob.kind == "json" else "bin"
			arcname = f"blobs/{sha}.{ext}"
			zf.writestr(_zipinfo(arcname), blob.data, compress_type=zipfile.ZIP_STORED)


def build_unsigned_package(
	*,
	modules: Iterable[PackageModuleEntry],
	interfaces: Mapping[str, bytes],
	payloads: Mapping[str, bytes],
	created_by: str = "driftc",
) -> tuple[dict[str, Any], list[PackageBlob]]:
	"""
	Build a manifest + blob list for an unsigned package.

`interfaces` and `payloads` are raw JSON bytes (already canonicalized by caller).
	"""
	module_entries: list[PackageModuleEntry] = sorted(list(modules), key=lambda m: m.module_id)

	blobs: list[PackageBlob] = []
	for module_id, data in interfaces.items():
		blobs.append(PackageBlob(sha256=sha256_hex(data), data=data, kind="json"))
	for module_id, data in payloads.items():
		blobs.append(PackageBlob(sha256=sha256_hex(data), data=data, kind="json"))

	# Content-addressed: manifest references blob hashes only.
	manifest_modules: list[dict[str, str]] = []
	for m in module_entries:
		manifest_modules.append(
			{
				"module_id": m.module_id,
				"interface_sha256": m.interface_sha256,
				"payload_sha256": m.payload_sha256,
			}
		)

	manifest_obj: dict[str, Any] = {
		"format": 0,
		"kind": "drift-package",
		"unsigned": True,
		"created_by": created_by,
		"unstable_format": True,
		"payload_kind": "provisional-dmir",
		"payload_version": 0,
		"modules": manifest_modules,
		"blobs": sorted({b.sha256: {"sha256": b.sha256, "kind": b.kind, "size": len(b.data)} for b in blobs}.values(), key=lambda x: x["sha256"]),
	}
	return manifest_obj, blobs
