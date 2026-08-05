#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Run fingerprint for the ownership corpus (resume/promote authority).

Two identities:
  * TOOLCHAIN fingerprint — everything that can change a COMPILE RESULT, ALL
    resolved through the single corpus_compile_contract (so compilation and
    fingerprinting never approximate it differently): all compile-relevant
    source under lang/ (minus tests/caches), the stdlib tree, the audit-tool
    sources + schema, the PRE-BUILT runtime archive (production authority, bytes
    hashed), the tool binaries (python/clang/ar/linker: path + EXECUTABLE BYTES
    + version), the native link libraries (-lz, debug-lane) identity, the
    platform, the exact driftc argv template, and the NORMALIZED compile
    environment.
  * RUN SNAPSHOT — toolchain fingerprint + the static-universe digest.  The
    compiling lanes (check/verify) compare it at START and FINISH of a run;
    fast promotion recomputes it ONCE, passively, for candidate identity.

How the lanes use these differs by design:
  * VERIFY requires a stable start==end run snapshot and produces exhaustive
    fresh evidence — the toolchain fingerprint is load-bearing there (it
    PREBUILDS the runtime through the production authority before
    fingerprinting).  PROMOTE probes the same identity WITHOUT building
    anything: `toolchain_fingerprint_passive` resolves and hashes the
    already-existing runtime artifact and fails toward a fresh verify when
    it is absent or stale.
  * The DEVELOPER cache keys each fixture record by its CONTENT HASH alone, so a
    toolchain-fingerprint move does NOT invalidate the cache (no full rebuild);
    the toolchain composite recorded on a record is used only to CLASSIFY it as a
    current observation (same toolchain) versus a projected/stale one (older
    toolchain).  Only new / edited / selected fixtures recompile.

fingerprint.json is CANONICAL, SCHEMA-VERSIONED, ATOMIC, and carries NO
timestamps/PIDs/scratch paths in the hashed body (TMPDIR is a stable
placeholder).  A git rev may appear ONLY under `diagnostic`.  read_fingerprint
validates shape + hex digests and RECOMPUTES the composite (tamper-evident).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from pathlib import Path

FINGERPRINT_SCHEMA_VERSION = 3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _contract():
	"""Load the single compile-contract authority (avoids a hard import cycle
	and keeps the tool file list in one place)."""
	spec = importlib.util.spec_from_file_location(
		"corpus_compile_contract", ROOT / "tools" / "corpus_compile_contract.py")
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	return mod


_AUDIT_TOOL_FILES = ("drift_corpus_audit.py", "drift_corpus_fingerprint.py",
                     "corpus_compile_contract.py", "drift_corpus_check.py")


def _record_schema_version(root: Path) -> str:
	"""The check tool's RECORD_SCHEMA_VERSION, read WITHOUT importing it (the
	check module imports this one — importing back would cycle).  Folded into the
	audit-tool stamp so a bump in cache/projection record semantics invalidates
	old records even beyond the raw source hash."""
	try:
		text = (root / "tools" / "drift_corpus_check.py").read_text()
	except OSError:
		return "MISSING"
	m = re.search(r"^RECORD_SCHEMA_VERSION\s*=\s*(\d+)", text, re.MULTILINE)
	return m.group(1) if m else "UNKNOWN"


# ── deterministic tree / file hashing ────────────────────────────────

def hash_tree(root_dir: Path, *, exclude_rel_prefixes=(), exclude_suffixes=()) -> str:
	"""Content hash over a tree: sorted (posix-relpath, sha256) pairs.  Excludes
	given top-relative path prefixes, any '__pycache__' segment, and suffixes.
	A missing dir hashes as the empty tree."""
	h = hashlib.sha256()
	if not root_dir.exists():
		return h.hexdigest()
	entries: list[tuple[str, Path]] = []
	for p in sorted(root_dir.rglob("*")):
		if not p.is_file():
			continue
		relparts = p.relative_to(root_dir).parts
		if any(part == "__pycache__" for part in relparts):
			continue
		rel = "/".join(relparts)
		if any(rel == pre or rel.startswith(pre + "/") for pre in exclude_rel_prefixes):
			continue
		if p.suffix in exclude_suffixes:
			continue
		entries.append((rel, p))
	for rel, p in entries:
		h.update(rel.encode())
		h.update(b"\0")
		h.update(hashlib.sha256(p.read_bytes()).hexdigest().encode())
		h.update(b"\0")
	return h.hexdigest()


