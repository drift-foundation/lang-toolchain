# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
drift/manifest.json — neutral parser + Artifact→SCI helper.

This module is the **shared** home for manifest parsing and the
Artifact→source_content_id mapping.  It is imported by:

  - `tools/drift_deploy/manifest.py` and `tools/drift_deploy/build_cmd.py`
    (the orch / deploy / cert pipeline);
  - `tools/drift_author/cli.py` (the author-side `drift-author publish`).

Both trees must share these helpers so the SCI in the on-disk
package manifest, the SCI in the author claim body, and the SCI
in the cert claim body all agree (trust-v1.md §3.5 three-way
equality).

The neutral location resolves a tension between two constraints:

  1. `tools/drift_author/*` must NOT import from `tools/drift_deploy/*`
     or `tools/deploy/*` (`test_author_module_does_not_import_orch_pipeline`)
     — the boundary keeps deploy state out of author tooling.
  2. The Artifact→SCI mapping must be byte-identical across drift
     author + drift deploy, so manifest parsing + SCI computation
     cannot live behind either side of that boundary alone.

Putting both helpers in `lang/driftc/packages/` (already the home
of the trust-format modules `source_content_id.py`,
`author_claim_v1.py`, `cert_claim_v1.py`) satisfies both: the
neutral package-format tree is free for either caller to import,
and `tools/drift_deploy/manifest.py` + `tools/drift_deploy/build_cmd.py`
remain in place as thin re-export shims for back-compat with the
many existing drift_deploy callers.

Three dep-naming boundaries, distinct by role:

- **manifest `package_deps`** — owner-authored source-of-truth
  declarations.  Each entry is `{name, version}` where `version` is
  the owner's declared acceptable range (`"M"` or `"M.N"`).  Lives
  in this file.
- **package `.dmp` `required_deps`** — published range requirements
  copied from `package_deps` at publish time.  Downstream `drift
  prepare` consumes `required_deps` to build the consumer's own
  exact lock.
- **lock `deps`** — exact resolved graph for this artifact: `M.N.P`
  + sha256 + author_key + dep_type.  Local to the artifact that
  owns the lock; never exported.

"This package REQUIRES these deps" (package/consumer contract) vs.
"this package LOCKS these deps" (local reproducibility answer) —
the two must not be conflated in downstream code.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


MANIFEST_SCHEMA_VERSION = 2

# v2 `package_deps[].version` is the package owner's **declared
# acceptable range** for that dependency.  Two forms are accepted:
#
#   "1"     → owner accepts any 1.x.x release
#   "1.4"   → owner accepts any 1.4.x release
#
# Any `M.N.P` exact pin, `^M.N.P` / `~M.N.P` range operator, or any
# other string is rejected at load time.  Exact versions live in
# `drift/lock.json`, not in the authored manifest.
#
# Drift does not decide semantic compatibility — the owner does, by
# choosing the range in this file.  The resolver only enforces that a
# selected candidate's exact version satisfies this declared range.
#
# `parse_constraint` in `semver.py` keeps a broader parser vocabulary
# (`^`/`~`/exact) for strictly internal, non-manifest use: lock-v3
# exact entries and the v1→v2 manifest migration path.  Pre-cut
# packages without v2 `required_deps` are clean-break rejected at
# consume time; there is no compatibility-shim path from `.dmp`-
# carried legacy metadata into the resolver.  The broader parser
# vocabulary must not reach this authored-manifest validator.
# Authored-manifest dep version shape is the same "owner-declared
# acceptable range" contract as the published `.dmp` `required_deps`
# field — delegate to the canonical validator in the package-format
# module.
from lang.driftc.packages.dmir_pkg_v0 import is_owner_declared_range


@dataclass(frozen=True)
class NativeDep:
	"""A native linker dependency."""
	lib: str


@dataclass(frozen=True)
class PackageDep:
	"""A Drift package dependency constraint."""
	name: str
	version: str  # semver constraint string


