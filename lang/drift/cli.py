# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lang.drift.crypto import compute_ed25519_kid, ed25519_public_bytes_raw
from lang.drift.fetch import FetchOptions, fetch_v0
from lang.drift.doctor import DoctorOptions, doctor_exit_code, doctor_v0
from lang.drift.index_v0 import load_index
from lang.drift.publish import PublishOptions, publish_packages_v0
from lang.drift.sign import SignOptions, load_sig_sidecar_v0, sign_package_v0
from lang.drift.trust import (
	TrustAddKeyOptions,
	TrustImportOptions,
	TrustListOptions,
	TrustRevokeOptions,
	add_key_to_trust_store,
	import_sidecar_keys_to_trust_store,
	list_trust_store,
	plan_trust_import,
	revoke_kid_in_trust_store,
)
from lang.drift.keygen import KeygenOptions, keygen_ed25519_seed
from lang.drift.vendor import VendorOptions, vendor_v0


def _default_keys_dir() -> Path:
	return Path.home() / ".config" / "drift" / "keys"


def _load_seed32(path: Path) -> bytes:
	text = path.read_text(encoding="utf-8").strip()
	try:
		raw = base64.b64decode(text.encode("ascii"), validate=True)
	except Exception as err:
		raise ValueError(f"invalid base64 seed in key file: {path}") from err
	if len(raw) != 32:
		raise ValueError(f"ed25519 seed must decode to 32 bytes: {path}")
	return raw


def _key_info(path: Path) -> dict[str, object]:
	seed = _load_seed32(path)
	priv = Ed25519PrivateKey.from_private_bytes(seed)
	pub_raw = ed25519_public_bytes_raw(priv.public_key())
	return {
		"name": path.stem,
		"path": str(path),
		"kid": compute_ed25519_kid(pub_raw),
		"pubkey": base64.b64encode(pub_raw).decode("ascii"),
	}


def _resolve_key_path(name_or_path: str, keys_dir: Path) -> Path:
	candidate = Path(name_or_path).expanduser()
	if candidate.exists():
		return candidate
	if "/" in name_or_path:
		raise ValueError(f"key file not found: {name_or_path}")
	seed_name = f"{name_or_path}.seed"
	candidate2 = keys_dir / seed_name
	if candidate2.exists():
		return candidate2
	raise ValueError(f"key not found: {name_or_path} (searched {candidate} and {candidate2})")


def _inspect_signers(path: Path, package_id: str | None) -> dict[str, object]:
	if path.suffix == ".sig":
		sf = load_sig_sidecar_v0(path)
		return {
			"source": "sidecar",
			"path": str(path),
			"package_sha256": sf.package_sha256,
			"signers": sorted({s.kid for s in sf.signatures}),
		}
	if path.suffix in (".dmp", ".zdmp"):
		sig_path = path.with_suffix(".sig")
		if not sig_path.exists():
			raise ValueError(f"missing sidecar for package: {sig_path}")
		sf = load_sig_sidecar_v0(sig_path)
		return {
			"source": "package-sidecar",
			"path": str(sig_path),
			"package_sha256": sf.package_sha256,
			"signers": sorted({s.kid for s in sf.signatures}),
		}
	if path.name == "index.json":
		if not package_id:
			raise ValueError("--package-id is required when inspecting index.json")
		idx = load_index(path)
		pkgs = idx.get("packages") or {}
		if not isinstance(pkgs, dict):
			raise ValueError("invalid index: packages must be an object")
		raw = pkgs.get(package_id)
		if not isinstance(raw, dict):
			raise ValueError(f"package_id not found in index: {package_id}")
		signers = raw.get("signers") or []
		if not isinstance(signers, list):
			raise ValueError(f"invalid signers for package_id: {package_id}")
		return {
			"source": "index",
			"path": str(path),
			"package_id": package_id,
			"signers": sorted({str(s) for s in signers}),
			"signed": bool(raw.get("signed", False)),
		}
	raise ValueError("unsupported input path: expected .dmp, .zdmp, .sig, or index.json")


