# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
drift verify-package — verify a deployed PACKAGE artifact directory (verify only).

The canonical read-only package verifier and top-level peer of
`drift verify-app`: given a deployed package directory (the `.zdmp`
container + signed author/cert/provenance sidecars), verify the artifact +
sidecars + provenance as one consistent set.  (`drift trust` is for trust
management/preflight, not artifact verification.)
"""
from __future__ import annotations

import argparse
import json
import sys

from tools.drift_deploy.build_cmd import UserPath
from lang.driftc.packages.verify_deployed_v1 import (
	VerifyPackageOptions,
	VerifyPackageUsageError,
	error_report as make_verify_error_report,
	verify_deployed_package,
)


def build_arg_parser() -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(
		prog="drift verify-package",
		description=(
			"Verify a deployed PACKAGE directory end to end (.zdmp container + "
			"author/cert/provenance sidecars). Read-only."
		),
	)
	p.add_argument("package_dir", type=UserPath, help="Deployed package directory.")
	g = p.add_mutually_exclusive_group()
	g.add_argument("--trust-store", type=UserPath, default=None,
		help="Verify signers against this v1 trust store.")
	g.add_argument("--author-pubkey-b64", type=str, default=None,
		help="Verify against this base64 Ed25519 author pubkey.")
	g.add_argument("--author-profile", type=UserPath, default=None,
		help="Verify against the key + namespaces in this .author-profile.")
	g.add_argument("--allow-bundled-pubkey", action="store_true",
		help="Self-consistency only: verify against the package's OWN bundled pubkey.")
	p.add_argument("--expect-version", type=str, default=None,
		help="Assert the package version equals this value.")
	p.add_argument("--expect-sci", type=str, default=None,
		help="Assert source_content_id equals this value.")
	p.add_argument("--json", action="store_true", help="Emit one JSON result object.")
	return p


def run(argv: list[str] | None = None) -> int:
	"""Entry point for `drift verify-package`.  0=verified, 1=failed, 2=usage."""
	parser = build_arg_parser()
	args = parser.parse_args(argv)

	author_pubkey_b64 = args.author_pubkey_b64
	author_namespaces = None
	try:
		if args.author_profile is not None:
			from lang.drift.author_profile import load_author_profile
			prof = load_author_profile(args.author_profile)
			author_pubkey_b64 = prof.pubkey_b64
			author_namespaces = list(prof.namespaces)
		opts = VerifyPackageOptions(
			package_dir=args.package_dir,
			trust_store_path=args.trust_store,
			author_pubkey_b64=author_pubkey_b64,
			author_namespaces=author_namespaces,
			allow_bundled_pubkey=args.allow_bundled_pubkey,
			expect_version=args.expect_version,
			expect_sci=args.expect_sci,
		)
	except Exception as err:
		parser.error(str(err))
		return 2

	try:
		report = verify_deployed_package(opts)
	except VerifyPackageUsageError as err:
		parser.error(str(err))
		return 2
	except Exception as err:  # noqa: BLE001 — backstop → structured ok=false
		err_report = make_verify_error_report(args.package_dir, code="verify-error", message=str(err))
		if args.json:
			print(json.dumps(err_report, sort_keys=True, separators=(",", ":")))
		else:
			print(f"FAIL: {args.package_dir} did not verify\n  verify-error: {err}")
		return 1

	if args.json:
		print(json.dumps(report, sort_keys=True, separators=(",", ":")))
	else:
		verdict = "OK" if report["ok"] else "FAIL"
		if report.get("package_id"):
			print(f"{verdict}: {report['package_id']}@{report['version']} ({report['trust_source']})")
		else:
			print(f"{verdict}: {report.get('package_dir')}")
		for m in report["modules"]:
			mark = "✓" if m["ok"] else "✗"
			detail = m["mode"] if m["ok"] else m["reason"]
			print(f"  {mark} {m['module_id']}: {detail}")
		prov = report["provenance_ok"]
		prov_str = "✓ matches" if prov is True else "✗ mismatch" if prov is False else "— not present"
		print(f"  provenance: {prov_str}")
		if report["warnings"]:
			print("\nwarnings:")
			for w in report["warnings"]:
				print(f"  {w}")
		if report["errors"]:
			print("\nerrors:")
			for e in report["errors"]:
				loc = f"[{e['module_id']}] " if e.get("module_id") else ""
				print(f"  {loc}{e['code']}: {e['message']}")
	return 0 if report["ok"] else 1


if __name__ == "__main__":
	sys.exit(run())
