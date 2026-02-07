# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lang.drift.fetch import FetchOptions, fetch_v0
from lang.drift.doctor import DoctorOptions, doctor_exit_code, doctor_v0
from lang.drift.publish import PublishOptions, publish_packages_v0
from lang.drift.sign import SignOptions, sign_package_v0
from lang.drift.trust import (
	TrustAddKeyOptions,
	TrustListOptions,
	TrustRevokeOptions,
	add_key_to_trust_store,
	list_trust_store,
	revoke_kid_in_trust_store,
)
from lang.drift.keygen import KeygenOptions, keygen_ed25519_seed
from lang.drift.vendor import VendorOptions, vendor_v0


def _build_parser() -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(prog="drift", description="Drift tooling (package signing, publishing, etc.)")
	sub = p.add_subparsers(dest="cmd", required=True)

	sign = sub.add_parser("sign", help="Sign a DMIR-PKG package (.dmp) by writing a .dmp.sig sidecar")
	sign.add_argument("package", type=Path, help="Path to pkg.dmp")
	sign.add_argument("--key", type=Path, required=True, help="Path to base64-encoded Ed25519 private seed (32 bytes)")
	sign.add_argument("--out", type=Path, default=None, help="Output sidecar path (default: <pkg>.sig)")
	sign.add_argument(
		"--add-signature",
		action="store_true",
		help="Append a signature to an existing sidecar (fails if the sidecar is missing or mismatched)",
	)
	sign.add_argument(
		"--include-pubkey",
		action="store_true",
		help="Include the public key bytes in the sidecar (driftc still verifies only against trust-store keys)",
	)

	keygen = sub.add_parser("keygen", help="Generate an Ed25519 private seed key file (base64)")
	keygen.add_argument("--out", type=Path, required=True, help="Output path for key seed file")
	keygen.add_argument("--print-pubkey", action="store_true", help="Print public key (base64) to stdout")
	keygen.add_argument("--print-kid", action="store_true", help="Print kid to stdout")

	trust = sub.add_parser("trust", help="Trust-store management (project-local)")
	trust_sub = trust.add_subparsers(dest="trust_cmd", required=True)

	trust_list = trust_sub.add_parser("list", help="List keys, namespaces, and revocations in a trust store")
	trust_list.add_argument(
		"--trust-store",
		type=Path,
		default=Path("drift") / "trust.json",
		help="Path to trust store file (default: ./drift/trust.json)",
	)
	trust_list.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

	trust_add = trust_sub.add_parser("add-key", help="Add a trusted signing key and allow it for a namespace")
	trust_add.add_argument(
		"--trust-store",
		type=Path,
		default=Path("drift") / "trust.json",
		help="Path to trust store file (default: ./drift/trust.json)",
	)
	trust_add.add_argument("--namespace", type=str, required=True, help="Module namespace (e.g. acme.*)")
	trust_add.add_argument("--pubkey", type=str, required=True, help="Base64-encoded Ed25519 public key (32 bytes)")
	trust_add.add_argument("--kid", type=str, default=None, help="Key id (kid); derived from pubkey if omitted")

	trust_revoke = trust_sub.add_parser("revoke", help="Revoke a trusted signing key id (kid)")
	trust_revoke.add_argument(
		"--trust-store",
		type=Path,
		default=Path("drift") / "trust.json",
		help="Path to trust store file (default: ./drift/trust.json)",
	)
	trust_revoke.add_argument("--kid", type=str, required=True, help="Key id (kid) to revoke")
	trust_revoke.add_argument("--reason", type=str, default=None, help="Optional revocation reason")

	publish = sub.add_parser("publish", help="Publish package(s) to a local directory repository (index.json)")
	publish.add_argument("--dest-dir", type=Path, required=True, help="Destination directory (repository root)")
	publish.add_argument("packages", nargs="+", type=Path, help="One or more pkg.dmp files to publish")
	publish.add_argument("--force", action="store_true", help="Replace existing entry/files for the same package_id")
	publish.add_argument(
		"--allow-unsigned",
		action="store_true",
		help="Allow publishing unsigned packages (no .sig sidecar)",
	)

	fetch = sub.add_parser("fetch", help="Fetch packages from local sources into a project cache")
	fetch.add_argument("--sources", type=Path, required=True, help="Path to drift-sources.json")
	fetch.add_argument(
		"--cache-dir",
		type=Path,
		default=Path("cache") / "driftpm",
		help="Cache directory (default: ./cache/driftpm)",
	)
	fetch.add_argument("--force", action="store_true", help="Replace conflicting entries in cache index")
	fetch.add_argument(
		"--lock",
		type=Path,
		default=Path("drift.lock.json"),
		help="Lockfile path; if it exists, fetch reproduces it exactly (default: ./drift.lock.json)",
	)
	fetch.add_argument("--json", action="store_true", help="Emit machine-readable JSON report")

	doctor = sub.add_parser("doctor", help="Sanity checks for sources/index/trust/lock configuration")
	doctor.add_argument("--sources", type=Path, default=Path("drift-sources.json"), help="Path to drift-sources.json")
	doctor.add_argument(
		"--trust-store",
		type=Path,
		default=Path("drift") / "trust.json",
		help="Path to trust store file (default: ./drift/trust.json)",
	)
	doctor.add_argument("--lock", type=Path, default=Path("drift.lock.json"), help="Path to drift.lock.json")
	doctor.add_argument(
		"--cache-dir",
		type=Path,
		default=Path("cache") / "driftpm",
		help="Cache directory (default: ./cache/driftpm)",
	)
	doctor.add_argument(
		"--vendor-dir",
		type=Path,
		default=Path("vendor") / "driftpkgs",
		help="Vendor directory (default: ./vendor/driftpkgs)",
	)
	doctor.add_argument("--deep", action="store_true", help="Perform expensive existence/hash checks")
	doctor.add_argument("--json", action="store_true", help="Emit machine-readable JSON report")
	doctor.add_argument(
		"--fail-on",
		choices=["fatal", "degraded"],
		default="fatal",
		help="Exit non-zero on this severity (fatal always exits 2; degraded exits 1 when selected)",
	)

	vendor = sub.add_parser("vendor", help="Vendor cached packages into vendor/driftpkgs and write a lockfile")
	vendor.add_argument(
		"--cache-dir",
		type=Path,
		default=Path("cache") / "driftpm",
		help="Cache directory (default: ./cache/driftpm)",
	)
	vendor.add_argument(
		"--dest-dir",
		type=Path,
		default=Path("vendor") / "driftpkgs",
		help="Vendored package directory (default: ./vendor/driftpkgs)",
	)
	vendor.add_argument(
		"--lock",
		type=Path,
		default=Path("drift.lock.json"),
		help="Lockfile output path (default: ./drift.lock.json)",
	)
	vendor.add_argument(
		"--package-id",
		dest="package_ids",
		action="append",
		default=None,
		help="Restrict vendoring to specific package_id (repeatable); defaults to all cached packages",
	)
	vendor.add_argument("--json", action="store_true", help="Emit machine-readable JSON report")
	return p