def _file_sha256(p: Path) -> "str | None":
	try:
		return hashlib.sha256(p.read_bytes()).hexdigest()
	except OSError:
		return None


def _compile_source_digest(root: Path) -> str:
	"""ALL compile-relevant source under lang/ — driftc, codegen, the C runtime
	sources/headers, compiler_infra, etc. — EXCLUDING tests and caches."""
	return hash_tree(root / "lang", exclude_rel_prefixes=("tests",),
	                 exclude_suffixes=(".pyc", ".pyo"))


def _stdlib_digest(root: Path) -> str:
	return hash_tree(root / "stdlib", exclude_suffixes=(".pyc", ".pyo"))


def _audit_tool_digest(root: Path) -> str:
	"""Hash the corpus audit/fingerprint/contract tool SOURCES + their schema
	versions, so a change to parsing/aggregation/record-layer behavior (or the
	contract) invalidates cached records — these live under tools/, outside the
	lang/ compile-source hash."""
	cc = _contract()
	h = hashlib.sha256()
	for name in _AUDIT_TOOL_FILES:
		h.update(name.encode())
		h.update(b"\0")
		h.update((_file_sha256(root / "tools" / name) or "MISSING").encode())
		h.update(b"\0")
	h.update(f"contract={cc.CONTRACT_SCHEMA_VERSION};fp={FINGERPRINT_SCHEMA_VERSION}"
	         f";record={_record_schema_version(root)}".encode())
	return h.hexdigest()


# ── tool binary identities (path + EXECUTABLE BYTES + version) ────────

def _bin_identity(resolved: "str | None") -> dict:
	"""Resolved path + executable-BYTES hash + `--version` hash.  Bytes catch a
	rebuilt binary whose --version is unchanged; version catches a bump the
	bytes-path might miss (symlinks, wrappers)."""
	if resolved is None:
		return {"path": None, "bytes_sha256": None, "version_sha256": None}
	entry = {"path": resolved, "bytes_sha256": _file_sha256(Path(resolved)),
	         "version_sha256": None}
	try:
		out = subprocess.run([resolved, "--version"], capture_output=True,
		                     text=True, timeout=30)
		entry["version_sha256"] = hashlib.sha256(
			((out.stdout or "") + (out.stderr or "")).encode()).hexdigest()
	except (OSError, subprocess.SubprocessError):
		pass
	return entry


def _python_identity() -> dict:
	return {
		"executable": sys.executable,
		"executable_bytes_sha256": _file_sha256(Path(sys.executable)) if sys.executable else None,
		"version": sys.version,
		"implementation": sys.implementation.name,
		"cache_tag": sys.implementation.cache_tag,
	}


def _tool_identities(extra_args) -> dict:
	cc = _contract()
	linker = cc.resolve_linker(extra_args)
	return {
		"python": _python_identity(),
		"clang": _bin_identity(cc.resolve_clang()),
		"ar": _bin_identity(cc.resolve_ar()),
		"linker": {"selection": linker["selection"], **_bin_identity(linker["path"])},
		"platform": {
			"system": platform.system(),
			"machine": platform.machine(),
			"platform": platform.platform(),
		},
	}


# ── native link libraries (availability can flip compile outcomes) ───

def _native_lib_identity(lib: str) -> dict:
	"""Resolve `lib<lib>.so` via the SAME production authority driftc links with
	(link_selection.resolve_native_lib_path: find_library + fixed search dirs) —
	NOT clang -print-file-name, which searches different directories.  If the
	library is UNAVAILABLE, resolved=None/bytes=None; that flip changes the
	fingerprint, so a lib appearing/disappearing invalidates cached results."""
	from lang.driftc import link_selection
	soname = f"lib{lib}.so"
	entry = {"soname": soname, "resolved": None, "bytes_sha256": None}
	path = link_selection.resolve_native_lib_path(lib)
	if path and Path(path).is_file():
		entry["resolved"] = path
		entry["bytes_sha256"] = _file_sha256(Path(path))
	return entry


