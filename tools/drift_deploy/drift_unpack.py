# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
drift unpack — verify a DEPLOYED package directory, then materialize the
assets it carries to a fresh destination directory.

    drift unpack <pkg-dir> --dest <dir> (--trust-store T | --author-profile P
                                          | --author-pubkey-b64 K
                                          | --allow-bundled-pubkey)

Fail-closed contract:

  - VERIFY FIRST, write NOTHING on failure.  The acceptance decision is
    entirely `verify_deployed_package`'s (author + cert signatures, SCI
    three-way equality, artifact hash, provenance cross-check).  A
    tampered, unsigned, or untrusted package can never materialize a byte.
  - NO silent trust fallback.  Exactly as `drift trust verify-package`, a
    trust source MUST be supplied; with none, the verify facade raises a
    usage error.  `--allow-bundled-pubkey` is self-consistency only (proves
    integrity + self-signature, NOT third-party trust) and must be opted
    into explicitly.
  - `--dest` MUST NOT already exist (v1).  Replacement is intentionally a
    later, explicit `--replace` (temp + swap/delete) — v1 never merges into
    or overwrites an existing tree.
  - Extraction is atomic: assets are written to a sibling temp directory,
    each blob's sha256 re-checked against the manifest and each logical path
    re-validated to stay under `--dest`, then the temp dir is atomically
    renamed into place.  A partial `--dest` is never visible.

Asset layout: each asset is written to `<dest>/<logical-path>`, where the
logical path is exactly what the producer declared (project-relative) and
what `source_content_id` committed to.  Authors that declare assets under
`assets/...` get `<dest>/assets/...` — e.g.

    drift unpack "$DRIFT_PKG_ROOT/singular/0.5.0" --dest "$t"
    mariachi --schema-template "$t/assets/singular/db" apply --schema singular_5 …
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from tools.drift_deploy.build_cmd import UserPath
from lang.driftc.packages.dmir_pkg_v0 import load_dmir_pkg_v0_from_bytes, sha256_hex
from lang.driftc.packages.verify_deployed_v1 import (
	VerifyPackageOptions,
	VerifyPackageUsageError,
	verify_deployed_package,
)
from lang.driftc.packages.zdmp import decompress_zdmp


class UnpackError(Exception):
	"""A problem with the package or destination that is NOT CLI misuse.

	Maps to exit code 1 (verification failure, no artifact found, an unsafe
	asset path, an unreadable artifact).  Invocation problems (bad flags,
	`--dest` exists, no trust source) surface as argparse usage / exit 2.
	"""


def build_arg_parser() -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(
		prog="drift unpack",
		description=(
			"Verify a deployed package directory end to end, then "
			"materialize its packed assets to a fresh --dest directory. "
			"Fail-closed: nothing is written unless every signature, the "
			"SCI equality, the artifact hash, and the provenance check "
			"pass.  --dest must not already exist."
		),
	)
	p.add_argument(
		"package_dir",
		type=UserPath,
		help=(
			"Deployed package directory (the `drift deploy` output layout: "
			"<pkg>.zdmp + sidecars + provenance.zst).  Pass the directory, "
			"not the .zdmp."
		),
	)
	p.add_argument(
		"--dest", type=UserPath, required=True,
		help="Destination directory for extracted assets.  MUST NOT exist yet.",
	)
	# Trust source — mirrors `drift trust verify-package` exactly so the
	# verify path is identical.  Mutually exclusive; one is REQUIRED (the
	# verify facade rejects "no trust source" — there is no silent fallback).
	_trust = p.add_mutually_exclusive_group()
	_trust.add_argument(
		"--trust-store", type=UserPath, default=None,
		help="Verify signers against this v1 trust store.",
	)
	_trust.add_argument(
		"--author-pubkey-b64", type=str, default=None,
		help=(
			"Verify against this base64 Ed25519 author pubkey (kid derived; "
			"granted author+certifier for the package's modules)."
		),
	)
	_trust.add_argument(
		"--author-profile", type=UserPath, default=None,
		help="Verify against the key + namespaces in this .author-profile.",
	)
	_trust.add_argument(
		"--allow-bundled-pubkey", action="store_true",
		help=(
			"Self-consistency only: verify against the package's OWN bundled "
			"<pkg>.author-pubkey.b64.  Proves integrity + self-signature, NOT "
			"third-party trust.  Without a trust flag the command is a usage "
			"error, so a CI gate can't mistake 'no key supplied' for 'trusted'."
		),
	)
	p.add_argument(
		"--expect-version", type=str, default=None,
		help="Assert the package version equals this value before extracting.",
	)
	p.add_argument(
		"--expect-sci", type=str, default=None,
		help="Assert source_content_id equals this value (e.g. sha256:...).",
	)
	p.add_argument(
		"--json", action="store_true",
		help="Emit one machine-readable JSON result object.",
	)
	return p