def _build_parser() -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(prog="drift", description="Drift tooling (package signing, publishing, etc.)")
	sub = p.add_subparsers(dest="cmd", required=True)

	sign = sub.add_parser("sign", help="Sign a DMIR-PKG package (.dmp) by writing a .sig sidecar")
	sign.add_argument("package", type=Path, help="Path to pkg.dmp")
	sign.add_argument(
		"--key",
		type=Path,
		required=False,
		default=None,
		help="Path to base64-encoded Ed25519 private seed (32 bytes). If omitted, uses DRIFT_SIGN_KEY_FILE or DRIFT_SIGN_KEY_CMD.",
	)
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

	key = sub.add_parser("key", help="Signing key utilities")
	key_sub = key.add_subparsers(dest="key_cmd", required=True)

	key_list = key_sub.add_parser("list", help="List local signing keys")
	key_list.add_argument("--keys-dir", type=Path, default=_default_keys_dir(), help="Keys directory (default: ~/.config/drift/keys)")
	key_list.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
	key_list.add_argument("--show-pubkey", action="store_true", help="Include public key in output")

	key_inspect = key_sub.add_parser("inspect", help="Inspect one local signing key by name or path")
	key_inspect.add_argument("key", type=str, help="Key name (without .seed) or full path")
	key_inspect.add_argument("--keys-dir", type=Path, default=_default_keys_dir(), help="Keys directory (default: ~/.config/drift/keys)")
	key_inspect.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

	key_match = key_sub.add_parser("match-signer", help="Find local key(s) matching a signer kid")
	key_match.add_argument("kid", type=str, help="Signer kid (e.g. ed25519:...)")
	key_match.add_argument("--keys-dir", type=Path, default=_default_keys_dir(), help="Keys directory (default: ~/.config/drift/keys)")
	key_match.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

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

	trust_import = trust_sub.add_parser("import", help="Import signing pubkeys from a package sidecar into a namespace")
	trust_import.add_argument(
		"--trust-store",
		type=Path,
		default=Path("drift") / "trust.json",
		help="Path to trust store file (default: ./drift/trust.json)",
	)
	trust_import.add_argument("--namespace", type=str, default=None, help="Module namespace override (default: <package_id>.*)")
	trust_import.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
	trust_import.add_argument("source", type=Path, help="Path to pkg.sig, pkg.dmp, or pkg.zdmp (uses sibling .sig)")
	trust_import.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

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

	pkg = sub.add_parser("package", help="Package inspection helpers")
	pkg_sub = pkg.add_subparsers(dest="pkg_cmd", required=True)
	pkg_signers = pkg_sub.add_parser("inspect-signers", help="Inspect signer kids from .sig/.dmp sidecar or index.json")
	pkg_signers.add_argument("path", type=Path, help="Path to .sig, .dmp, or index.json")
	pkg_signers.add_argument("--package-id", type=str, default=None, help="Required when path is index.json")
	pkg_signers.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

	sub.add_parser("deploy", help="Build, sign, smoke-test, and publish Drift artifacts (see: drift deploy --help)")
	return p