def _native_libs_identity(debug_style: bool) -> dict:
	cc = _contract()
	return {lib: _native_lib_identity(lib) for lib in cc.native_link_libs(debug_style)}


# ── runtime archive (resolved + prebuilt through the production authority) ──

def prebuild_runtime(root: Path, extra_args=()) -> dict:
	"""Resolve the corpus runtime variant via the CONTRACT (honoring `--sanitize`
	in extra_args, so the prebuilt/hashed archive is the one driftc links),
	PREBUILD it through the production authority BEFORE the fingerprint is taken,
	and record variant / archive relpath / bytes hash / cache-root settings."""
	from lang.language_runtime import build_runtime_archive, runtime_archive_cache_root
	cc = _contract()
	variant = cc.runtime_variant(extra_args)
	clang = cc.resolve_clang()
	if clang is None:
		raise RuntimeError("clang not available — cannot prebuild the runtime archive")
	archive_path = Path(build_runtime_archive(root, clang=clang, variant=variant))
	cache_root = runtime_archive_cache_root(root)
	return {
		"variant": variant,
		"archive_relpath": str(archive_path.relative_to(cache_root))
		                   if _is_relative_to(archive_path, cache_root)
		                   else archive_path.name,
		"archive_sha256": _file_sha256(archive_path),
		"cache_root_settings": {
			"DRIFT_RUNTIME_LIB_CACHE_DIR": os.environ.get("DRIFT_RUNTIME_LIB_CACHE_DIR"),
			"DRIFT_RUNTIME_BUILD_ROOT": os.environ.get("DRIFT_RUNTIME_BUILD_ROOT"),
		},
	}


def resolve_runtime_identity(root: Path, extra_args=()) -> dict:
	"""PASSIVE twin of `prebuild_runtime` for the fast-or-fail promotion
	path: resolve and hash the EXISTING runtime archive without ever
	invoking the build authority.  Raises RuntimeError when the artifact is
	absent — the caller must fail and request a fresh
	`ownership-corpus-verify` (which prebuilds via the production
	authority), never build here.

	Staleness is CONTENT IDENTITY, not cache mtime: the archive's bytes
	hash joins the toolchain composite alongside the compile-source/stdlib
	digests (which cover the runtime sources), so a source change flips
	the composite and the candidate identity check rejects — while a
	content-identical archive with a touched mtime, or a deployed
	read-only archive, promotes fine.  No freshness heuristic is copied
	from the build authority."""
	from lang.language_runtime import (runtime_archive_cache_root,
	                                   runtime_archive_path)
	cc = _contract()
	variant = cc.runtime_variant(extra_args)
	archive_path = runtime_archive_path(root, variant=variant)
	if not archive_path.is_file():
		raise RuntimeError(
			f"runtime archive {archive_path} does not exist; promotion never "
			f"builds — run `ownership-corpus-verify` first")
	archive_sha = _file_sha256(archive_path)
	if not _is_hex64(archive_sha):
		# Existing-but-unreadable (or racing) artifact: fail closed rather
		# than letting a None/garbage identity join the composite.
		raise RuntimeError(
			f"runtime archive {archive_path} could not be hashed; promotion "
			f"never builds — run `ownership-corpus-verify`")
	cache_root = runtime_archive_cache_root(root)
	return {
		"variant": variant,
		"archive_relpath": str(archive_path.relative_to(cache_root))
		                   if _is_relative_to(archive_path, cache_root)
		                   else archive_path.name,
		"archive_sha256": archive_sha,
		"cache_root_settings": {
			"DRIFT_RUNTIME_LIB_CACHE_DIR": os.environ.get("DRIFT_RUNTIME_LIB_CACHE_DIR"),
			"DRIFT_RUNTIME_BUILD_ROOT": os.environ.get("DRIFT_RUNTIME_BUILD_ROOT"),
		},
	}