def _find_single_zdmp(package_dir: Path) -> Path:
	"""Return the sole `*.zdmp` in `package_dir`, or raise UnpackError."""
	zdmps = sorted(package_dir.glob("*.zdmp"))
	if not zdmps:
		raise UnpackError(f"no .zdmp artifact found in {package_dir}")
	if len(zdmps) > 1:
		raise UnpackError(
			f"expected exactly one .zdmp in {package_dir}, found {len(zdmps)}: "
			f"{[z.name for z in zdmps]}"
		)
	return zdmps[0]


def _extract_assets_fail_closed(zdmp_path: Path, dest: Path, *, expected_artifact_sha256: str) -> list[str]:
	"""Extract verified assets from `zdmp_path` into `dest` atomically.

	Precondition: the package has ALREADY passed `verify_deployed_package`.
	Re-reads the artifact and re-binds it to the verification result by
	recomputing sha256 of the decompressed `.dmp` bytes and requiring it to
	equal the verified `expected_artifact_sha256` — closing the TOCTOU
	window where the `.zdmp` could be swapped between verify and extract
	(extraction must materialize the EXACT bytes that verified).  Then writes
	to a sibling temp dir (so the rename is atomic on the same filesystem),
	re-checks each blob's sha256 and re-validates each logical path stays
	under the temp root, and atomically renames temp → dest.  Returns the
	sorted list of materialized logical paths.
	"""
	try:
		raw = decompress_zdmp(zdmp_path.read_bytes())
	except Exception as e:  # corrupt/truncated zstd, unreadable file, …
		raise UnpackError(f"failed to read/decompress {zdmp_path.name}: {e}")
	actual_artifact_sha = "sha256:" + sha256_hex(raw)
	if not expected_artifact_sha256 or actual_artifact_sha != expected_artifact_sha256:
		raise UnpackError(
			f"artifact changed between verify and extract: {zdmp_path.name} now hashes "
			f"to {actual_artifact_sha} but verification covered {expected_artifact_sha256} "
			f"— refusing to materialize unverified bytes"
		)
	try:
		loaded = load_dmir_pkg_v0_from_bytes(raw, zdmp_path)
	except Exception as e:  # malformed container that still hashed equal (defensive)
		raise UnpackError(f"failed to load package container from {zdmp_path.name}: {e}")

	dest.parent.mkdir(parents=True, exist_ok=True)
	# Temp dir as a sibling of dest → same filesystem → atomic os.replace.
	tmp_dir = Path(tempfile.mkdtemp(prefix=f".{dest.name}.unpack-", dir=dest.parent))
	tmp_root = tmp_dir.resolve()
	written: list[str] = []
	try:
		for asset in loaded.assets:
			data = loaded.blobs_by_sha256.get(asset.sha256)
			if data is None:
				raise UnpackError(f"asset '{asset.path}' references a blob absent from the container")
			# Defense in depth: the loader already normalized the path, but
			# re-confirm the resolved target stays under the temp root before
			# any write — a verified author path is still sanitized.
			out_path = (tmp_dir / asset.path)
			resolved_parent = out_path.parent.resolve()
			if tmp_root != resolved_parent and tmp_root not in resolved_parent.parents:
				raise UnpackError(f"asset '{asset.path}' resolves outside the destination tree")
			if sha256_hex(data) != asset.sha256:
				raise UnpackError(f"asset '{asset.path}' content hash mismatch")
			out_path.parent.mkdir(parents=True, exist_ok=True)
			out_path.write_bytes(data)
			written.append(asset.path)
		# Atomic publish.  dest is guaranteed not to exist (checked by run()).
		import os
		os.replace(str(tmp_dir), str(dest))
		tmp_dir = None  # ownership transferred to dest; don't clean up below
	finally:
		if tmp_dir is not None and tmp_dir.exists():
			shutil.rmtree(str(tmp_dir), ignore_errors=True)
	return sorted(written)


