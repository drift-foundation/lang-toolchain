# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
`drift-deploy cert ...` CLI surface (additive in C.1).

This module provides argparse subcommands for emitting v1 cert
claims.  In C.1 it's a standalone CLI invocable as
`python -m tools.drift_deploy.cert_cli`; in C.2 the existing
`drift-deploy` top-level CLI will route a `cert` subcommand here
instead of the v0 sign path.

Body fields (`--artifact-sha256`, `--source-content-id`, `--target`,
toolchain, dep_graph entries, cert_suite) are passed explicitly so
the certifier signs exactly what the orch pipeline computed.  A
later helper can build the body from a lockfile + cert-suite
report, but that wrapping belongs in the pipeline layer, not the
CLI primitive.

Author-key isolation: this CLI imports
`tools.drift_deploy.cert_emit` (certifier-side) and NEVER
`tools.drift_author.*`.  The static boundary check enforces this.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from lang.driftc.packages.cert_claim_v1 import (
	CertClaimBody,
	CertSuite,
	DepGraphEntry,
	Toolchain,
)
from tools.drift_deploy.cert_emit import (
	SignCertClaimOptions,
	add_cert_signature_to_claim_file,
	decode_cert_seed32,
	load_cert_seed32,
	sign_and_write_cert_claim,
)


_BODY_SCHEMA_VERSION = 1


def _load_seed_from_args(args: argparse.Namespace) -> bytes:
	if bool(args.key_file) == bool(args.key_text):
		raise SystemExit(
			"drift-deploy cert: exactly one of --key-file or "
			"--key-text must be set"
		)
	if args.key_file:
		return load_cert_seed32(args.key_file)
	return decode_cert_seed32(args.key_text)


def _parse_dep_entry(spec: str) -> DepGraphEntry:
	"""Parse `--dep PKG=VER,ART_SHA,SCI,AUTHOR_KID|-,CERT_KID|-,KIND`.

	Six comma-separated fields:
	  package_id, version, artifact_sha256, source_content_id,
	  author_kid (or `-` for None), cert_kid (or `-` for None),
	  dep_kind (`direct` | `transitive`).

	Verbose by design -- the cert claim is the deploy pipeline's
	authoritative attestation of the resolved graph; making the
	fields explicit at the CLI surface keeps a malformed entry
	from being silently signed.
	"""
	parts = spec.split(",")
	if len(parts) != 7:
		raise SystemExit(
			f"drift-deploy cert: --dep expects 7 comma-separated "
			f"fields (PKG, VER, ART_SHA, SCI, AUTHOR_KID|-, CERT_KID|-, "
			f"KIND); got {spec!r}"
		)
	pkg, ver, art, sci, ak, ck, kind = (p.strip() for p in parts)
	return DepGraphEntry(
		package_id=pkg,
		version=ver,
		artifact_sha256=art,
		source_content_id=sci,
		author_kid=None if ak == "-" else ak,
		cert_kid=None if ck == "-" else ck,
		dep_kind=kind,
	)


def _build_body(args: argparse.Namespace) -> CertClaimBody:
	toolchain = Toolchain(
		driftc_version=args.driftc_version,
		drift_rt_abi=int(args.drift_rt_abi),
		driftc_commit=args.driftc_commit or "",
	)
	cert_suite = CertSuite(
		id=args.cert_suite_id,
		version=args.cert_suite_version,
		result=args.cert_suite_result,
		result_evidence_sha256=args.cert_suite_evidence_sha256,
	)
	dep_graph = tuple(_parse_dep_entry(s) for s in (args.dep or []))
	return CertClaimBody(
		schema_version=_BODY_SCHEMA_VERSION,
		package_id=args.package_id,
		version=args.version,
		artifact_sha256=args.artifact_sha256,
		source_content_id=args.source_content_id,
		target=args.target,
		toolchain=toolchain,
		dep_graph=dep_graph,
		cert_suite=cert_suite,
		run_id=args.run_id,
		run_started_utc=args.run_started_utc,
		evidence_sha256=args.evidence_sha256,
	)


def _cmd_publish(args: argparse.Namespace) -> int:
	seed = _load_seed_from_args(args)
	body = _build_body(args)
	written = sign_and_write_cert_claim(SignCertClaimOptions(
		body=body, seed32=seed, sidecar_dir=args.sidecar_dir,
		overwrite=bool(args.overwrite),
	))
	if args.json:
		print(json.dumps({"sidecar": str(written)}))
	else:
		print(f"wrote {written}")
	return 0


def _cmd_cosign(args: argparse.Namespace) -> int:
	seed = _load_seed_from_args(args)
	written = add_cert_signature_to_claim_file(
		sidecar_dir=args.sidecar_dir,
		package_id=args.package_id,
		current_certifier_kid=args.current_certifier_kid,
		seed32=seed,
	)
	if args.json:
		print(json.dumps({"sidecar": str(written)}))
	else:
		print(f"appended rotation signature to {written}")
	return 0


def _add_key_args(p: argparse.ArgumentParser) -> None:
	p.add_argument("--key-file", type=Path)
	p.add_argument("--key-text", type=str)


def _build_parser() -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(
		prog="drift-deploy cert",
		description="Cert-side claim emit for trust-v1.",
	)
	sub = p.add_subparsers(dest="cmd", required=True)

	pub = sub.add_parser("publish", help="Sign and write a fresh cert claim")
	pub.add_argument("--sidecar-dir", type=Path, required=True)
	pub.add_argument("--package-id", type=str, required=True)
	pub.add_argument("--version", type=str, required=True)
	pub.add_argument("--artifact-sha256", type=str, required=True)
	pub.add_argument("--source-content-id", type=str, required=True)
	pub.add_argument("--target", type=str, required=True)
	pub.add_argument("--driftc-version", type=str, required=True)
	pub.add_argument("--drift-rt-abi", type=int, required=True)
	pub.add_argument("--driftc-commit", type=str, default="")
	pub.add_argument(
		"--dep", action="append", default=[],
		help="dep_graph entry (PKG,VER,ART_SHA,SCI,AUTHOR_KID|-,CERT_KID|-,KIND)",
	)
	pub.add_argument("--cert-suite-id", type=str, required=True)
	pub.add_argument("--cert-suite-version", type=str, required=True)
	pub.add_argument(
		"--cert-suite-result", type=str, required=True,
		choices=("pass", "fail"),
	)
	pub.add_argument("--cert-suite-evidence-sha256", type=str, required=True)
	pub.add_argument("--run-id", type=str, required=True)
	pub.add_argument("--run-started-utc", type=str, required=True)
	pub.add_argument("--evidence-sha256", type=str, required=True)
	pub.add_argument("--overwrite", action="store_true")
	pub.add_argument("--json", action="store_true")
	_add_key_args(pub)
	pub.set_defaults(func=_cmd_publish)

	cos = sub.add_parser(
		"cosign",
		help="Append a rotation co-signature to an existing cert claim",
	)
	cos.add_argument("--sidecar-dir", type=Path, required=True)
	cos.add_argument("--package-id", type=str, required=True)
	cos.add_argument(
		"--current-certifier-kid", type=str, required=True,
		help="kid of the existing sidecar (selects which file to append to)",
	)
	cos.add_argument("--json", action="store_true")
	_add_key_args(cos)
	cos.set_defaults(func=_cmd_cosign)

	return p


def main(argv: Optional[list[str]] = None) -> int:
	parser = _build_parser()
	args = parser.parse_args(argv)
	return args.func(args)


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))
