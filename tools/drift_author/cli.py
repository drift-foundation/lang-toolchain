# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
`drift-author` CLI.

Subcommands:

  - `publish` — **the publish command for humans.**  Reads
    `drift/manifest.json`, computes SCI from the declared
    sources/assets/native-deps, derives package_id / version /
    required_deps / module-namespace from the manifest, and signs.
    The operator never hand-enters SCI (a footgun: humans cannot
    compute the canonical digest by hand, and any drift between
    the SCI in the author claim and the SCI stamped into the
    `.dmp` manifest is a hard verify-time rejection per
    trust-v1.md §3.5).  This is the only flow downstream docs
    should teach.

  - `publish-raw` — **internal / test tooling.**  Same end product,
    but every body field is passed explicitly on the command line
    (including `--source-content-id`).  Used by release flows that
    don't go through `drift/manifest.json` at all (notably the
    stdlib release, which computes SCI over the toolchain's own
    `stdlib/` tree in `tools/deploy/steps/stdlib.py`).  Authors
    publishing application or library packages should NOT use this
    flow — picking the wrong SCI silently kills consumer-side
    three-way equality.

  - `cosign` — append a second author's signature to an existing
    sidecar (multi-author releases per O8).

All flags that name on-disk files use absolute paths.

Author-key isolation: this CLI runs in `tools/drift_author/` and
loads author seeds via `tools.drift_author.key_loader`.  The
deploy / cert pipeline cannot reach either by the static
import-boundary check (`test_author_key_boundary.py`).  The
reverse direction is also pinned: this module imports the shared
manifest parser + SCI helper from `lang/driftc/packages/manifest.py`
(neutral home), NOT from `tools/drift_deploy/*`, so the boundary
stays clean in both directions.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from lang.driftc.packages.author_claim_v1 import (
	AuthorClaimBody,
	RequiredDep,
)
from tools.drift_author.author_publish import (
	SignAuthorClaimOptions,
	add_signature_to_claim_file,
	find_existing_author_claim,
	sign_and_write_author_claim,
)
from tools.drift_author.key_loader import (
	decode_author_seed32,
	load_author_seed32,
)


_BODY_SCHEMA_VERSION = 1


def _load_seed_from_args(args: argparse.Namespace) -> bytes:
	"""Return the 32-byte seed from --key-file or --key-text.

	Exactly one of the two MUST be set.  No env-var fallback in v1:
	the trust-v1 audit pinned that the author key never enters the
	process implicitly.
	"""
	if bool(args.key_file) == bool(args.key_text):
		raise SystemExit(
			"drift-author: exactly one of --key-file or --key-text "
			"must be set"
		)
	if args.key_file:
		return load_author_seed32(args.key_file)
	return decode_author_seed32(args.key_text)


def _parse_required_dep(spec: str) -> RequiredDep:
	"""Parse `--required-dep NAME=RANGE` into a `RequiredDep`.

	Range syntax mirrors the author-claim schema (caret/tilde/exact);
	validation happens downstream in `validate_body_shape`.
	"""
	if "=" not in spec:
		raise SystemExit(
			f"drift-author: --required-dep must use NAME=RANGE form; "
			f"got {spec!r}"
		)
	name, _, version_range = spec.partition("=")
	if not name or not version_range:
		raise SystemExit(
			f"drift-author: --required-dep NAME and RANGE must be "
			f"non-empty; got {spec!r}"
		)
	return RequiredDep(name=name, version_range=version_range)


def _build_body(args: argparse.Namespace) -> AuthorClaimBody:
	"""Assemble the AuthorClaimBody from CLI args."""
	namespaces = tuple(args.namespace or ())
	if not namespaces:
		raise SystemExit("drift-author: at least one --namespace is required")
	required = tuple(_parse_required_dep(s) for s in (args.required_dep or []))
	return AuthorClaimBody(
		schema_version=_BODY_SCHEMA_VERSION,
		package_id=args.package_id,
		version=args.version,
		namespaces=namespaces,
		source_content_id=args.source_content_id,
		required_deps=required,
		release_utc=args.release_utc,
	)


