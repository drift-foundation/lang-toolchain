#!/usr/bin/env python3
"""Generate a drift-trust v0 JSON file from a signed package sidecar.

Usage:
    gen_trust_store.py --sidecar <sig_path> --namespaces std.*,lang.* --output <path>

Reads the .sig sidecar JSON, validates exactly one Ed25519 signature entry
with kid + pubkey present, and writes a drift-trust v0 trust store.

Fails on ambiguity (multiple signatures) or missing/invalid fields.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Generate drift-trust v0 JSON from sidecar")
	parser.add_argument("--sidecar", required=True, type=Path, help="Path to .sig sidecar JSON")
	parser.add_argument("--namespaces", required=True, help="Comma-separated namespace patterns (e.g. std.*,lang.*)")
	parser.add_argument("--output", required=True, type=Path, help="Output trust store JSON path")
	args = parser.parse_args(argv)

	sidecar_path: Path = args.sidecar
	if not sidecar_path.exists():
		print(f"error: sidecar not found: {sidecar_path}", file=sys.stderr)
		return 1

	try:
		sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
	except (json.JSONDecodeError, OSError) as e:
		print(f"error: failed to read sidecar: {e}", file=sys.stderr)
		return 1

	fmt = sidecar.get("format")
	if fmt != "dmir-pkg-sig":
		print(f"error: unexpected sidecar format: {fmt!r} (expected 'dmir-pkg-sig')", file=sys.stderr)
		return 1

	ver = sidecar.get("version")
	if ver != 0:
		print(f"error: unexpected sidecar version: {ver!r} (expected 0)", file=sys.stderr)
		return 1

	sigs = sidecar.get("signatures")
	if not isinstance(sigs, list) or len(sigs) == 0:
		print("error: sidecar has no signatures", file=sys.stderr)
		return 1

	if len(sigs) > 1:
		print(f"error: sidecar has {len(sigs)} signatures; expected exactly 1 (ambiguous signer)", file=sys.stderr)
		return 1

	entry = sigs[0]
	algo = entry.get("algo")
	if algo != "ed25519":
		print(f"error: unsupported signature algorithm: {algo!r} (expected 'ed25519')", file=sys.stderr)
		return 1

	kid = entry.get("kid")
	if not kid or not isinstance(kid, str):
		print("error: signature entry missing 'kid'", file=sys.stderr)
		return 1

	pubkey = entry.get("pubkey")
	if not pubkey or not isinstance(pubkey, str):
		print("error: signature entry missing 'pubkey' (was --include-pubkey used during signing?)", file=sys.stderr)
		return 1

	namespaces = [ns.strip() for ns in args.namespaces.split(",") if ns.strip()]
	if not namespaces:
		print("error: no namespaces specified", file=sys.stderr)
		return 1

	trust_store = {
		"format": "drift-trust",
		"version": 0,
		"keys": {kid: {"algo": "ed25519", "pubkey": pubkey}},
		"namespaces": {ns: [kid] for ns in namespaces},
		"revoked": [],
	}

	args.output.parent.mkdir(parents=True, exist_ok=True)
	args.output.write_text(json.dumps(trust_store, indent=2, sort_keys=True) + "\n", encoding="utf-8")
	print(f"wrote trust store: {args.output} (kid={kid[:24]}...)")
	return 0


if __name__ == "__main__":
	sys.exit(main())
