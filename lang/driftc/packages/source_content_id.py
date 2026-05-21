# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Canonical `source_content_id` computation (v1 trust model).

`source_content_id` is the deterministic hash of a package's source
identity: kind, package_id, version, module_namespace, entry_module,
sorted module (path, sha256) tuples, sorted package_deps (name,
version_range) tuples, sorted native_deps, unsafe flag, sorted
assets (path, sha256).

It is signed by the author in `.author-claim` (binding "I, the
author, authorized this exact source release") and re-stated by the
certifier in `.cert-claim` (binding "I, the certifier, built and
attested an artifact from this exact source identity").  The
verifier compares the three stamps (author claim, package manifest,
cert claim) at load time WITHOUT recomputing from binary — only
self-verify mode rebuilds source and recomputes.

What's IN the canonical hash:
- identity (kind, package_id, version)
- namespace + entry module
- source module set (paths + content shas)
- declared dep ranges (NAMES + VERSION RANGES — not resolved
  versions; the resolver's choices are NOT part of source identity)
- native dep names
- unsafe flag
- asset paths + content shas

What's OUT (non-canonical):
- build epoch, compiler version, ABI fingerprint
- absolute paths, file mtimes
- compiler-produced payload bytes
- signatures
- **target / build class** (e.g. `drift-dev`, `library`, `app`).
  Target is a CERTIFIER concern (which build environment produced
  the artifact bytes) and lives on `cert_claim.body.target`, not
  on source identity.  One author claim can therefore cover the
  same source release across multiple build targets, while each
  target/artifact gets its own cert claim.  See trust-v1.md
  §3 (role split) and the project_target_class_removed memory.

Anything that varies across legitimate rebuilds of the same source
is excluded by construction.

This module is the canonical home for SCI in the v1 trust model.
The `tools/drift_deploy/source_attestation.py` duplicate that
existed during the additive transition was deleted in slice 4;
all callers now route through `compute_artifact_source_content_id`
(or the `tools/drift_deploy/build_cmd.compute_artifact_sci`
manifest-aware wrapper).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SHA256_BARE_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_PREFIXED_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


# ── Internal helpers ───────────────────────────────────────────────


def _validate_sha256_bare_hex(s: Any, *, field: str) -> str:
	"""Bare 64-lowercase-hex form (no `sha256:` prefix) — used for
	per-module / per-asset content shas inside the canonical body
	since the prefix would just repeat 64 times."""
	if not isinstance(s, str):
		raise ValueError(f"{field}: must be a string, got {type(s).__name__}")
	if not _SHA256_BARE_HEX_RE.fullmatch(s):
		raise ValueError(
			f"{field}: must be 64 lowercase hex chars; got {s!r}"
		)
	return s


def validate_sci(s: Any, *, field: str = "source_content_id") -> str:
	"""Validate a prefixed `sha256:<hex>` source_content_id string.

	Public helper — callers carrying an SCI through trust verification
	can validate shape before comparison so they don't accidentally
	match a malformed value.
	"""
	if not isinstance(s, str):
		raise ValueError(f"{field}: must be a string, got {type(s).__name__}")
	if not _SHA256_PREFIXED_RE.fullmatch(s):
		raise ValueError(
			f"{field}: must be 'sha256:<64-lowercase-hex>'; got {s!r}"
		)
	return s


def canonical_json_bytes(obj: Any) -> bytes:
	"""Canonical JSON serialization — sorted keys, compact separators,
	UTF-8.  Matches the pattern used by `provenance.py::build_provenance`
	and `lockfile.py::write_lock` so determinism is consistent across
	all signed/hashed artifacts.

	Re-exported under this name so the v1 trust module set has a
	single canonical encoder; callers in `author_claim_v1` and
	`cert_claim_v1` sign over `canonical_json_bytes(body)`.
	"""
	return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
	return hashlib.sha256(data).hexdigest()


def _normalize_canonical_path(p: str) -> str:
	"""Normalize a project-relative path for canonical hashing.

	Pinned policy:
	- backslashes → forward slashes
	- no absolute paths (must not start with `/`)
	- no `.` or `..` segments
	- no leading `./`
	- no trailing slash
	- no empty path

	Any path that fails these rules is rejected at canonicalisation
	time so an absolute or escape-style path can't sneak into a
	signed source identity (which would let two different source
	trees collide on `source_content_id` by aliasing absolute paths).
	"""
	if not isinstance(p, str) or not p:
		raise ValueError("canonical path must be a non-empty string")
	q = p.replace("\\", "/")
	while q.startswith("./"):
		q = q[2:]
	q = q.rstrip("/")
	if not q:
		raise ValueError(f"canonical path is empty after normalisation: {p!r}")
	if q.startswith("/"):
		raise ValueError(f"canonical path must be project-relative, not absolute: {p!r}")
	for seg in q.split("/"):
		if seg in ("", ".", ".."):
			raise ValueError(
				f"canonical path may not contain '.', '..', or empty segments: {p!r}"
			)
	return q


# ── Public types ───────────────────────────────────────────────────


@dataclass(frozen=True)
class SourceContentInputs:
	"""Stable build inputs that determine source identity.

	Field names mirror the manifest exactly so the canonical schema
	is auditable against `tools/drift_deploy/manifest.py::Artifact`:
	`kind`, `name` (→ `package_id` for the package manifest),
	`version`, `module_namespace`, `entry_module`, `modules`,
	`package_deps`, `native_deps`, `assets`, `unsafe`.

	**No `target_class`.**  Target/build environment is certifier
	metadata (`cert_claim.body.target`), not source identity.
	"""
	kind: str  # "library" or "app"
	package_id: str
	version: str
	module_namespace: str
	entry_module: str
	modules: list[tuple[str, str]]       # [(relative_path, sha256_hex), ...]
	package_deps: list[tuple[str, str]]  # [(name, version_range), ...]
	native_deps: list[str]
	unsafe: bool
	assets: list[tuple[str, str]]        # [(relative_path, sha256_hex), ...]


# ── Public computation ────────────────────────────────────────────


def _reject_duplicates(items: list[tuple[str, str]], *, field: str, key_label: str) -> None:
	"""Reject duplicate first-tuple-entries in a (key, value) list.

	Used for modules / assets (`path`, `sha256`) and package_deps
	(`name`, `version_range`).  The canonical SCI sort is by key
	only; equal keys with different values would make the
	canonical bytes depend on input ORDER for tie-broken entries
	(stable sort), even though they look canonical.  Fail closed.
	"""
	seen: set[str] = set()
	for k, _v in items:
		if k in seen:
			raise ValueError(
				f"{field}: duplicate {key_label} {k!r} — the canonical "
				f"sort is by {key_label} only, so duplicates would make "
				f"the signed bytes order-dependent.  Each {key_label} "
				f"must appear exactly once."
			)
		seen.add(k)


def compute_source_content_id(inputs: SourceContentInputs) -> str:
	"""Compute the canonical, deterministic `source_content_id`.

	Returns `"sha256:<hex>"`.  The same inputs always yield the same
	id; different inputs (different module bytes, reordered deps with
	different content, etc.) yield different ids.  Order of inputs in
	`modules`, `package_deps`, `native_deps`, `assets` is normalized
	(sorted) inside this function so callers don't have to.

	Duplicate keys (same module path / asset path / dep name) are
	rejected: the canonical sort tie-breaks would otherwise be
	input-order-sensitive.

	Path safety: every path in `modules`, `entry_module`, and
	`assets` is run through `_normalize_canonical_path`, which
	rejects absolute paths, `..` segments, and empty entries.  This
	guarantees the signed identity references project-local source
	only.
	"""
	_reject_duplicates(list(inputs.modules), field="modules", key_label="path")
	_reject_duplicates(list(inputs.assets), field="assets", key_label="path")
	_reject_duplicates(list(inputs.package_deps), field="package_deps", key_label="name")
	# native_deps is a flat list of strings; reject simple duplicates too.
	seen_native: set[str] = set()
	for n in inputs.native_deps:
		if n in seen_native:
			raise ValueError(
				f"native_deps: duplicate native dep {n!r}; each must "
				f"appear exactly once"
			)
		seen_native.add(n)
	canonical = {
		"schema_version": 1,
		"kind": inputs.kind,
		"package_id": inputs.package_id,
		"version": inputs.version,
		"module_namespace": inputs.module_namespace,
		"entry_module": _normalize_canonical_path(inputs.entry_module),
		"modules": sorted(
			[
				{
					"path": _normalize_canonical_path(p),
					"sha256": _validate_sha256_bare_hex(s, field=f"modules[{p!r}].sha256"),
				}
				for (p, s) in inputs.modules
			],
			key=lambda e: e["path"],
		),
		"package_deps": sorted(
			[{"name": n, "version": v} for (n, v) in inputs.package_deps],
			key=lambda e: e["name"],
		),
		"native_deps": sorted(inputs.native_deps),
		"unsafe": bool(inputs.unsafe),
		"assets": sorted(
			[
				{
					"path": _normalize_canonical_path(p),
					"sha256": _validate_sha256_bare_hex(s, field=f"assets[{p!r}].sha256"),
				}
				for (p, s) in inputs.assets
			],
			key=lambda e: e["path"],
		),
	}
	return "sha256:" + _sha256_hex(canonical_json_bytes(canonical))


def hash_file(path: Path) -> str:
	"""sha256 hex of a file's exact bytes.  For module/asset content."""
	h = hashlib.sha256()
	with path.open("rb") as f:
		for chunk in iter(lambda: f.read(65536), b""):
			h.update(chunk)
	return h.hexdigest()


def _resolve_source_path(
	source_root: Path, rel: str, *, kind: str,
) -> Path:
	"""Resolve `source_root / rel` and require the resolved path
	stays under `source_root`.

	Rejects symlinks (and parent-directory symlinks) that escape the
	project tree.  The threat: a project that includes
	`src/foo.drift -> /opt/upstream/foo.drift` would have SCI hash
	the upstream bytes under the logical project-relative path,
	letting an attacker who controls the upstream file silently
	change the project's SCI without touching anything inside the
	source tree.  v1 forbids this: every byte the SCI commits to
	must live under the declared source_root.

	`kind` is "module" or "asset" for the error message.
	"""
	full = source_root / rel
	if not full.is_file():
		raise FileNotFoundError(
			f"source {kind} '{rel}' not found at {full}; cannot compute "
			f"source_content_id"
		)
	# `resolve(strict=True)` follows every symlink in the chain and
	# raises FileNotFoundError if any link is dangling.  We then
	# require the resolved real path to live under source_root's
	# resolved real path.
	try:
		resolved = full.resolve(strict=True)
		root_resolved = source_root.resolve(strict=True)
		resolved.relative_to(root_resolved)
	except (FileNotFoundError, ValueError, OSError) as e:
		raise ValueError(
			f"source {kind} '{rel}' (full={full}) resolves outside the "
			f"declared source_root ({source_root}): {e}.  v1 does not "
			f"accept source/asset paths that escape the project tree, "
			f"even through symlinks -- the SCI would otherwise commit "
			f"to bytes that an attacker outside the project could "
			f"change without touching the project itself.  Either "
			f"materialize the file inside source_root, or restructure "
			f"the source_root to include the symlink target."
		)
	return full


def compute_artifact_source_content_id(
	*,
	kind: str,
	package_id: str,
	version: str,
	module_namespace: str,
	entry_module: str,
	module_paths: list[str],
	package_deps: list[tuple[str, str]],
	native_deps: list[str],
	unsafe: bool,
	asset_paths: list[str],
	source_root: Path,
) -> str:
	"""Compute `source_content_id` for an artifact by hashing its
	on-disk source/asset files.

	`source_root` is the project root that `module_paths` and
	`asset_paths` are relative to.

	**Stated semantics for symlinks (deliberate, not accidental):**

	- SCI hashes the **resolved target bytes** (`hash_file` opens
	  the file via Python's IO which follows symlinks).
	- SCI records the **logical project-relative path** (`rel`),
	  NOT the resolved real path.  Two source trees that produce
	  the same `rel -> bytes` mapping yield the same SCI even if
	  one uses regular files and the other uses in-tree symlinks
	  to alias content.

	- Symlinks that resolve OUTSIDE `source_root` are REJECTED
	  (`_resolve_source_path` raises `ValueError`).  Without that
	  rejection, an attacker controlling bytes outside the project
	  tree could silently change SCI by writing through a
	  project-internal symlink target.
	- Symlinks whose resolved real path stays INSIDE `source_root`
	  are permitted.  Use case: a project that aliases the same
	  source file under two logical paths for build purposes.  The
	  project owns the target's bytes; the alias is just a name.

	Missing files raise `FileNotFoundError` — silent dropping would
	let a deleted module produce a different id than the same source
	at consumption time, breaking the rebuild equivalence claim.
	Paths that resolve outside `source_root` raise `ValueError`.
	"""
	module_entries: list[tuple[str, str]] = []
	for rel in module_paths:
		full = _resolve_source_path(source_root, rel, kind="module")
		module_entries.append((rel, hash_file(full)))
	asset_entries: list[tuple[str, str]] = []
	for rel in asset_paths:
		full = _resolve_source_path(source_root, rel, kind="asset")
		asset_entries.append((rel, hash_file(full)))
	return compute_source_content_id(SourceContentInputs(
		kind=kind,
		package_id=package_id,
		version=version,
		module_namespace=module_namespace,
		entry_module=entry_module,
		modules=module_entries,
		package_deps=list(package_deps),
		native_deps=list(native_deps),
		unsafe=unsafe,
		assets=asset_entries,
	))