def main(argv: list[str] | None = None) -> int:
	effective_argv = argv if argv is not None else sys.argv[1:]

	# Intercept "deploy" before argparse — deploy has its own arg parser.
	if effective_argv and effective_argv[0] == "deploy":
		from tools.drift_deploy.drift_deploy import run as deploy_run
		return deploy_run(effective_argv[1:])

	p = _build_parser()
	args = p.parse_args(argv)

	if args.cmd == "sign":
		pkg_path: Path = args.package
		out: Path = args.out if args.out is not None else pkg_path.with_suffix(".sig")
		key_seed_path: Path | None = None
		key_seed_text: str | None = None
		if args.key is not None:
			key_seed_path = args.key
		elif os.environ.get("DRIFT_SIGN_KEY_FILE"):
			key_seed_path = Path(str(os.environ["DRIFT_SIGN_KEY_FILE"])).expanduser()
		elif os.environ.get("DRIFT_SIGN_KEY_CMD"):
			cmd = str(os.environ["DRIFT_SIGN_KEY_CMD"])
			cp = subprocess.run(cmd, shell=True, text=True, capture_output=True)
			if cp.returncode != 0:
				p.error(f"DRIFT_SIGN_KEY_CMD failed (exit {cp.returncode}): {(cp.stderr or '').strip()}")
				return 2
			key_seed_text = cp.stdout or ""
		else:
			p.error("missing signing key: pass --key, or set DRIFT_SIGN_KEY_FILE, or set DRIFT_SIGN_KEY_CMD")
			return 2
		opts = SignOptions(
			package_path=pkg_path,
			key_seed_path=key_seed_path,
			key_seed_text=key_seed_text,
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

	if args.cmd == "key":
		if args.key_cmd == "list":
			keys_dir = args.keys_dir.expanduser()
			default_path = Path(os.environ.get("DRIFT_SIGN_KEY_FILE", str(keys_dir / "default.seed"))).expanduser()
			items: list[dict[str, object]] = []
			if keys_dir.exists():
				for key_file in sorted(keys_dir.glob("*.seed")):
					info = _key_info(key_file)
					info["default"] = (key_file.resolve() == default_path.resolve())
					if not args.show_pubkey:
						info.pop("pubkey", None)
					items.append(info)
			if args.json:
				print(json.dumps({"keys": items}, sort_keys=True, separators=(",", ":")))
			else:
				for item in items:
					mark = "*" if bool(item.get("default")) else " "
					print(f"{mark} {item['name']} {item['kid']} {item['path']}")
			return 0
		if args.key_cmd == "inspect":
			try:
				key_path = _resolve_key_path(args.key, args.keys_dir.expanduser())
				info = _key_info(key_path)
				if args.json:
					print(json.dumps(info, sort_keys=True, separators=(",", ":")))
				else:
					print(f"name: {info['name']}")
					print(f"path: {info['path']}")
					print(f"kid: {info['kid']}")
					print(f"pubkey: {info['pubkey']}")
				return 0
			except Exception as err:
				p.error(str(err))
				return 2
		if args.key_cmd == "match-signer":
			try:
				keys_dir = args.keys_dir.expanduser()
				matches: list[dict[str, object]] = []
				if keys_dir.exists():
					for key_file in sorted(keys_dir.glob("*.seed")):
						info = _key_info(key_file)
						if str(info["kid"]) == args.kid:
							info.pop("pubkey", None)
							matches.append(info)
				if args.json:
					print(json.dumps({"kid": args.kid, "matches": matches}, sort_keys=True, separators=(",", ":")))
				else:
					for m in matches:
						print(f"{m['kid']} {m['path']}")
				return 0 if matches else 2
			except Exception as err:
				p.error(str(err))
				return 2
		raise AssertionError("unreachable")

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

		if args.trust_cmd == "import":
			opts = TrustImportOptions(
				trust_store_path=args.trust_store,
				namespace=args.namespace,
				source_path=args.source,
			)
			if not args.yes:
				if args.json:
					p.error("trust import with --json requires --yes")
					return 2
				if not sys.stdin.isatty():
					p.error("trust import requires interactive prompt; pass --yes for non-interactive mode")
					return 2
				try:
					preview_sidecar, preview_namespace, preview_package_id = plan_trust_import(opts)
				except Exception as err:
					p.error(str(err))
					return 2
				pkg_text = f" package_id='{preview_package_id}'" if preview_package_id else ""
				reply = input(
					f"Import signer key(s) from {preview_sidecar} for namespace '{preview_namespace}'{pkg_text}? [y/N]: "
				).strip().lower()
				if reply not in ("y", "yes"):
					print("aborted", file=sys.stderr)
					return 2
			try:
				report = import_sidecar_keys_to_trust_store(opts)
			except Exception as err:
				p.error(str(err))
				return 2
			if args.json:
				print(json.dumps(report, sort_keys=True, separators=(",", ":")))
			else:
				for kid in report.get("imported_kids", []):
					print(f"imported {kid}")
				for kid in report.get("missing_pubkeys", []):
					print(f"missing-pubkey {kid}", file=sys.stderr)
			return 0 if len(report.get("imported_kids", [])) > 0 else 2

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

	if args.cmd == "package":
		if args.pkg_cmd == "inspect-signers":
			try:
				report = _inspect_signers(args.path, args.package_id)
				if args.json:
					print(json.dumps(report, sort_keys=True, separators=(",", ":")))
				else:
					for signer in report.get("signers", []):
						print(signer)
				return 0
			except Exception as err:
				p.error(str(err))
				return 2
		raise AssertionError("unreachable")

	raise AssertionError("unreachable")