def run(argv: list[str] | None = None) -> int:
	"""Main entry point for ``drift unpack``.  Returns exit code.

	0 — verified and assets materialized to --dest.
	1 — package failed verification, or extraction problem (nothing written).
	2 — CLI misuse (bad flags, --dest exists, no trust source).
	"""
	parser = build_arg_parser()
	args = parser.parse_args(argv)

	# --dest must not already exist (v1).  Replacement is a future --replace.
	if args.dest.exists():
		parser.error(
			f"--dest already exists: {args.dest}.  `drift unpack` (v1) refuses "
			f"to write into or over an existing directory; choose a fresh path "
			f"(explicit replacement will arrive later as --replace)."
		)
		return 2

	# Translate --author-profile into the pubkey form (CLI-layer concern),
	# exactly as `drift trust verify-package` does.
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

	# Step 1 — verify (fail-closed gate).  No filesystem writes yet.
	try:
		report = verify_deployed_package(opts)
	except VerifyPackageUsageError as err:
		# Invoked wrong (not a directory, zero/many .zdmp, no/conflicting
		# trust source, unreadable CLI trust input) → usage error, exit 2.
		parser.error(str(err))
		return 2

	if not report.get("ok"):
		# Verification OUTCOME failure → exit 1, write nothing.
		if args.json:
			print(json.dumps(
				{"ok": False, "unpacked": False, "dest": str(args.dest), "verify": report},
				sort_keys=True, separators=(",", ":")))
		else:
			print(f"FAIL: {args.package_dir} did not verify — nothing unpacked")
			for e in report.get("errors", []):
				loc = f"[{e['module_id']}] " if e.get("module_id") else ""
				print(f"  {loc}{e['code']}: {e['message']}")
		return 1

	# Step 2 — extract atomically.  Verified; safe to materialize.
	try:
		zdmp_path = _find_single_zdmp(args.package_dir)
		written = _extract_assets_fail_closed(
			zdmp_path, args.dest,
			expected_artifact_sha256=report.get("artifact_sha256"),
		)
	except UnpackError as err:
		# Post-verify extraction problem (e.g. unsafe path).  Atomic temp dir
		# is already cleaned up; --dest was never created.
		if args.json:
			print(json.dumps(
				{"ok": False, "unpacked": False, "dest": str(args.dest), "error": str(err)},
				sort_keys=True, separators=(",", ":")))
		else:
			print(f"FAIL: {err} — nothing unpacked")
		return 1

	if args.json:
		print(json.dumps(
			{
				"ok": True,
				"unpacked": True,
				"dest": str(args.dest),
				"package_id": report.get("package_id"),
				"version": report.get("version"),
				"trust_source": report.get("trust_source"),
				"assets": written,
			},
			sort_keys=True, separators=(",", ":")))
	else:
		pid = report.get("package_id")
		ver = report.get("version")
		print(f"OK: unpacked {pid}@{ver} ({report.get('trust_source')}) → {args.dest}")
		if written:
			for w in written:
				print(f"  {w}")
		else:
			print("  (no assets declared)")
	return 0


if __name__ == "__main__":
	sys.exit(run())