def _cmd_publish_raw(args: argparse.Namespace) -> int:
	"""`drift-author publish-raw` — sign and write a sidecar from
	hand-supplied body fields (incl. ``--source-content-id``).

	Internal / test tooling.  Used by toolchain release flows that
	compute SCI outside ``drift/manifest.json`` (chiefly the stdlib
	publish in ``tools/deploy/steps/stdlib.py``).  Package authors
	should use ``_cmd_publish`` (manifest-aware) instead.
	"""
	seed = _load_seed_from_args(args)
	body = _build_body(args)
	sidecar_dir = args.sidecar_dir
	if existing := find_existing_author_claim(sidecar_dir, package_id=body.package_id):
		if not args.overwrite:
			print(
				f"drift-author: refusing to overwrite existing "
				f"sidecar {existing}; use `drift-author cosign` for "
				f"multi-author release, or pass --overwrite to "
				f"replace.",
				file=sys.stderr,
			)
			return 1
	written = sign_and_write_author_claim(SignAuthorClaimOptions(
		body=body, seed32=seed, sidecar_dir=sidecar_dir,
		overwrite=bool(args.overwrite),
	))
	if args.json:
		print(json.dumps({"sidecar": str(written)}))
	else:
		print(f"wrote {written}")
	return 0


def _cmd_publish(args: argparse.Namespace) -> int:
	"""`drift-author publish` — manifest-aware publish.

	Reads `drift/manifest.json`, computes SCI via the shared helper
	(`lang.driftc.packages.manifest.compute_artifact_sci`), derives
	the other body fields from the manifest, signs, writes the
	sidecar.

	Defaults are tuned for the common case (single-library manifest,
	publish into the manifest dir):
	  - `--artifact`   defaults to the sole library artifact; required
	                   when the manifest declares multiple.
	  - `--sidecar-dir` defaults to the manifest's own directory
	                   (`<repo>/drift/`), which is where
	                   `tools/drift_deploy/drift_deploy.py`'s
	                   `_attach_author_claim_to_artifact` looks.
	  - `--namespace`  defaults to `<art.module_namespace>.*`;
	                   repeatable for packages that own additional
	                   patterns.
	  - `--release-utc` defaults to now (UTC).
	"""
	seed = _load_seed_from_args(args)

	manifest_path = args.manifest.expanduser().resolve()
	if not manifest_path.is_file():
		raise SystemExit(f"drift-author: manifest not found: {manifest_path}")
	# Import from the neutral manifest module so the author tree
	# stays free of any `tools.drift_deploy.*` / `tools.deploy.*`
	# dependency (the author-key-out-of-orch boundary check).
	from lang.driftc.packages.manifest import (
		ManifestError, compute_artifact_sci, load_manifest,
	)
	try:
		manifest = load_manifest(manifest_path)
	except ManifestError as e:
		raise SystemExit(f"drift-author: {e}")
	manifest_dir = manifest_path.parent

	# Pick the artifact.  Only library artifacts carry SCI (apps don't
	# get verified through the package closure path); filter to those.
	libs = [a for a in manifest.artifacts if a.kind == "library"]
	if not libs:
		raise SystemExit(
			f"drift-author: manifest at {manifest_path} declares no "
			f"library artifacts; nothing to publish"
		)
	if args.artifact:
		matches = [a for a in libs if a.name == args.artifact]
		if not matches:
			raise SystemExit(
				f"drift-author: --artifact {args.artifact!r} not found in "
				f"manifest; available library artifacts: "
				f"{[a.name for a in libs]!r}"
			)
		art = matches[0]
	elif len(libs) == 1:
		art = libs[0]
	else:
		raise SystemExit(
			f"drift-author: manifest declares multiple library artifacts "
			f"({[a.name for a in libs]!r}); pass --artifact <name> to pick one"
		)

	try:
		sci = compute_artifact_sci(art, manifest_dir=manifest_dir)
	except (FileNotFoundError, ValueError) as e:
		raise SystemExit(
			f"drift-author: SCI computation failed for "
			f"'{art.name}@{art.version}': {e}"
		)

	if args.namespace:
		namespaces = tuple(args.namespace)
	else:
		# Default: the package's declared module_namespace covers a
		# single glob `<ns>.*`.  Operators that own additional
		# patterns (e.g. stdlib: std.*, lang.*, drift.*) override via
		# repeated --namespace.
		namespaces = (f"{art.module_namespace}.*",)

	required_deps = tuple(
		RequiredDep(name=d.name, version_range=d.version)
		for d in art.package_deps
	)

	if args.release_utc:
		release_utc = args.release_utc
	else:
		import datetime as _dt
		release_utc = _dt.datetime.now(_dt.timezone.utc).strftime(
			"%Y-%m-%dT%H:%M:%SZ"
		)

	body = AuthorClaimBody(
		schema_version=_BODY_SCHEMA_VERSION,
		package_id=art.name,
		version=art.version,
		namespaces=namespaces,
		source_content_id=sci,
		required_deps=required_deps,
		release_utc=release_utc,
	)

	sidecar_dir = args.sidecar_dir if args.sidecar_dir else manifest_dir
	if existing := find_existing_author_claim(sidecar_dir, package_id=body.package_id):
		if not args.overwrite:
			print(
				f"drift-author: refusing to overwrite existing sidecar "
				f"{existing}; pass --overwrite to replace.",
				file=sys.stderr,
			)
			return 1

	written = sign_and_write_author_claim(SignAuthorClaimOptions(
		body=body, seed32=seed, sidecar_dir=sidecar_dir,
		overwrite=bool(args.overwrite),
	))
	if args.json:
		print(json.dumps({
			"sidecar": str(written),
			"package_id": body.package_id,
			"version": body.version,
			"source_content_id": sci,
			"namespaces": list(namespaces),
			"release_utc": release_utc,
		}))
	else:
		print(f"wrote {written}")
		print(f"  package_id:        {body.package_id}")
		print(f"  version:           {body.version}")
		print(f"  source_content_id: {sci}")
		print(f"  namespaces:        {list(namespaces)}")
		print(f"  release_utc:       {release_utc}")
	return 0


