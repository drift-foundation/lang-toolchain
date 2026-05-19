# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
`drift-author` CLI.

Subcommands:

  - `publish` — sign and write a fresh `<pkg>.author-claim` sidecar.
  - `cosign`  — append a second author's signature to an existing
    sidecar (multi-author releases per O8).

All flags that name on-disk files use absolute paths.  Body fields
(`--package-id`, `--version`, `--namespace`, `--source-content-id`,
`--required-dep`, `--target-class`, `--release-utc`) are passed
explicitly rather than being inferred from the package's manifest;
the inference step belongs in a higher-level wrapper (the deploy
pipeline migration in C.2 will provide it).  Keeping this CLI
schema-explicit makes the contract auditable and prevents the
publisher from silently signing a body that does not match the
publishing intent.

Author-key isolation: this CLI runs in `tools/drift_author/` and
loads author seeds via `tools.drift_author.key_loader`.  The
deploy / cert pipeline cannot reach either by the static
import-boundary check.
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
		target_class=args.target_class,
		release_utc=args.release_utc,
	)


def _cmd_publish(args: argparse.Namespace) -> int:
	"""`drift-author publish` — sign and write a fresh sidecar."""
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
		help="Sign and write a fresh <pkg>.author-claim sidecar",
	)
	pub.add_argument("--sidecar-dir", type=Path, required=True)
	pub.add_argument("--package-id", type=str, required=True)
	pub.add_argument("--version", type=str, required=True)
	pub.add_argument(
		"--namespace", type=str, action="append",
		help="Module-id namespace covered by this claim (repeatable)",
	)
	pub.add_argument("--source-content-id", type=str, required=True,
		help="`sha256:<hex>` stamp of the canonicalized build inputs")
	pub.add_argument(
		"--required-dep", type=str, action="append",
		help="NAME=RANGE; repeatable", default=[],
	)
	pub.add_argument("--target-class", type=str, default="library")
	pub.add_argument("--release-utc", type=str, required=True,
		help="Release timestamp, ISO 8601 (e.g. 2026-05-19T00:00:00Z)")
	pub.add_argument("--overwrite", action="store_true",
		help="Replace any existing sidecar; discards prior signatures")
	pub.add_argument("--json", action="store_true",
		help="Emit machine-readable JSON to stdout")
	_add_key_args(pub)
	pub.set_defaults(func=_cmd_publish)

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