@dataclass(frozen=True)
class Artifact:
	"""A single artifact definition from the manifest."""
	kind: str  # "library" or "app" (legacy "package" normalized to "library")
	name: str
	version: str
	description: str
	license: str
	entry_module: str
	modules: list[str]
	package_deps: list[PackageDep] = field(default_factory=list)
	native_deps: list[NativeDep] = field(default_factory=list)
	assets: list[str] = field(default_factory=list)
	smoke_command: list[str] | None = None
	unsafe: bool = False
	module_namespace: str = ""  # set during parsing; defaults to name with hyphens → underscores
	entry_point: str = ""  # app-only: "module::fn" entry point (e.g. "pushcoin.bookkeeper::main")

	def __post_init__(self) -> None:
		if self.kind == "package":
			object.__setattr__(self, "kind", "library")


@dataclass(frozen=True)
class Project:
	"""Project-level metadata."""
	name: str
	license: str
	# Relative path to .author-profile file.  Optional in the manifest schema
	# (tools that only read metadata don't need it), but required by drift deploy
	# for all publishable projects.
	author_profile: str | None = None


@dataclass(frozen=True)
class Manifest:
	"""Parsed drift/manifest.json."""
	schema_version: int
	project: Project
	artifacts: list[Artifact]


class ManifestError(Exception):
	"""Raised on manifest validation failure."""
	pass


def load_manifest(path: Path) -> Manifest:
	"""
	Load and validate drift/manifest.json.

	Raises ManifestError on invalid manifest.
	"""
	if not path.exists():
		raise ManifestError(f"manifest not found: {path}")

	try:
		data = json.loads(path.read_text(encoding="utf-8"))
	except (json.JSONDecodeError, OSError) as e:
		raise ManifestError(f"failed to read manifest: {e}")

	if not isinstance(data, dict):
		raise ManifestError("drift/manifest.json must be a JSON object")

	# schema_version
	sv = data.get("schema_version")
	if sv == 1:
		raise ManifestError(
			f"manifest schema v1 is no longer supported at {path}; "
			"run `drift manifest migrate` to convert to v2.  v2 dep "
			"versions are the owner's declared acceptable range — "
			"`\"0.3\"` accepts any 0.3.x, `\"1\"` accepts any 1.x.x.  "
			"Exact resolved versions live in `drift/lock.json`, not in "
			"the manifest."
		)
	if sv != MANIFEST_SCHEMA_VERSION:
		raise ManifestError(f"unsupported schema_version: {sv} (expected {MANIFEST_SCHEMA_VERSION})")

	# project
	project_obj = data.get("project")
	if not isinstance(project_obj, dict):
		raise ManifestError("missing 'project' object")
	project_name = _require_str(project_obj, "name", "project")
	project_license = _require_str(project_obj, "license", "project")
	author_profile_raw = project_obj.get("author_profile")
	if author_profile_raw is not None:
		if not isinstance(author_profile_raw, str) or not author_profile_raw:
			raise ManifestError("'project.author_profile' must be a non-empty string path")
	project = Project(name=project_name, license=project_license, author_profile=author_profile_raw)

	# artifacts
	artifacts_arr = data.get("artifacts")
	if not isinstance(artifacts_arr, list) or len(artifacts_arr) == 0:
		raise ManifestError("'artifacts' must be a non-empty array")

	artifacts: list[Artifact] = []
	seen_names: set[str] = set()
	for i, art_obj in enumerate(artifacts_arr):
		if not isinstance(art_obj, dict):
			raise ManifestError(f"artifact[{i}] must be an object")
		art = _parse_artifact(art_obj, i, project.license)
		if art.name in seen_names:
			raise ManifestError(f"duplicate artifact name: '{art.name}'")
		seen_names.add(art.name)
		artifacts.append(art)

	# Validation: libraries cannot depend on apps.
	app_names = {a.name for a in artifacts if a.kind == "app"}
	for art in artifacts:
		if art.kind == "library":
			for dep in art.package_deps:
				if dep.name in app_names:
					raise ManifestError(
						f"library artifact '{art.name}' cannot depend on app artifact '{dep.name}'"
					)

	return Manifest(schema_version=sv, project=project, artifacts=artifacts)