def _cmd_cosign(args: argparse.Namespace) -> int:
	"""`drift-author cosign` — append a co-author signature."""
	seed = _load_seed_from_args(args)
	written = add_signature_to_claim_file(
		sidecar_dir=args.sidecar_dir,
		package_id=args.package_id,
		seed32=seed,
	)
	if args.json:
		print(json.dumps({"sidecar": str(written)}))
	else:
		print(f"appended signature to {written}")
	return 0


def _add_key_args(p: argparse.ArgumentParser) -> None:
	"""Both subcommands take exactly one of --key-file / --key-text."""
	p.add_argument(
		"--key-file", type=Path,
		help="Path to a base64-encoded 32-byte Ed25519 private seed",
	)
	p.add_argument(
		"--key-text", type=str,
		help="Base64-encoded 32-byte Ed25519 private seed (inline)",
	)


def _build_parser() -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(
		prog="drift-author",
		description="Author-side claim emit for trust-v1.",
	)
	sub = p.add_subparsers(dest="cmd", required=True)

	pub = sub.add_parser(
		"publish",
		help=(
			"Manifest-aware publish.  Reads drift/manifest.json, computes "
			"SCI, derives body fields, signs.  This is the publish command "
			"for humans -- use this unless you're a release flow that "
			"doesn't go through drift/manifest.json."
		),
	)
	pub.add_argument("--manifest", type=Path, required=True,
		help="Path to drift/manifest.json (the file, not the directory)")
	pub.add_argument("--artifact", type=str, default=None,
		help=(
			"Pick a specific library artifact by name when the manifest "
			"declares more than one.  Required in the multi-library case."
		))
	pub.add_argument(
		"--namespace", type=str, action="append",
		help=(
			"Override declared namespaces (repeatable).  Default: a "
			"single glob `<art.module_namespace>.*`."
		))
	pub.add_argument("--release-utc", type=str, default=None,
		help="Release timestamp (ISO 8601); default is now (UTC).")
	pub.add_argument("--sidecar-dir", type=Path, default=None,
		help=(
			"Where to write the .author-claim sidecar.  Default: the "
			"manifest's own directory (`<repo>/drift/`), which is where "
			"`drift deploy` will look for it."
		))
	pub.add_argument("--overwrite", action="store_true",
		help="Replace any existing sidecar; discards prior signatures")
	pub.add_argument("--json", action="store_true",
		help="Emit machine-readable JSON to stdout")
	_add_key_args(pub)
	pub.set_defaults(func=_cmd_publish)

	# ── publish-raw: internal / test tooling ──
	# Use this only when the publisher does NOT have a
	# drift/manifest.json that fully describes the artifact -- chiefly
	# the toolchain's own stdlib release (which computes SCI over the
	# `stdlib/` tree directly in `tools/deploy/steps/stdlib.py` and
	# does not load a v2 package manifest).  Authors of regular
	# packages should use `publish` instead; hand-entering SCI on the
	# command line is a footgun (any mismatch between this SCI and the
	# one stamped into the .dmp manifest is a hard verify-time
	# rejection at consumer load time per trust-v1.md §3.5).
	raw = sub.add_parser(
		"publish-raw",
		help=(
			"Internal / test tooling.  Sign with hand-supplied "
			"--source-content-id; only correct for flows that compute "
			"SCI outside drift/manifest.json (chiefly the toolchain's "
			"own stdlib release).  Most users want `publish` instead."
		),
	)
	raw.add_argument("--sidecar-dir", type=Path, required=True)
	raw.add_argument("--package-id", type=str, required=True)
	raw.add_argument("--version", type=str, required=True)
	raw.add_argument(
		"--namespace", type=str, action="append",
		help="Module-id namespace covered by this claim (repeatable)",
	)
	raw.add_argument("--source-content-id", type=str, required=True,
		help="`sha256:<hex>` stamp of the canonicalized build inputs")
	raw.add_argument(
		"--required-dep", type=str, action="append",
		help="NAME=RANGE; repeatable", default=[],
	)
	raw.add_argument("--release-utc", type=str, required=True,
		help="Release timestamp, ISO 8601 (e.g. 2026-05-19T00:00:00Z)")
	raw.add_argument("--overwrite", action="store_true",
		help="Replace any existing sidecar; discards prior signatures")
	raw.add_argument("--json", action="store_true",
		help="Emit machine-readable JSON to stdout")
	_add_key_args(raw)
	raw.set_defaults(func=_cmd_publish_raw)

	cos = sub.add_parser(
		"cosign",
		help="Append a co-author signature to an existing sidecar",
	)
	cos.add_argument("--sidecar-dir", type=Path, required=True)
	cos.add_argument("--package-id", type=str, required=True)
	cos.add_argument("--json", action="store_true")
	_add_key_args(cos)
	cos.set_defaults(func=_cmd_cosign)

	return p


