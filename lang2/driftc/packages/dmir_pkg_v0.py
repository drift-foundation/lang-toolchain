# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
DMIR-PKG container (v0).

This is a tiny, deterministic, streaming-friendly package container suitable
for signing. It matches Drift's trust model:
- packages are distribution containers (may contain many modules),
- module payloads are content-addressed by sha256,
- loaders verify hashes before trusting bytes.

This module intentionally does **not** implement signature verification yet.
It focuses on a stable byte layout and integrity verification.
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

MAGIC = b"DMIRPKG\0"
VERSION = 0
TOC_ENTRY_SIZE_V0 = 80
HEADER_RESERVED_LEN = 64

# Header layout:
# magic(8), version(u16), flags(u16), header_size(u32), manifest_len(u64),
# manifest_sha256(32), toc_len(u64), toc_entry_size(u32), toc_sha256(32),
# reserved(64)
_HEADER_STRUCT = struct.Struct("<8sHHI Q 32s Q I 32s 64s")
HEADER_SIZE_V0 = _HEADER_STRUCT.size

# TOC entry layout (80 bytes):
# blob_sha256(32), offset(u64), length(u64), type(u16), flags(u16),
# name_len(u32), name(24)
_TOC_ENTRY_STRUCT = struct.Struct("<32sQQHHI24s")


def sha256_bytes(data: bytes) -> bytes:
	"""Return sha256 digest bytes for `data`."""
	return hashlib.sha256(data).digest()


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
class TocEntry:
	"""A table-of-contents entry describing a blob region."""

	sha256: str
	offset: int
	length: int
	type: int
	name: str


@dataclass(frozen=True)
class PackageModule:
	"""Loaded module entry: decoded JSON interface + payload objects."""

	module_id: str
	interface: dict[str, Any]
	payload: dict[str, Any]


@dataclass(frozen=True)
class LoadedPackage:
	"""A fully verified, decoded package container."""

	path: Path
	manifest: dict[str, Any]
	toc: list[TocEntry]
	modules_by_id: dict[str, PackageModule]
	blobs_by_sha256: dict[str, bytes]


def _encode_name_prefix(name: str) -> tuple[int, bytes]:
	"""
	Encode a name for the fixed-length TOC `name` field.

We record a deterministic prefix (up to 24 bytes of UTF-8).
The full name is carried in the manifest when needed.
	"""
	data = name.encode("utf-8")
	if len(data) > 24:
		return 24, data[:24]
	return len(data), data


def write_dmir_pkg_v0(path: Path, *, manifest_obj: Mapping[str, Any], blobs: Mapping[str, bytes], blob_types: Mapping[str, int], blob_names: Mapping[str, str]) -> None:
	"""
	Write a DMIR-PKG v0 container.

`blobs` is a mapping of sha256 hex -> bytes.
`blob_types` provides a numeric type for each blob sha.
`blob_names` provides a human-oriented name for each blob sha (stored as a 24B prefix in TOC).
	"""
	manifest_bytes = canonical_json_bytes(dict(manifest_obj))
	manifest_len = len(manifest_bytes)
	manifest_hash = sha256_bytes(manifest_bytes)

	# Deterministic blob ordering: sha256 bytewise ascending.
	sha_list = sorted(blobs.keys())
	for sha in sha_list:
		if sha not in blob_types:
			raise ValueError(f"missing blob type for sha256 {sha}")

	# Compute offsets for blob region.
	blob_region_start = HEADER_SIZE_V0 + manifest_len + (len(sha_list) * TOC_ENTRY_SIZE_V0)
	cur = blob_region_start
	entry_bytes: list[bytes] = []
	entries: list[tuple[str, int, int, int, str]] = []
	for sha in sha_list:
		data = blobs[sha]
		typ = int(blob_types[sha])
		name = blob_names.get(sha, "")
		name_len, name_prefix = _encode_name_prefix(name)
		name_field = name_prefix.ljust(24, b"\0")
		blob_sha_bytes = bytes.fromhex(sha)
		entry_bytes.append(_TOC_ENTRY_STRUCT.pack(blob_sha_bytes, cur, len(data), typ, 0, name_len, name_field))
		entries.append((sha, cur, len(data), typ, name))
		cur += len(data)

	toc_bytes = b"".join(entry_bytes)
	toc_hash = sha256_bytes(toc_bytes)

	header = _HEADER_STRUCT.pack(
		MAGIC,
		VERSION,
		0,
		HEADER_SIZE_V0,
		manifest_len,
		manifest_hash,
		len(sha_list),
		TOC_ENTRY_SIZE_V0,
		toc_hash,
		b"\0" * HEADER_RESERVED_LEN,
	)

	path.parent.mkdir(parents=True, exist_ok=True)
	with path.open("wb") as f:
		f.write(header)
		f.write(manifest_bytes)
		f.write(toc_bytes)
		for sha in sha_list:
			f.write(blobs[sha])


def _read_exact(f, n: int) -> bytes:
	data = f.read(n)
	if len(data) != n:
		raise ValueError("unexpected EOF while reading package")
	return data