def _require_str(obj: dict, key: str, context: str) -> str:
	val = obj.get(key)
	if not isinstance(val, str) or not val:
		raise ManifestError(f"'{context}' requires non-empty string field '{key}'")
	return val


def _parse_artifact(obj: dict, idx: int, project_license: str) -> Artifact:
	ctx = f"artifact[{idx}]"

	kind = _require_str(obj, "kind", ctx)
	if kind == "package":
		import sys
		print(f"warning: {ctx}: 'kind: package' is deprecated; use 'kind: library'", file=sys.stderr)
	if kind not in ("library", "package", "app"):
		raise ManifestError(f"{ctx}: 'kind' must be 'library' or 'app', got '{kind}'")

	name = _require_str(obj, "name", ctx)
	version = _require_str(obj, "version", ctx)
	description = _require_str(obj, "description", ctx)
	entry_module = _require_str(obj, "entry_module", ctx)

	# license inherits from project
	license_val = obj.get("license")
	if isinstance(license_val, str) and license_val:
		lic = license_val
	else:
		lic = project_license

	# modules
	modules_arr = obj.get("modules")
	if not isinstance(modules_arr, list) or len(modules_arr) == 0:
		raise ManifestError(f"{ctx}: 'modules' must be a non-empty array of strings")
	modules: list[str] = []
	for m in modules_arr:
		if not isinstance(m, str):
			raise ManifestError(f"{ctx}: 'modules' entries must be strings")
		modules.append(m)

	# package_deps (optional)
	package_deps: list[PackageDep] = []
	raw_deps = obj.get("package_deps", [])
	if not isinstance(raw_deps, list):
		raise ManifestError(f"{ctx}: 'package_deps' must be an array")
	for j, dep in enumerate(raw_deps):
		if not isinstance(dep, dict):
			raise ManifestError(f"{ctx}: package_deps[{j}] must be an object")
		dep_name = dep.get("name")
		dep_ver = dep.get("version")
		if not isinstance(dep_name, str) or not dep_name:
			raise ManifestError(f"{ctx}: package_deps[{j}] requires 'name'")
		if not isinstance(dep_ver, str) or not dep_ver:
			raise ManifestError(f"{ctx}: package_deps[{j}] requires 'version'")
		if not is_owner_declared_range(dep_ver):
			# Common stale shapes get targeted diagnostics; everything
			# else falls through to the generic rejection.  v2 dep
			# versions are owner-declared acceptable ranges — `"M"` or
			# `"M.N"` — and nothing else.  Exact resolved versions
			# live in `drift/lock.json`.
			if re.match(r"^\d+\.\d+\.\d+$", dep_ver):
				raise ManifestError(
					f"{ctx}: package_deps[{j}] ('{dep_name}') uses exact "
					f"version '{dep_ver}'; v2 manifest dep versions are "
					f"the owner's declared acceptable range — `\"M\"` "
					f"(any M.x.x) or `\"M.N\"` (any M.N.x).  Change to "
					f"'{dep_ver.rsplit('.', 1)[0]}'; the exact resolved "
					f"artifact is recorded in drift/lock.json."
				)
			if dep_ver.startswith("^") or dep_ver.startswith("~"):
				inner = dep_ver[1:]
				suggested = inner.rsplit(".", 1)[0] if "." in inner else inner
				raise ManifestError(
					f"{ctx}: package_deps[{j}] ('{dep_name}') uses range "
					f"operator '{dep_ver[0]}' which is not accepted in v2 "
					f"manifests.  Authored dep versions are `\"M\"` or "
					f"`\"M.N\"` only — e.g. '{suggested}'.  The `^`/`~` "
					f"vocabulary was removed in 0.29 to keep authored "
					f"ranges single-form."
				)
			raise ManifestError(
				f"{ctx}: package_deps[{j}] ('{dep_name}') has invalid "
				f"version '{dep_ver}' — v2 dep versions are the owner's "
				f"declared acceptable range: `\"M\"` (any M.x.x) or "
				f"`\"M.N\"` (any M.N.x)."
			)
		package_deps.append(PackageDep(name=dep_name, version=dep_ver))

	# native_deps (optional)
	native_deps: list[NativeDep] = []
	raw_ndeps = obj.get("native_deps", [])
	if not isinstance(raw_ndeps, list):
		raise ManifestError(f"{ctx}: 'native_deps' must be an array")
	for j, nd in enumerate(raw_ndeps):
		if not isinstance(nd, dict):
			raise ManifestError(f"{ctx}: native_deps[{j}] must be an object")
		lib = nd.get("lib")
		if not isinstance(lib, str) or not lib:
			raise ManifestError(f"{ctx}: native_deps[{j}] requires 'lib'")
		native_deps.append(NativeDep(lib=lib))

	# assets (optional)
	assets: list[str] = []
	raw_assets = obj.get("assets", [])
	if not isinstance(raw_assets, list):
		raise ManifestError(f"{ctx}: 'assets' must be an array")
	for a in raw_assets:
		if not isinstance(a, str):
			raise ManifestError(f"{ctx}: 'assets' entries must be strings")
		assets.append(a)

	# smoke_command (optional)
	smoke_command: list[str] | None = None
	raw_smoke = obj.get("smoke_command")
	if raw_smoke is not None:
		if not isinstance(raw_smoke, list) or not all(isinstance(s, str) for s in raw_smoke):
			raise ManifestError(f"{ctx}: 'smoke_command' must be an array of strings")
		if len(raw_smoke) == 0:
			raise ManifestError(f"{ctx}: 'smoke_command' must not be empty")
		smoke_command = raw_smoke

	# unsafe (optional, default false)
	unsafe = obj.get("unsafe", False)
	if not isinstance(unsafe, bool):
		raise ManifestError(f"{ctx}: 'unsafe' must be a boolean")

	# module_namespace (optional, defaults to name with hyphens → underscores)
	raw_ns = obj.get("module_namespace")
	if raw_ns is not None:
		if not isinstance(raw_ns, str) or not raw_ns:
			raise ManifestError(f"{ctx}: 'module_namespace' must be a non-empty string")
		module_namespace = raw_ns
	else:
		module_namespace = name.replace("-", "_")

	# entry_point (optional, app-only: "module::fn" entry point)
	entry_point = ""
	raw_ep = obj.get("entry_point")
	if raw_ep is not None:
		if not isinstance(raw_ep, str) or not raw_ep:
			raise ManifestError(f"{ctx}: 'entry_point' must be a non-empty string")
		if "::" not in raw_ep:
			raise ManifestError(f"{ctx}: 'entry_point' must be in 'module::fn' format, got '{raw_ep}'")
		if kind != "app":
			raise ManifestError(f"{ctx}: 'entry_point' is only valid for app artifacts")
		entry_point = raw_ep

	return Artifact(
		kind=kind,
		name=name,
		version=version,
		description=description,
		license=lic,
		entry_module=entry_module,
		modules=modules,
		package_deps=package_deps,
		native_deps=native_deps,
		assets=assets,
		smoke_command=smoke_command,
		unsafe=unsafe,
		module_namespace=module_namespace,
		entry_point=entry_point,
	)