def main(argv: Optional[list[str]] = None) -> int:
	parser = _build_parser()
	args = parser.parse_args(argv)
	return args.func(args)


def _build_author_subcommand_parser() -> argparse.ArgumentParser:
	"""Flat (no inner subcommand) parser for the public `drift author`
	command.

	`drift author` is the **author-role** step of the trust-v1
	lifecycle:

	  drift author   →  refresh the author claim (this command)
	  drift deploy   →  build artifact + cert claim
	  drift trust    →  consumer trust store bootstrap/check/add

	Single-purpose by design: no further subcommand level (everything
	below was previously surfaced as `python -m tools.drift_author
	publish`).  Specialised flows that don't go through
	`drift/manifest.json` (notably the toolchain's own stdlib release)
	keep using the internal `python -m tools.drift_author publish-raw`
	entry point; co-signing additional authors keeps using
	`python -m tools.drift_author cosign`.
	"""
	p = argparse.ArgumentParser(
		prog="drift author",
		description=(
			"Mint or refresh this package's author claim.  Reads "
			"`drift/manifest.json`, computes source_content_id via the "
			"shared manifest helper, signs, and writes "
			"`drift/<pkg>.author-claim` (plus `drift/<pkg>.author-pubkey.b64`).  "
			"Does NOT build artifacts, does NOT deploy, does NOT emit "
			"cert claims, does NOT write package roots."
		),
	)
	p.add_argument("--manifest", type=Path,
		default=Path("drift") / "manifest.json",
		help=(
			"Path to drift/manifest.json (the file, not the "
			"directory).  Default: ./drift/manifest.json -- matches "
			"the lifecycle commands (`drift build`, `drift prepare`, "
			"`drift deploy`)."
		))
	p.add_argument("--artifact", type=str, default=None,
		help=(
			"Pick a specific library artifact by name when the manifest "
			"declares more than one.  Required in the multi-library case."
		))
	p.add_argument(
		"--namespace", type=str, action="append",
		help=(
			"Override declared namespaces (repeatable).  Default: a "
			"single glob `<art.module_namespace>.*`."
		))
	p.add_argument("--release-utc", type=str, default=None,
		help="Release timestamp (ISO 8601); default is now (UTC).")
	p.add_argument("--sidecar-dir", type=Path, default=None,
		help=(
			"Where to write the .author-claim sidecar.  Default: the "
			"manifest's own directory (`<repo>/drift/`), which is where "
			"`drift deploy` will look for it."
		))
	p.add_argument("--overwrite", action="store_true",
		help="Replace any existing sidecar; discards prior signatures")
	p.add_argument("--json", action="store_true",
		help="Emit machine-readable JSON to stdout")
	_add_key_args(p)
	return p


def run_author_subcommand(argv: list[str]) -> int:
	"""Entry point for `drift author …`.

	Wired from `lang/drift/cli.py` (intercept-before-argparse, next to
	`build`/`prepare`/`deploy`).  Thin wrapper that builds the flat
	parser above and dispatches to the same `_cmd_publish` handler the
	internal `python -m tools.drift_author publish` entry point uses,
	so the manifest-driven mint behaviour is bit-identical between the
	two surfaces.
	"""
	parser = _build_author_subcommand_parser()
	args = parser.parse_args(argv)
	return _cmd_publish(args)