def main(argv: list[str] | None = None) -> int:
	p = _build_parser()
	args = p.parse_args(argv)

	if args.cmd == "sign":
		pkg_path: Path = args.package
		out: Path = args.out if args.out is not None else Path(str(pkg_path) + ".sig")
		opts = SignOptions(
			package_path=pkg_path,
			key_seed_path=args.key,
			out_path=out,
			add_signature=bool(args.add_signature),
			include_pubkey=bool(args.include_pubkey),
		)
		try:
			sign_package_v0(opts)
			return 0
		except Exception as err:
			p.error(str(err))
			return 2

	if args.cmd == "keygen":
		opts = KeygenOptions(out_path=args.out, print_pubkey=bool(args.print_pubkey), print_kid=bool(args.print_kid))
		try:
			keygen_ed25519_seed(opts)
			return 0
		except Exception as err:
			p.error(str(err))
			return 2

	if args.cmd == "trust":
		if args.trust_cmd == "list":
			opts = TrustListOptions(trust_store_path=args.trust_store)
			obj = list_trust_store(opts)
			if args.json:
				print(json.dumps(obj, sort_keys=True, separators=(",", ":")))
			else:
				print(json.dumps(obj, indent=2, sort_keys=True))
			return 0

		if args.trust_cmd == "add-key":
			opts = TrustAddKeyOptions(
				trust_store_path=args.trust_store,
				namespace=args.namespace,
				pubkey_b64=args.pubkey,
				kid=args.kid,
			)
			try:
				add_key_to_trust_store(opts)
				return 0
			except Exception as err:
				p.error(str(err))
				return 2

		if args.trust_cmd == "revoke":
			opts = TrustRevokeOptions(trust_store_path=args.trust_store, kid=args.kid, reason=args.reason)
			try:
				revoke_kid_in_trust_store(opts)
				return 0
			except Exception as err:
				p.error(str(err))
				return 2

		raise AssertionError("unreachable")

	if args.cmd == "publish":
		opts = PublishOptions(
			dest_dir=args.dest_dir,
			package_paths=list(args.packages),
			force=bool(args.force),
			allow_unsigned=bool(args.allow_unsigned),
		)
		try:
			publish_packages_v0(opts)
			return 0
		except Exception as err:
			p.error(str(err))
			return 2

	if args.cmd == "fetch":
		opts = FetchOptions(sources_path=args.sources, cache_dir=args.cache_dir, force=bool(args.force), lock_path=args.lock)
		report = fetch_v0(opts)
		if args.json:
			print(json.dumps(report.to_dict(), sort_keys=True, separators=(",", ":")))
			return 0 if report.ok else 2
		if report.ok:
			return 0
		for err in report.errors:
			print(err.format_human(), file=sys.stderr)
		return 2

	if args.cmd == "doctor":
		opts = DoctorOptions(
			sources_path=args.sources,
			trust_store_path=args.trust_store,
			lock_path=args.lock,
			cache_dir=args.cache_dir,
			vendor_dir=args.vendor_dir,
			deep=bool(args.deep),
			fail_on=args.fail_on,
		)
		report = doctor_v0(opts)
		code = doctor_exit_code(report, fail_on=args.fail_on)
		if args.json:
			print(json.dumps(report.to_dict(), sort_keys=True, separators=(",", ":")))
			return code
		# Human mode: compact summary + findings.
		print(
			f"doctor: fatal={report.fatal_count} degraded={report.degraded_count} info={report.info_count} ok={report.ok}",
			file=sys.stderr if code != 0 else sys.stdout,
		)
		for check in sorted(report.checks, key=lambda c: c.check_id):
			if check.status == "ok":
				continue
			stream = sys.stderr if check.status in ("fatal", "degraded") else sys.stdout
			print(f"- {check.check_id}: {check.status} ({check.summary})", file=stream)
			for finding in sorted(
				check.findings,
				key=lambda e: (
					e.reason_code,
					(e.identity.package_id if e.identity is not None and e.identity.package_id is not None else ""),
					(e.source_id or ""),
					(e.artifact_path or ""),
				),
			):
				print(f"  - {finding.format_human()}", file=stream)
		return code

	if args.cmd == "vendor":
		opts = VendorOptions(
			cache_dir=args.cache_dir,
			dest_dir=args.dest_dir,
			lock_path=args.lock,
			package_ids=list(args.package_ids) if args.package_ids else None,
			json=bool(args.json),
		)
		try:
			return int(vendor_v0(opts))
		except Exception as err:
			p.error(str(err))
			return 2

	raise AssertionError("unreachable")