# ── Project-root resolution + Artifact→SCI helper ─────────────────


def project_root_for(manifest_dir: Path) -> Path:
	"""Resolve the project root (where module / asset paths are anchored).

	Under the canonical layout the manifest lives at
	``<project_root>/drift/manifest.json``, and module / asset paths
	in the manifest are project-root-relative.  So a manifest entry
	like ``entry_module: "src/lib.drift"`` resolves to
	``<project_root>/src/lib.drift`` -- i.e. this function returns
	``manifest_dir.parent`` for the canonical layout, the project
	root *above* the ``drift/`` directory.  Authors write the paths
	they want to see in their source tree, not paths buried inside
	the ``drift/`` subdir.

	If the manifest's containing dir is NOT named ``drift`` (e.g. a
	non-standard manifest location passed via ``--manifest /tmp/foo.json``),  # drift-tmp-root-audit: allow docstring example

	the project root collapses to the manifest dir itself, so the legacy
	"sources next to manifest" interpretation still works for one-off use.
	"""
	if manifest_dir.name == "drift":
		return manifest_dir.parent
	return manifest_dir


def resolve_module_files(modules: list[str], *, source_root: Path) -> list[str]:
	"""Expand authored ``modules[]`` entries to the concrete compile set.

	A v2 ``artifacts[].modules[]`` entry is the *authored compile input
	set*.  Each entry may be either:

	- a **.drift file** path — kept verbatim (the pinned, must-be-listed
	  contract: adding a file means editing the manifest); or
	- a **directory** path (e.g. ``"src/"`` or ``"src/handlers/"``) —
	  scanned **recursively** for ``*.drift`` files, so new sources under
	  the tree are picked up automatically.

	Returns project-relative POSIX path strings, **deduplicated** and with
	each directory's contents **sorted** (a file matched by both a
	directory entry and an explicit entry appears once).

	This is the single expansion used by BOTH the compile path
	(`build_source_args`) and the SCI path (`compute_artifact_sci`), so
	the bytes that get compiled and the file set that gets signed into the
	author claim are derived identically — switching a listing from
	explicit files to the directory that contains them yields the *same*
	`source_content_id` over the same tree.

	`source_root` is the project root the entries are relative to
	(see `project_root_for`).  An entry that is neither an existing file
	nor directory is passed through unchanged, so the existing
	missing-source diagnostics (`FileNotFoundError` in the SCI path, the
	compiler's own error on the build path) still fire.

	**Source-root containment (same threat the SCI path guards):** a
	directory or file entry — and every `.drift` file discovered under a
	directory entry — must resolve (following symlinks) to a real path
	*inside* `source_root`.  An entry like ``"../outside/"`` or a symlink
	that escapes the project tree is rejected with a `ManifestError`,
	before any walk.  Without this the build path (which does not call the
	SCI path's `_resolve_source_path`) would compile bytes from outside the
	tree, and an out-of-tree symlink target could change what is built
	without touching the project.  In-tree symlinks (target stays under
	`source_root`) are permitted, matching the SCI policy in
	`source_content_id._resolve_source_path`.
	"""
	# Non-strict resolve: canonicalizes the existing prefix without
	# raising for a not-yet-existent root (unit tests pass a synthetic
	# root; all such entries are non-existent passthroughs below).
	root_resolved = source_root.resolve()

	def _inside_root(p: Path) -> bool:
		"""True iff p's real (symlink-followed) path lives under source_root."""
		try:
			p.resolve(strict=True).relative_to(root_resolved)
			return True
		except (OSError, RuntimeError, ValueError):
			return False

	out: list[str] = []
	seen: set[str] = set()

	def _add(rel: str) -> None:
		if rel not in seen:
			seen.add(rel)
			out.append(rel)

	def _escape_error(entry: str, *, what: str) -> "ManifestError":
		return ManifestError(
			f"modules entry {entry!r} {what} outside the project source root "
			f"({source_root}); paths that escape the tree — including through "
			f"`..` or symlinks — are not allowed (a build must compile, and an "
			f"author claim must sign, only bytes under the project root). "
			f"Materialize the source inside the project, or restructure the "
			f"root to include it."
		)

	for entry in modules:
		full = source_root / entry
		if full.is_dir():
			# Validate the directory resolves inside the root BEFORE walking,
			# so `../outside/` or a symlinked-out directory can't be scanned.
			if not _inside_root(full):
				raise _escape_error(entry, what="is a directory resolving")
			found: list[str] = []
			for p in full.rglob("*.drift"):
				if not p.is_file():
					continue
				# A discovered file may itself be a symlink escaping the root.
				if not _inside_root(p):
					rel_disp = p.relative_to(source_root).as_posix() if str(p).startswith(str(source_root)) else str(p)
					raise _escape_error(rel_disp, what="(found under a directory entry) resolves")
				found.append(p.relative_to(source_root).as_posix())
			if not found:
				raise ManifestError(
					f"modules entry {entry!r} is a directory but contains no "
					f"`.drift` source files (recursively); list the files "
					f"explicitly or remove the entry"
				)
			for rel in sorted(found):
				_add(rel)
		else:
			# File path, or a non-existent path.  An *existing* file entry is
			# held to the same containment rule; a non-existent path passes
			# through so the downstream missing-source error (not a raw
			# IsADirectoryError) is what surfaces.
			if full.is_file() and not _inside_root(full):
				raise _escape_error(entry, what="resolves")
			_add(Path(entry).as_posix())
	return out


