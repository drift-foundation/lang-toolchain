# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
drift/manifest.json manifest loading and validation.

The manifest is the single authoritative source of truth for project
identity, per-artifact configuration, build inputs, dependencies,
and smoke configuration.

Three dep-naming boundaries, distinct by role:

- **manifest `package_deps`** — owner-authored source-of-truth
  declarations.  Each entry is `{name, version}` where `version` is
  the owner's declared acceptable range (`"M"` or `"M.N"`).  Lives
  in this file.
- **package `.dmp` `required_deps`** — published range requirements
  copied from `package_deps` at publish time.  Downstream `drift
  prepare` consumes `required_deps` to build the consumer's own
  exact lock.  (Phase 4 wires this into the `.dmp` emitter.)
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
# consume time (Phase 4); there is no compatibility-shim path from
# `.dmp`-carried legacy metadata into the resolver.  The broader
# parser vocabulary must not reach this authored-manifest validator.
# Authored-manifest dep version shape is the same "owner-declared
# acceptable range" contract as the published `.dmp` `required_deps`
# field — delegate to the canonical validator in the package-format
# module.  (Four surfaces, one regex; see the comment block at the
# helper definition.)
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