def _is_relative_to(p: Path, base: Path) -> bool:
	try:
		p.relative_to(base)
		return True
	except ValueError:
		return False


# ── composite + snapshot (pure) ──────────────────────────────────────

def canonical_json(obj) -> str:
	return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def composite_hash(components: dict) -> str:
	return hashlib.sha256(canonical_json(components).encode()).hexdigest()


def collect_toolchain_components(root: Path, *, extra_args, runtime=None) -> dict:
	"""Every compile-result-relevant input, ALL via the single contract.
	Side-effecting by default: PREBUILDS the runtime through the production
	authority (verify/check) and runs read-only `--version` /
	`-print-file-name` probes.  A caller that must never build (fast
	promotion) passes a precomputed PASSIVE `runtime` identity from
	`resolve_runtime_identity` instead."""
	cc = _contract()
	if runtime is None:
		runtime = prebuild_runtime(root, extra_args)
	return {
		"contract_schema": cc.CONTRACT_SCHEMA_VERSION,
		"compile_source": _compile_source_digest(root),
		"stdlib": _stdlib_digest(root),
		"audit_tool": _audit_tool_digest(root),
		"runtime": runtime,
		# native libs are keyed on DRIFT_DEBUG (debug backtrace libs), NOT on the
		# sanitizer variant — matching driftc's own gating.
		"native_libs": _native_libs_identity(cc.debug_style()),
		"tools": _tool_identities(extra_args),
		"driftc_argv_template": cc.driftc_argv_template(extra_args),
		"env": cc.fingerprint_env(),
	}


def toolchain_fingerprint(root: Path, *, extra_args=(), git_rev: "str | None" = None) -> dict:
	components = collect_toolchain_components(root, extra_args=extra_args)
	return {
		"schema_version": FINGERPRINT_SCHEMA_VERSION,
		"kind": "toolchain",
		"components": components,
		"composite": composite_hash(components),
		"diagnostic": {"git_rev": git_rev},
	}


def toolchain_fingerprint_passive(root: Path, *, extra_args=(), git_rev: "str | None" = None) -> dict:
	"""Fingerprint the CURRENT toolchain without building anything: the
	runtime identity comes from `resolve_runtime_identity` (existing
	artifact only; RuntimeError when missing/unreadable — stale CONTENT
	surfaces as a composite mismatch downstream).  Identical output shape
	to `toolchain_fingerprint` — an unchanged tree yields the SAME
	composite, which is exactly what fast promotion's identity check
	needs."""
	components = collect_toolchain_components(
		root, extra_args=extra_args,
		runtime=resolve_runtime_identity(root, extra_args))
	return {
		"schema_version": FINGERPRINT_SCHEMA_VERSION,
		"kind": "toolchain",
		"components": components,
		"composite": composite_hash(components),
		"diagnostic": {"git_rev": git_rev},
	}


def _run_snapshot_body(toolchain_composite: str, static_universe_digest: str) -> dict:
	return {
		"toolchain_composite": toolchain_composite,
		"static_universe_digest": static_universe_digest,
	}


def run_snapshot(toolchain_fp: dict, static_universe_digest: str) -> dict:
	body = _run_snapshot_body(toolchain_fp["composite"], static_universe_digest)
	return {
		"schema_version": FINGERPRINT_SCHEMA_VERSION,
		"kind": "run_snapshot",
		"toolchain": toolchain_fp,
		"static_universe_digest": static_universe_digest,
		"composite": composite_hash(body),
	}


def static_universe_digest(universe: dict) -> str:
	body = {
		"inclusion_rule": universe.get("inclusion_rule"),
		"fixtures": sorted((f["name"], f["sha256"]) for f in universe.get("fixtures", [])),
		"excluded": sorted((e["name"], e.get("reason")) for e in universe.get("excluded", [])),
	}
	return hashlib.sha256(canonical_json(body).encode()).hexdigest()


# ── canonical, atomic, integrity-checked I/O ─────────────────────────