def resolve_asset_files(assets: list[str], *, source_root: Path) -> list[str]:
	"""Expand authored ``artifacts[].assets[]`` entries to the concrete file set.

	The asset analog of `resolve_module_files`.  Each entry may be either:

	- a **file** path — kept verbatim; or
	- a **directory** path (e.g. ``"assets/singular/db/"``) — scanned
	  **recursively** for ALL regular files (any extension, not just
	  ``*.drift``), so a naturally directory-shaped asset tree (a folder of
	  SQL migrations, doc assets, …) can be declared as one entry.

	Returns project-relative POSIX path strings, **deduplicated** and with
	each directory's contents **sorted**, so the packed asset set and the
	signed `source_content_id` are derived identically — this MUST be used by
	BOTH `compute_artifact_sci` (the SCI path) and `build_package_cmd` (the
	packing path), exactly as `resolve_module_files` is shared, or the two
	would disagree and the package would fail consumer-side SCI verification.

	Symlink policy (deliberate; matches source-input handling, and the
	container carries BYTES not filesystem topology — `drift unpack` never
	creates symlinks):

	- A symlink to a regular file INSIDE the root is OK: the target bytes are
	  packed under the symlink's logical path (a regular file materializes on
	  unpack).
	- A symlink whose resolved target ESCAPES the root is REJECTED (not
	  silently skipped).
	- A DANGLING symlink is REJECTED.
	- A symlink to a DIRECTORY is REJECTED for v1 (avoids loops and
	  "what-got-signed" ambiguity) — declare the real directory instead.

	The directory walk therefore walks regular files only and does NOT recurse
	into symlinked directories.  A non-existent file entry passes through
	unchanged so the downstream missing-asset diagnostic
	(`FileNotFoundError` in the SCI path) fires.
	"""
	root_resolved = source_root.resolve()

	def _inside_root(p: Path) -> bool:
		try:
			p.resolve(strict=True).relative_to(root_resolved)
			return True
		except (OSError, RuntimeError, ValueError):
			return False

	out: list[str] = []
	seen: set[str] = set()

	def _add(rel: str) -> None:
		if rel not in seen:
			seen.add(rel)
			out.append(rel)

	def _escape_error(entry: str, *, what: str) -> "ManifestError":
		return ManifestError(
			f"assets entry {entry!r} {what} outside the project source root "
			f"({source_root}); asset paths that escape the tree — including "
			f"through `..` or symlinks — are not allowed (an author claim must "
			f"sign, and `drift unpack` must materialize, only bytes under the "
			f"project root). Materialize the asset inside the project, or "
			f"restructure the root to include it."
		)

	def _disp(p: Path) -> str:
		try:
			return p.relative_to(source_root).as_posix()
		except ValueError:
			return str(p)

	def _classify_symlink(p: Path) -> None:
		"""Reject a symlink that is not an in-root regular-file symlink.
		Returns normally only for the allowed case (in-root file)."""
		try:
			target = p.resolve(strict=True)
		except (OSError, RuntimeError):
			raise ManifestError(
				f"assets entry contains a dangling symlink {_disp(p)!r} "
				f"(target does not resolve); remove it or point it at a real "
				f"in-project file."
			)
		if target.is_dir():
			raise ManifestError(
				f"assets entry contains a symlink to a directory {_disp(p)!r}; "
				f"symlinked directories are not allowed in v1 (avoids loops and "
				f"signing ambiguity) — declare the real directory, or "
				f"materialize the tree under the project root."
			)
		try:
			target.relative_to(root_resolved)
		except ValueError:
			raise _escape_error(_disp(p), what="(a symlink found under a directory entry) resolves")

	for entry in assets:
		full = source_root / entry
		# A declared directory entry that is itself a symlink → reject (v1).
		if full.is_symlink() and full.is_dir():
			raise ManifestError(
				f"assets entry {entry!r} is a symlink to a directory; symlinked "
				f"directories are not allowed in v1 — declare the real directory."
			)
		if full.is_dir():
			if not _inside_root(full):
				raise _escape_error(entry, what="is a directory resolving")
			found: list[str] = []
			# `rglob` does not recurse into symlinked directories (no loops);
			# each yielded symlink is classified explicitly below rather than
			# silently skipped.
			for p in full.rglob("*"):
				if p.is_symlink():
					_classify_symlink(p)  # raises unless in-root file symlink
					found.append(p.relative_to(source_root).as_posix())
				elif p.is_file():
					# Real regular file physically under `full` (inside root).
					found.append(p.relative_to(source_root).as_posix())
				# else: real directory → walked into by rglob; nothing to add.
			if not found:
				raise ManifestError(
					f"assets entry {entry!r} is a directory but contains no "
					f"files (recursively); list the files explicitly or remove "
					f"the entry"
				)
			for rel in sorted(found):
				_add(rel)
		else:
			# File path (possibly a symlink) or a non-existent passthrough.
			if full.is_symlink():
				_classify_symlink(full)  # raises unless in-root file symlink
			elif full.is_file() and not _inside_root(full):
				raise _escape_error(entry, what="resolves")
			_add(Path(entry).as_posix())
	return out