def load_dmir_pkg_v0(path: Path) -> LoadedPackage:
	"""
	Load a DMIR-PKG v0 container and verify integrity.

Verification steps:
	- header magic/version/entry sizes
	- manifest sha256 matches header
	- toc sha256 matches header
	- each blob sha256 matches TOC entry
	- blob offsets are in-range and non-overlapping
	- manifest references match TOC (strict: TOC must not contain unreferenced blobs)
	"""
	with path.open("rb") as f:
		header_bytes = _read_exact(f, HEADER_SIZE_V0)
		(
			magic,
			version,
			flags,
			header_size,
			manifest_len,
			manifest_sha,
			toc_len,
			toc_entry_size,
			toc_sha,
			_reserved,
		) = _HEADER_STRUCT.unpack(header_bytes)
		if magic != MAGIC:
			raise ValueError("invalid package magic")
		if version != VERSION:
			raise ValueError(f"unsupported package version {version}")
		if flags != 0:
			raise ValueError("unsupported package flags")
		if header_size != HEADER_SIZE_V0:
			raise ValueError("unsupported header size")
		if toc_entry_size != TOC_ENTRY_SIZE_V0:
			raise ValueError("unsupported toc entry size")

		manifest_bytes = _read_exact(f, int(manifest_len))
		if sha256_bytes(manifest_bytes) != manifest_sha:
			raise ValueError("manifest sha256 mismatch")

		toc_bytes = _read_exact(f, int(toc_len) * TOC_ENTRY_SIZE_V0)
		if sha256_bytes(toc_bytes) != toc_sha:
			raise ValueError("toc sha256 mismatch")

		# Parse TOC.
		entries: list[TocEntry] = []
		seen_shas: set[str] = set()
		for i in range(int(toc_len)):
			chunk = toc_bytes[i * TOC_ENTRY_SIZE_V0 : (i + 1) * TOC_ENTRY_SIZE_V0]
			blob_sha, offset, length, typ, _eflags, name_len, name_raw = _TOC_ENTRY_STRUCT.unpack(chunk)
			sha = blob_sha.hex()
			if sha in seen_shas:
				raise ValueError(f"duplicate blob sha256 in toc: {sha}")
			seen_shas.add(sha)
			name_prefix = name_raw[: int(name_len)]
			try:
				name = name_prefix.decode("utf-8")
			except Exception:
				name = ""
			entries.append(TocEntry(sha256=sha, offset=int(offset), length=int(length), type=int(typ), name=name))

		# Validate offsets are in-range and non-overlapping.
		file_size = path.stat().st_size
		sorted_by_offset = sorted(entries, key=lambda e: e.offset)
		prev_end = HEADER_SIZE_V0 + int(manifest_len) + (int(toc_len) * TOC_ENTRY_SIZE_V0)
		for e in sorted_by_offset:
			if e.offset < prev_end:
				raise ValueError("blob offsets overlap or point into header/manifest/toc")
			end = e.offset + e.length
			if end > file_size:
				raise ValueError("blob offset out of range")
			prev_end = end

		manifest_obj = json.loads(manifest_bytes.decode("utf-8"))
		if not isinstance(manifest_obj, dict):
			raise ValueError("manifest must be a JSON object")

		# Build sha -> bytes map and verify per-entry hashes.
		blobs_by_sha: dict[str, bytes] = {}
		for e in entries:
			f.seek(e.offset)
			data = _read_exact(f, e.length)
			if sha256_hex(data) != e.sha256:
				raise ValueError(f"blob sha256 mismatch for {e.sha256}")
			blobs_by_sha[e.sha256] = data

		# Cross-check manifest <-> toc.
		manifest_blobs = manifest_obj.get("blobs")
		if not isinstance(manifest_blobs, dict):
			raise ValueError("manifest missing 'blobs' map")
		manifest_blob_shas = {k.split("sha256:", 1)[1] if isinstance(k, str) and k.startswith("sha256:") else str(k) for k in manifest_blobs.keys()}
		toc_blob_shas = {e.sha256 for e in entries}
		missing = manifest_blob_shas - toc_blob_shas
		extra = toc_blob_shas - manifest_blob_shas
		if missing:
			raise ValueError(f"manifest references missing blob(s): {sorted(missing)}")
		if extra:
			raise ValueError(f"toc contains unreferenced blob(s): {sorted(extra)}")

		# Decode modules.
		modules_by_id: dict[str, PackageModule] = {}
		mod_list = manifest_obj.get("modules")
		if not isinstance(mod_list, list):
			raise ValueError("manifest missing 'modules' list")
		for m in mod_list:
			if not isinstance(m, dict):
				raise ValueError("invalid module entry in manifest")
			mid = m.get("module_id")
			if not isinstance(mid, str):
				raise ValueError("module entry missing module_id")
			iface_ref = m.get("interface_blob")
			payload_ref = m.get("payload_blob")
			if not isinstance(iface_ref, str) or not iface_ref.startswith("sha256:"):
				raise ValueError(f"module '{mid}' missing interface_blob reference")
			if not isinstance(payload_ref, str) or not payload_ref.startswith("sha256:"):
				raise ValueError(f"module '{mid}' missing payload_blob reference")
			iface_sha = iface_ref.split("sha256:", 1)[1]
			payload_sha = payload_ref.split("sha256:", 1)[1]
			iface_bytes = blobs_by_sha.get(iface_sha)
			payload_bytes = blobs_by_sha.get(payload_sha)
			if iface_bytes is None or payload_bytes is None:
				raise ValueError(f"module '{mid}' references missing blobs")
			interface_obj = json.loads(iface_bytes.decode("utf-8"))
			payload_obj = json.loads(payload_bytes.decode("utf-8"))
			if not isinstance(interface_obj, dict) or not isinstance(payload_obj, dict):
				raise ValueError(f"module '{mid}' blobs must decode to JSON objects")
			modules_by_id[mid] = PackageModule(module_id=mid, interface=interface_obj, payload=payload_obj)

	return LoadedPackage(
		path=path,
		manifest=dict(manifest_obj),
		toc=list(entries),
		modules_by_id=modules_by_id,
		blobs_by_sha256=blobs_by_sha,
	)