def write_atomic(path: Path, fp: dict) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	text = json.dumps(fp, sort_keys=True, indent=2) + "\n"
	fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".fp-", suffix=".tmp")
	try:
		with os.fdopen(fd, "w") as fh:
			fh.write(text)
		os.replace(tmp, path)
	finally:
		if os.path.exists(tmp):
			os.unlink(tmp)


def _is_hex64(s) -> bool:
	return isinstance(s, str) and len(s) == 64 and all(c in "0123456789abcdef" for c in s)


_TOOLCHAIN_KEYS = {"schema_version", "kind", "components", "composite", "diagnostic"}
_SNAPSHOT_KEYS = {"schema_version", "kind", "toolchain", "static_universe_digest", "composite"}
# Exact component shape a toolchain fingerprint must carry.  Value: a predicate
# the component must satisfy (structural — not a value assertion).
_COMPONENT_SHAPE = {
	"contract_schema": lambda v: isinstance(v, int) and not isinstance(v, bool),
	"compile_source": _is_hex64,
	"stdlib": _is_hex64,
	"audit_tool": _is_hex64,
	"runtime": lambda v: isinstance(v, dict) and "variant" in v,
	"native_libs": lambda v: isinstance(v, dict),
	"tools": lambda v: isinstance(v, dict),
	"driftc_argv_template": lambda v: isinstance(v, list),
	"env": lambda v: isinstance(v, dict),
}


def _validate_components(components) -> None:
	if set(components) != set(_COMPONENT_SHAPE):
		missing = sorted(set(_COMPONENT_SHAPE) - set(components))
		extra = sorted(set(components) - set(_COMPONENT_SHAPE))
		raise ValueError(f"toolchain components shape wrong "
		                 f"(missing={missing}, unexpected={extra})")
	for key, ok in _COMPONENT_SHAPE.items():
		if not ok(components[key]):
			raise ValueError(f"toolchain component {key!r} has the wrong shape/type")


def validate_fingerprint(data) -> dict:
	"""Full integrity check: EXACT shape (exact key set — no unknown AND no
	missing keys), hex digests, validated component/run-snapshot shapes, correct
	kind, and a RECOMPUTED composite (tamper-evident).  Raises ValueError on any
	violation so callers fail closed."""
	if not isinstance(data, dict):
		raise ValueError("fingerprint is not an object")
	if data.get("schema_version") != FINGERPRINT_SCHEMA_VERSION:
		raise ValueError(f"fingerprint schema {data.get('schema_version')} != {FINGERPRINT_SCHEMA_VERSION}")
	kind = data.get("kind")
	if kind == "toolchain":
		if set(data) != _TOOLCHAIN_KEYS:
			raise ValueError(f"toolchain fingerprint key set {sorted(set(data))} "
			                 f"!= required {sorted(_TOOLCHAIN_KEYS)}")
		if not isinstance(data["components"], dict):
			raise ValueError("toolchain components not an object")
		_validate_components(data["components"])
		if not _is_hex64(data["composite"]):
			raise ValueError("toolchain composite not a hex digest")
		if composite_hash(data["components"]) != data["composite"]:
			raise ValueError("toolchain composite mismatch — components tampered")
	elif kind == "run_snapshot":
		if set(data) != _SNAPSHOT_KEYS:
			raise ValueError(f"run_snapshot key set {sorted(set(data))} "
			                 f"!= required {sorted(_SNAPSHOT_KEYS)}")
		if not isinstance(data["toolchain"], dict):
			raise ValueError("run_snapshot missing embedded toolchain")
		if not _is_hex64(data.get("static_universe_digest")):
			raise ValueError("run_snapshot static_universe_digest not a hex digest")
		if not _is_hex64(data["composite"]):
			raise ValueError("run_snapshot composite not a hex digest")
		validate_fingerprint(data["toolchain"])  # embedded toolchain must also be intact
		body = _run_snapshot_body(data["toolchain"]["composite"], data["static_universe_digest"])
		if composite_hash(body) != data["composite"]:
			raise ValueError("run_snapshot composite mismatch — tampered")
	else:
		raise ValueError(f"unknown fingerprint kind: {kind!r}")
	return data


def read_fingerprint(path: Path) -> dict:
	return validate_fingerprint(json.loads(path.read_text()))