def compute_artifact_sci(
	art: Artifact,
	*,
	manifest_dir: Path,
) -> str:
	"""Single source of truth for `source_content_id` construction.

	Both ``drift build`` / ``drift deploy`` (the orch pipeline) and
	``drift-author publish`` (the author tool) MUST compute the SCI
	from the exact same input set.  v1's three-way SCI equality check
	(``trust-v1.md`` §3.5: ``package_manifest.sci ==
	author_claim.body.sci == cert_claim.body.sci``) rejects any
	package whose stamped SCI does not match the claim SCIs --
	if the build path and the deploy / author paths disagree on
	inputs, the resulting package fails consumer-side verify.

	No ``target`` parameter: target / build environment is certifier
	metadata (``cert_claim.body.target``), not source identity.  The
	same source release therefore produces the same SCI regardless
	of which target it is built for.

	Caller contract: gate on ``art.kind == "library"`` before calling.
	Apps don't carry SCI (no consumer closure walk).  Surfacing the
	graceful-fallback policy (warn-and-skip when the source tree is
	partially mocked / missing) is left to callers, which already
	wrap the call in ``try / except (FileNotFoundError, ValueError)``.
	"""
	from lang.driftc.packages.source_content_id import (
		compute_artifact_source_content_id,
	)
	source_root = project_root_for(manifest_dir)
	return compute_artifact_source_content_id(
		kind="library",
		package_id=art.name,
		version=art.version,
		module_namespace=art.module_namespace,
		entry_module=art.entry_module,
		# Expand directory entries to their .drift files — identically to
		# the compile path (`build_source_args`) — so the signed source
		# identity matches what is compiled.
		module_paths=resolve_module_files(list(art.modules), source_root=source_root),
		package_deps=[(d.name, d.version) for d in art.package_deps],
		native_deps=[d.lib for d in art.native_deps],
		unsafe=art.unsafe,
		# Expand directory asset entries to their files — identically to the
		# packing path (`build_package_cmd`) — so the signed source identity
		# matches the set of asset blobs actually packed into the .dmp.
		asset_paths=resolve_asset_files(list(art.assets), source_root=source_root),
		source_root=source_root,
	)
