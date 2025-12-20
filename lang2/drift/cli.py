# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import argparse
import json
from pathlib import Path

from lang2.drift.fetch import FetchOptions, fetch_v0
from lang2.drift.publish import PublishOptions, publish_packages_v0
from lang2.drift.sign import SignOptions, sign_package_v0
from lang2.drift.trust import (
	TrustAddKeyOptions,
	TrustListOptions,
	TrustRevokeOptions,
	add_key_to_trust_store,
	list_trust_store,
	revoke_kid_in_trust_store,
)
from lang2.drift.keygen import KeygenOptions, keygen_ed25519_seed
from lang2.drift.vendor import VendorOptions, vendor_v0


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
		try:
			fetch_v0(opts)
			return 0
		except Exception as err:
			p.error(str(err))
			return 2

	if args.cmd == "vendor":
		opts = VendorOptions(
			cache_dir=args.cache_dir,
			dest_dir=args.dest_dir,
			lock_path=args.lock,
			package_ids=list(args.package_ids) if args.package_ids else None,
		)
		try:
			vendor_v0(opts)
			return 0
		except Exception as err:
			p.error(str(err))
			return 2

	raise AssertionError("unreachable")
