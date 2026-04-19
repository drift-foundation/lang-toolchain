# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
drift manifest migrate — explicit, opt-in v1 → v2 manifest migration.

Normal manifest reads (``tools/drift_deploy/manifest.load_manifest``)
reject v1 files with a pointer at this subcommand.  Migration is
never silent: the authored manifest is only rewritten when the user
explicitly invokes ``drift manifest migrate``.

Scope, strictly:

- Input  : authored ``drift/manifest.json`` with ``schema_version`` 1
           or 2.  Anything else is a hard error.
- Output : the same file, rewritten in place with
           ``schema_version`` bumped to 2 and every
           ``package_deps[].version`` normalised to the v2
           owner-declared acceptable range vocabulary
           (``"M"`` or ``"M.N"``).

Supported per-dep version conversions:

- ``M.N.P``        → ``M.N``        (an exact v1 pin collapses to the
                                     owner-declared minor range that
                                     the pin lived on).
- ``M.N`` or ``M`` → unchanged      (already valid v2).
- anything else    → hard error, file is NOT partially rewritten.

Running the tool twice is a no-op: on the second run the manifest is
already v2 with every dep at ``M``/``M.N``, so there is nothing to do
and the file is left byte-for-byte alone.

Out of scope:

- Lock regeneration — that stays in ``drift prepare``.  Migrating the
  manifest is a structurally safe, purely local rewrite; locking
  requires disk scans and the resolver.
- Silent migration from ``drift prepare`` / ``drift build`` /
  ``drift deploy``.  Those tools only read; they never mutate
  authored metadata.

All-or-nothing: the file is only rewritten after every dep has been
validated, so an unsupported ``^``/``~``/garbage version in any one
entry aborts the whole operation with a diagnostic and leaves the
manifest untouched.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from tools.drift_deploy.build_cmd import UserPath
from tools.drift_deploy.manifest import MANIFEST_SCHEMA_VERSION
# Shared canonical validator for the owner-declared acceptable-range
# shape (`M` / `M.N`).  Keep this import — the migrator must use the
# same rule as the authored-manifest loader and the `.dmp` emitter so
# the "already v2" check can never drift from the production
# validator (the exact regression the Phase 4 consolidation fixed).
from lang.driftc.packages.dmir_pkg_v0 import is_owner_declared_range


class MigrateError(Exception):
	"""Fatal migration error — surfaced as non-zero exit code."""
	pass


# Migration-specific: v1 exact pin `M.N.P` with numeric segments only.
# Collapsing the patch segment yields the v2 owner-declared minor
# range.  Lives here because it is uniquely a migration concern —
# production paths never accept `M.N.P` as an authored dep version.
_MNP_EXACT_RE = re.compile(r"^(\d+)\.(\d+)\.\d+$")


def _classify_version(v: Any) -> tuple[str, str]:
	"""Classify a single dep version string.

	Returns one of:

	- ``("accept", v)``    — already v2 shape, keep as-is.
	- ``("rewrite", "M.N")`` — v1 exact ``M.N.P``, collapse to ``M.N``.
	- ``("invalid", reason)`` — unsupported shape; ``reason`` is a
	  short human-readable diagnostic (``^M.N.P``, ``~M.N``, empty
	  string, 4-part, non-numeric, non-string, etc.).
	"""
	if not isinstance(v, str) or not v:
		return ("invalid", "must be a non-empty string")
	if is_owner_declared_range(v):
		return ("accept", v)
	m = _MNP_EXACT_RE.match(v)
	if m:
		return ("rewrite", f"{m.group(1)}.{m.group(2)}")
	# Targeted sub-diagnostics for the common stale shapes, so the
	# user immediately knows what to change manually.
	if v.startswith("^") or v.startswith("~"):
		return (
			"invalid",
			f"range operator '{v[0]}' is not accepted in v2 manifests; "
			f"authored versions are owner-declared ranges (\"M\" or "
			f"\"M.N\") only — rewrite by hand",
		)
	if re.match(r"^\d+(?:\.\d+){3,}$", v):
		return ("invalid", "version has too many numeric segments "
			"(v2 authored versions are \"M\" or \"M.N\" only)")
	return ("invalid", "not a recognised v1 or v2 dep version shape "
		"(expected \"M\", \"M.N\", or v1 exact \"M.N.P\")")


def _plan_rewrites(data: dict) -> tuple[list[tuple[int, str, int, str, str]], list[str]]:
	"""Walk ``data`` (raw manifest JSON) and plan dep-version rewrites.

	Returns ``(rewrites, errors)`` where:

	- ``rewrites`` is ``[(artifact_index, artifact_name, dep_index,
	  old, new), ...]`` for every ``M.N.P`` → ``M.N`` conversion
	  required.  The ``artifact_index`` is the positional index into
	  ``data["artifacts"]`` — the migrator applies rewrites
	  positionally so that duplicate artifact names in a malformed v1
	  manifest cannot cross-contaminate (rewrites from one artifact
	  leaking into a same-named sibling).  ``artifact_name`` is
	  carried only for diagnostics; it is NEVER the apply key.
	- ``errors`` is a list of diagnostic strings for every
	  unsupported version found.  If non-empty, the caller MUST NOT
	  rewrite the file — this is the all-or-nothing guarantee.
	"""
	rewrites: list[tuple[int, str, int, str, str]] = []
	errors: list[str] = []

	artifacts = data.get("artifacts")
	if not isinstance(artifacts, list):
		errors.append("'artifacts' must be an array")
		return rewrites, errors

	for ai, art in enumerate(artifacts):
		if not isinstance(art, dict):
			errors.append(f"artifact[{ai}] must be an object")
			continue
		art_name = art.get("name", f"<artifact[{ai}]>")
		deps = art.get("package_deps", [])
		if not isinstance(deps, list):
			errors.append(f"{art_name}: 'package_deps' must be an array")
			continue
		for di, dep in enumerate(deps):
			if not isinstance(dep, dict):
				errors.append(f"{art_name}: package_deps[{di}] must be an object")
				continue
			name = dep.get("name", f"<unnamed[{di}]>")
			ver = dep.get("version")
			kind, payload = _classify_version(ver)
			if kind == "invalid":
				errors.append(
					f"{art_name}: package_deps[{di}] ('{name}') version "
					f"'{ver}': {payload}"
				)
			elif kind == "rewrite":
				rewrites.append((ai, art_name, di, ver, payload))
			# "accept" — no action.
	return rewrites, errors


