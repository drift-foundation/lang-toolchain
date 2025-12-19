# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import argparse
from pathlib import Path

from lang2.drift.sign import SignOptions, sign_package_v0


def _build_parser() -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(prog="drift", description="Drift tooling (package signing, publishing, etc.)")
	sub = p.add_subparsers(dest="cmd", required=True)

	sign = sub.add_parser("sign", help="Sign a DMIR-PKG package (.dmp) by writing a .dmp.sig sidecar")
	sign.add_argument("package", type=Path, help="Path to pkg.dmp")
	sign.add_argument("--key", type=Path, required=True, help="Path to base64-encoded Ed25519 private seed (32 bytes)")
	sign.add_argument("--out", type=Path, default=None, help="Output sidecar path (default: <pkg>.sig)")
	sign.add_argument(
		"--include-pubkey",
		action="store_true",
		help="Include the public key bytes in the sidecar (driftc still verifies only against trust-store keys)",
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
			include_pubkey=bool(args.include_pubkey),
		)
		sign_package_v0(opts)
		return 0

	raise AssertionError("unreachable")