def build_arg_parser() -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(
		prog="drift manifest migrate",
		description=(
			"Convert a v1 drift/manifest.json to v2 in place.  Collapses "
			"exact `M.N.P` package_deps pins to `M.N` owner-declared "
			"ranges and bumps schema_version.  Unsupported versions "
			"(^/~/garbage) are rejected without touching the file.  "
			"Running twice is a no-op."
		),
	)
	p.add_argument(
		"--manifest", "-m", type=UserPath,
		default=Path("drift") / "manifest.json",
		help="Path to drift/manifest.json (default: ./drift/manifest.json)",
	)
	p.add_argument(
		"--dry-run", action="store_true",
		help="Plan and print the changes without modifying the file.",
	)
	return p


def run(argv: list[str] | None = None) -> int:
	"""Main entry point for ``drift manifest migrate``.  Returns exit code."""
	parser = build_arg_parser()
	args = parser.parse_args(argv)
	try:
		return _run_impl(args)
	except MigrateError as e:
		print(f"error: {e}", file=sys.stderr)
		return 1
	except KeyboardInterrupt:
		print("\ninterrupted", file=sys.stderr)
		return 130


def _run_impl(args: argparse.Namespace) -> int:
	path: Path = args.manifest
	if not path.exists():
		raise MigrateError(f"manifest not found: {path}")
	try:
		raw = path.read_text(encoding="utf-8")
	except OSError as e:
		raise MigrateError(f"failed to read {path}: {e}")
	try:
		data = json.loads(raw)
	except json.JSONDecodeError as e:
		raise MigrateError(f"{path} is not valid JSON: {e}")

	if not isinstance(data, dict):
		raise MigrateError(f"{path} must be a JSON object")

	sv = data.get("schema_version")
	if sv not in (1, 2):
		raise MigrateError(
			f"{path} has schema_version {sv!r}; this tool only accepts "
			f"v1 (to migrate) or v2 (idempotent no-op).  Hand-edit "
			f"the file to a supported schema first."
		)

	# Plan first.  Any invalid dep-version shape aborts the run before
	# a single byte is written — the "no partial rewrite" guarantee.
	rewrites, errors = _plan_rewrites(data)
	if errors:
		msg = f"{path}: cannot migrate — {len(errors)} unsupported dep version(s):\n"
		msg += "\n".join(f"  - {e}" for e in errors)
		msg += (
			"\nFix these entries by hand and re-run `drift manifest "
			"migrate`.  The manifest has NOT been modified."
		)
		raise MigrateError(msg)

	if sv == MANIFEST_SCHEMA_VERSION and not rewrites:
		# Already v2 and every dep is already at M/M.N.  Nothing to
		# do — return success without touching the file.  This is the
		# idempotency guarantee.
		print(f"drift manifest migrate: {path} is already at schema v{MANIFEST_SCHEMA_VERSION}; "
		      f"no changes needed")
		return 0

	# Apply rewrites positionally.  Each entry names the exact
	# `(artifact_index, dep_index)` slot to mutate — NEVER the
	# artifact name.  A malformed v1 manifest with duplicate artifact
	# names (which the normal loader would later reject anyway) must
	# not leak rewrites from one artifact into a same-named sibling.
	# Python's `json.load` preserves insertion order (3.7+), so
	# indices from `_plan_rewrites` remain stable here.
	for ai, _art_name, di, _old, new in rewrites:
		data["artifacts"][ai]["package_deps"][di]["version"] = new

	data["schema_version"] = MANIFEST_SCHEMA_VERSION

	new_text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"

	if args.dry_run:
		print(f"drift manifest migrate (--dry-run): would update {path}")
		for ai, art_name, di, old, new in rewrites:
			print(f"  artifacts[{ai}] ({art_name}): package_deps[{di}] '{old}' → '{new}'")
		if sv != MANIFEST_SCHEMA_VERSION:
			print(f"  schema_version: {sv} → {MANIFEST_SCHEMA_VERSION}")
		return 0

	# Only write if the text actually changes.  A no-op write would
	# touch mtime and cause spurious downstream cache invalidation.
	if new_text == raw:
		print(f"drift manifest migrate: {path} already in target shape; no write")
		return 0

	path.write_text(new_text, encoding="utf-8")
	print(f"drift manifest migrate: updated {path}")
	for ai, art_name, di, old, new in rewrites:
		print(f"  artifacts[{ai}] ({art_name}): package_deps[{di}] '{old}' → '{new}'")
	if sv != MANIFEST_SCHEMA_VERSION:
		print(f"  schema_version: {sv} → {MANIFEST_SCHEMA_VERSION}")
	return 0


if __name__ == "__main__":
	sys.exit(run())
