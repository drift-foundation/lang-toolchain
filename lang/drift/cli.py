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
from lang.drift.doctor import DoctorOptions, doctor_exit_code, doctor_v0
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


def _UserPath(s: str) -> Path:
	"""Argparse type that expands ``~`` in path arguments."""
	return Path(s).expanduser()


def _default_keys_dir() -> Path:
	return Path.home() / ".config" / "drift" / "keys"


def _slugify(text: str) -> str:
	"""Simple slug: lowercase, non-alnum to hyphens, collapse runs."""
	import re
	slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
	return slug or "signer"


def _prompt(label: str, *, default: str = "", required: bool = False) -> str:
	"""Prompt for input with optional default. Returns stripped input."""
	if default:
		raw = input(f"  {label} [{default}]: ").strip()
		return raw if raw else default
	suffix = ": " if required else " (optional, press Enter to skip): "
	raw = input(f"  {label}{suffix}").strip()
	if required and not raw:
		print("    This field is required.", file=sys.stderr)
		return _prompt(label, default=default, required=required)
	return raw


def _resolve_signing_key_path(cli_key: Path | None) -> Path | None:
	"""Resolve signing key: --key flag first, then $DRIFT_SIGN_KEY_FILE."""
	if cli_key is not None:
		if not cli_key.exists():
			raise ValueError(f"--key path does not exist: {cli_key}")
		return cli_key
	env_path = os.environ.get("DRIFT_SIGN_KEY_FILE")
	if env_path:
		p = Path(env_path).expanduser()
		if p.exists():
			return p
	return None


def _init_interactive(args: argparse.Namespace) -> int:
	"""Interactive wizard for drift init — publisher project setup."""
	from lang.drift.author_profile import create_author_profile, write_author_profile
	from lang.drift.keygen import KeygenOptions, keygen_ed25519_seed
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

	print("\ndrift init — set up package publishing for this project\n")

	# ── Key resolution ──
	print("Signing key")
	print("  The private Ed25519 key used to sign your packages.\n")

	key_path = _resolve_signing_key_path(args.key)
	if key_path is not None:
		source = "$DRIFT_SIGN_KEY_FILE" if args.key is None else "--key"
		print(f"  Using signing key from {source}: {key_path}")
		override = input("  Press Enter to accept, or enter a different path: ").strip()
		if override:
			key_path = Path(override).expanduser()
	else:
		print("  No signing key found.")
		gen_reply = input("  Generate a new one? [Y/n]: ").strip().lower()
		if gen_reply and gen_reply not in ("y", "yes"):
			key_path_str = _prompt("Signing key path", required=True)
			key_path = Path(key_path_str).expanduser()
		else:
			default_key = _default_keys_dir() / "default.seed"
			key_path = Path(_prompt("Key path", default=str(default_key))).expanduser()
			if key_path.exists():
				print(f"  Key already exists at {key_path}, using it.")
			else:
				keygen_ed25519_seed(KeygenOptions(
					out_path=key_path, print_pubkey=False, print_kid=False,
				))
				print(f"\n  Generated signing key: {key_path}")
				print(f"    Keep this file secret. It is the private key used to sign")
				print(f"    published packages. Back it up securely.\n")

	if not key_path.exists():
		print(f"error: key file not found: {key_path}", file=sys.stderr)
		return 1

	try:
		seed = _load_seed32(key_path)
	except Exception as e:
		print(f"error: {e}", file=sys.stderr)
		return 1

	priv = Ed25519PrivateKey.from_private_bytes(seed)
	pub_raw = ed25519_public_bytes_raw(priv.public_key())
	kid = compute_ed25519_kid(pub_raw)
	print(f"\n  Key loaded")
	print(f"    kid: {kid}\n")

	# ── Publisher details ──
	print("Publisher details")
	print("  Consumers see these when deciding whether to trust your packages.")
	print("  They are informational — trust is verified by key fingerprint.")
	print("  At least one of name or organization is required.\n")

	name = args.name if args.name is not None else _prompt("Author name")
	org = args.org if args.org is not None else _prompt("Organization")
	if not name and not org:
		print("    At least one of name or organization is required.", file=sys.stderr)
		name = _prompt("Author name", required=True)
	email = args.email if args.email is not None else _prompt("Email")
	url = args.url if args.url is not None else _prompt("Website")

	# ── Namespaces ──
	print("\nNamespaces")
	print("  Which Drift module namespaces will this key sign for?")
	print("  These must match the module names consumers import, not package ids.")
	print("  Consumers authorize trust per-namespace, so be specific.")
	print('  Examples: "acme.*" (all acme modules), "net_tls.*" (note: underscores, not hyphens)\n')

	namespaces: list[str] = list(args.namespace) if args.namespace else []
	if not namespaces:
		print("  Namespace (enter one per line, empty line to finish):")
		while True:
			ns = input("    > ").strip()
			if not ns:
				break
			namespaces.append(ns)

	if not namespaces:
		print("error: at least one namespace is required", file=sys.stderr)
		return 1

	# ── Output path ──
	if args.out:
		out_path = args.out
	else:
		default_name = _slugify(org if org else name) + ".author-profile"
		out_path = Path(_prompt("Author profile path", default=default_name))

	# ── Overwrite check ──
	if out_path.exists():
		print(f"\n  File already exists: {out_path}")
		reply = input("  Overwrite? [y/N]: ").strip().lower()
		if reply not in ("y", "yes"):
			print("aborted — existing file preserved", file=sys.stderr)
			return 1

	# ── Summary + confirmation ──
	print(f"\nSummary")
	if name and org:
		print(f"  Publisher:  {name} ({org})")
	elif org:
		print(f"  Publisher:  {org}")
	else:
		print(f"  Publisher:  {name}")
	if email:
		print(f"  Email:      {email}")
	if url:
		print(f"  Website:    {url}")
	print(f"  Key:        {kid}")
	print(f"  Namespaces: {', '.join(namespaces)}")
	print(f"  Output:     {out_path}")

	if not args.yes:
		reply = input("\n  Create this author profile? [Y/n]: ").strip().lower()
		if reply and reply not in ("y", "yes"):
			print("aborted", file=sys.stderr)
			return 1

	profile = create_author_profile(
		pubkey_raw=pub_raw,
		name=name,
		org=org,
		email=email,
		url=url,
		namespaces=namespaces,
	)
	write_author_profile(profile, out_path)
	print(f"\nWrote {out_path}")
	print(f"\nShare this file with consumers. They run:")
	print(f"  drift trust {out_path}")
	return 0


def _init_noninteractive(args: argparse.Namespace) -> int:
	"""Non-interactive drift init (all fields via flags)."""
	from lang.drift.author_profile import create_author_profile, write_author_profile
	from lang.drift.keygen import KeygenOptions, keygen_ed25519_seed
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

	key_path = _resolve_signing_key_path(args.key)
	if key_path is None:
		# Auto-generate key in non-interactive mode.
		key_path = _default_keys_dir() / "default.seed"
		if not key_path.exists():
			keygen_ed25519_seed(KeygenOptions(
				out_path=key_path, print_pubkey=False, print_kid=False,
			))
			print(f"Generated signing key: {key_path}")

	if not key_path.exists():
		print(f"error: key file not found: {key_path}", file=sys.stderr)
		return 1

	try:
		seed = _load_seed32(key_path)
	except Exception as e:
		print(f"error: {e}", file=sys.stderr)
		return 1

	if not args.name and not args.org:
		print("error: at least one of --name or --org is required", file=sys.stderr)
		return 1
	if not args.namespace:
		print("error: --namespace is required in non-interactive mode", file=sys.stderr)
		return 1

	priv = Ed25519PrivateKey.from_private_bytes(seed)
	pub_raw = ed25519_public_bytes_raw(priv.public_key())

	profile = create_author_profile(
		pubkey_raw=pub_raw,
		name=args.name or "",
		org=args.org or "",
		email=args.email or "",
		url=args.url or "",
		namespaces=list(args.namespace),
	)

	out_path = args.out or Path(_slugify(args.org or args.name or "publisher") + ".author-profile")
	if out_path.exists() and not args.yes:
		print(f"error: output file already exists: {out_path}; pass --yes to overwrite", file=sys.stderr)
		return 1
	write_author_profile(profile, out_path)
	print(f"Wrote {out_path}")
	return 0


def _trust_profile_flow(profile_path_str: str, extra_argv: list[str]) -> int:
	"""Handle drift trust <file>.author-profile — consumer review + trust."""
	import argparse as _ap
	from lang.drift.author_profile import apply_author_profile_to_trust_store, load_author_profile

	p = _ap.ArgumentParser(prog="drift trust <profile>", add_help=False)
	p.add_argument("--trust-store", type=_UserPath, default=Path("drift") / "trust.json")
	p.add_argument("--yes", "-y", action="store_true")
	opts = p.parse_args(extra_argv)

	profile_path = Path(profile_path_str)
	if not profile_path.exists():
		print(f"error: author profile not found: {profile_path}", file=sys.stderr)
		return 1

	try:
		profile = load_author_profile(profile_path)
	except ValueError as e:
		print(f"error: {e}", file=sys.stderr)
		return 1

	# Profile-to-package binding in v1 is carried by the
	# `<pkg>.author-claim` sidecar that `drift-author publish` emits
	# next to the `.dmp`.  This CLI no longer reconstructs / verifies
	# any v0 `.sig` envelope -- the v1 trust gate verifies the author
	# claim at `driftc`/`drift_deploy` consumption time.  Surfacing
	# `binding_status = "unbound"` here is deliberate: this command's
	# job is to grant trust for the kid; artifact binding is a
	# downstream concern.
	binding_status = "unbound"

	# Display profile contents.
	print(f"\ndrift trust — review author profile\n")
	if profile.name and profile.org:
		print(f"  Publisher:  {profile.name} ({profile.org})")
	elif profile.org:
		print(f"  Publisher:  {profile.org}")
	else:
		print(f"  Publisher:  {profile.name}")
	if profile.email:
		print(f"  Email:      {profile.email}")
	if profile.url:
		print(f"  Website:    {profile.url}")
	print(f"  Algorithm:  Ed25519")
	print(f"  Key (kid):  {profile.kid}")
	print(f"\n  Requested namespaces:")
	for ns in profile.namespaces:
		print(f"    {ns}")
	if binding_status == "bound":
		print(f"\n  Profile is cryptographically bound to package signature ({profile.package}).")
		print(f"  Package bytes on disk verified against signed digest.")
	elif binding_status == "signature-only":
		print(f"\n  Profile is bound to a signed envelope ({profile.package}),")
		print(f"  but package artifact not found on disk — bytes not independently verified.")
	else:
		print(f"\n  This profile is not cryptographically bound to a package signature.")
		print(f"  Verify the author's identity through an independent channel.")

	if not opts.yes:
		if not sys.stdin.isatty():
			print("error: interactive prompt required; pass --yes for non-interactive mode", file=sys.stderr)
			return 1
		reply = input("\n  Trust this author for the namespaces listed above? [y/N]: ").strip().lower()
		if reply not in ("y", "yes"):
			print("aborted", file=sys.stderr)
			return 1

	try:
		report = apply_author_profile_to_trust_store(profile, opts.trust_store)
	except Exception as e:
		print(f"error: {e}", file=sys.stderr)
		return 1

	added = report.get("namespaces_added", [])
	already = report.get("already_trusted", [])

	if added:
		print(f"\nAdded to {opts.trust_store}")
		print(f"  Key {profile.kid} now trusted for: {', '.join(added)}")
	if already:
		print(f"  Already trusted for: {', '.join(already)}")
	if not added and already:
		print(f"\nAlready fully trusted — no changes made.")

	return 0


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


def _build_parser() -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(
		prog="drift",
		description=(
			"Drift tooling.  Author-side signing in v1 lives in "
			"`drift-author publish` (separate CLI, author-key isolated "
			"from the deploy pipeline)."
		),
	)
	sub = p.add_subparsers(dest="cmd", required=True)

	keygen = sub.add_parser("keygen", help="Generate an Ed25519 private seed key file (base64)")
	keygen.add_argument("--out", type=_UserPath, required=True, help="Output path for key seed file")
	keygen.add_argument("--print-pubkey", action="store_true", help="Print public key (base64) to stdout")
	keygen.add_argument("--print-kid", action="store_true", help="Print kid to stdout")

	key = sub.add_parser("key", help="Signing key utilities")
	key_sub = key.add_subparsers(dest="key_cmd", required=True)

	key_list = key_sub.add_parser("list", help="List local signing keys")
	key_list.add_argument("--keys-dir", type=_UserPath, default=_default_keys_dir(), help="Keys directory (default: ~/.config/drift/keys)")
	key_list.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
	key_list.add_argument("--show-pubkey", action="store_true", help="Include public key in output")

	key_inspect = key_sub.add_parser("inspect", help="Inspect one local signing key by name or path")
	key_inspect.add_argument("key", type=str, help="Key name (without .seed) or full path")
	key_inspect.add_argument("--keys-dir", type=_UserPath, default=_default_keys_dir(), help="Keys directory (default: ~/.config/drift/keys)")
	key_inspect.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

	key_match = key_sub.add_parser("match-signer", help="Find local key(s) matching a signer kid")
	key_match.add_argument("kid", type=str, help="Signer kid (e.g. ed25519:...)")
	key_match.add_argument("--keys-dir", type=_UserPath, default=_default_keys_dir(), help="Keys directory (default: ~/.config/drift/keys)")
	key_match.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

	init = sub.add_parser("init", help="Set up package publishing (signing key + author profile)")
	init.add_argument("--key", type=_UserPath, default=None,
		help="Path to Ed25519 signing key seed (default: $DRIFT_SIGN_KEY_FILE)")
	init.add_argument("--name", type=str, default=None, help="Author name")
	init.add_argument("--org", type=str, default=None, help="Organization or project name")
	init.add_argument("--email", type=str, default=None, help="Contact email")
	init.add_argument("--url", type=str, default=None, help="Website URL")
	init.add_argument("--namespace", type=str, action="append", default=None,
		help="Drift module namespace this key signs for, e.g. 'acme.*' (repeatable)")
	init.add_argument("--out", type=_UserPath, default=None, help="Output author profile path")
	init.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

	trust = sub.add_parser("trust", help="Consumer trust management (project-local)")
	trust_sub = trust.add_subparsers(dest="trust_cmd", required=True)

	trust_list = trust_sub.add_parser("list", help="List keys, namespaces, and revocations in a trust store")
	trust_list.add_argument(
		"--trust-store",
		type=_UserPath,
		default=Path("drift") / "trust.json",
		help="Path to trust store file (default: ./drift/trust.json)",
	)
	trust_list.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

	trust_revoke = trust_sub.add_parser("revoke", help="Revoke a trusted signing key id (kid)")
	trust_revoke.add_argument(
		"--trust-store",
		type=_UserPath,
		default=Path("drift") / "trust.json",
		help="Path to trust store file (default: ./drift/trust.json)",
	)
	trust_revoke.add_argument("--kid", type=str, required=True, help="Key id (kid) to revoke")
	trust_revoke.add_argument("--reason", type=str, default=None, help="Optional revocation reason")

	trust_add = trust_sub.add_parser(
		"add",
		help="Grant a pubkey the author and/or certifier role for a namespace",
	)
	trust_add.add_argument(
		"--trust-store",
		type=_UserPath,
		default=Path("drift") / "trust.json",
		help="Path to trust store file (default: ./drift/trust.json)",
	)
	trust_add.add_argument("--namespace", type=str, required=True, help="Module-id namespace glob (e.g. acme.crypto.*)")
	trust_add.add_argument("--pubkey-b64", type=str, required=True, help="Base64-encoded 32-byte Ed25519 public key")
	trust_add.add_argument("--kid", type=str, default=None, help="Optional kid override (derived from pubkey if omitted)")
	trust_add.add_argument(
		"--role",
		choices=["author", "certifier", "both"],
		default="both",
		help="Role(s) to grant the kid (default: both -- Foundation-bootstrap pattern; production setups should pass author or certifier explicitly)",
	)

	trust_import = trust_sub.add_parser(
		"import",
		help="Import author kids from a v1 <pkg>.author-claim sidecar",
	)
	trust_import.add_argument(
		"--trust-store",
		type=_UserPath,
		default=Path("drift") / "trust.json",
		help="Path to trust store file (default: ./drift/trust.json)",
	)
	trust_import.add_argument(
		"source",
		type=_UserPath,
		help="Path to a .author-claim sidecar or a .dmp / .zdmp (canonical sidecar resolved relative to it)",
	)
	trust_import.add_argument(
		"--namespace",
		type=str,
		default=None,
		help="Override the namespace granted to the imported kids (defaults to the claim's first declared namespace, else <package_id>.*)",
	)
	trust_import.add_argument(
		"--role",
		choices=["author", "certifier", "both"],
		default="author",
		help="Role(s) to grant the imported kids (default: author -- author-claim sidecars carry author kids)",
	)
	trust_import.add_argument("--json", action="store_true", help="Emit machine-readable JSON report")

	doctor = sub.add_parser("doctor", help="Sanity checks for sources/index/trust/lock configuration")
	doctor.add_argument("--sources", type=_UserPath, default=Path("drift") / "sources.json", help="Path to drift/sources.json")
	doctor.add_argument(
		"--trust-store",
		type=_UserPath,
		default=Path("drift") / "trust.json",
		help="Path to trust store file (default: ./drift/trust.json)",
	)
	doctor.add_argument("--lock", type=_UserPath, default=Path("drift") / "sources.lock.json", help="Path to drift/sources.lock.json")
	doctor.add_argument(
		"--cache-dir",
		type=_UserPath,
		default=Path("cache") / "driftpm",
		help="Cache directory (default: ./cache/driftpm)",
	)
	doctor.add_argument(
		"--vendor-dir",
		type=_UserPath,
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

	doc = sub.add_parser("doc", help="Generate API reference documentation from .drift source files")
	doc.add_argument("source", type=_UserPath, help="A .drift file or directory of .drift files to document")
	doc.add_argument("-o", "--output", type=_UserPath, default=Path("doc"), help="Output directory for generated Markdown (default: doc/)")

	sub.add_parser("build", help="Build Drift artifacts from drift/manifest.json (see: drift build --help)")
	sub.add_parser("prepare", help="Resolve dependencies and write drift/lock.json (see: drift prepare --help)")
	sub.add_parser("deploy", help="Build, sign, smoke-test, and publish Drift artifacts (see: drift deploy --help)")
	sub.add_parser("manifest", help="Manifest maintenance helpers: `drift manifest migrate` converts v1 → v2 (see: drift manifest --help)")
	sub.add_parser("lock", help="Lock inspection helpers: `drift lock emit` emits --dep flags for an artifact's resolved graph (see: drift lock --help)")
	return p


def _version_string() -> str:
	"""Build the drift --version output, matching driftc contract."""
	from lang.versions import DRIFTC_VERSION, DRIFT_RT_ABI_VERSION, DRIFTC_GIT_SHA

	# Prefer the build-time stamp; fall back to runtime git only in dev.
	git_sha = DRIFTC_GIT_SHA
	if not git_sha:
		try:
			res = subprocess.run(
				["git", "rev-parse", "--short", "HEAD"],
				capture_output=True, text=True,
				cwd=Path(__file__).resolve().parents[2],
				timeout=5,
			)
			if res.returncode == 0:
				git_sha = res.stdout.strip()
		except Exception:
			pass

	parts = [
		f"drift {DRIFTC_VERSION}",
		f"abi {DRIFT_RT_ABI_VERSION}",
	]
	if git_sha:
		parts.append(f"git {git_sha}")
	parts.append("license GPL-3.0")
	parts.append("The Drift Language Foundation")
	return " | ".join(parts)


def main(argv: list[str] | None = None) -> int:
	effective_argv = argv if argv is not None else sys.argv[1:]

	# Handle --version / -V before argparse so it works without subcommands.
	if "--version" in effective_argv or "-V" in effective_argv:
		print(_version_string())
		return 0

	# Intercept "prepare" and "deploy" before argparse — they have their own arg parsers.
	if effective_argv and effective_argv[0] == "prepare":
		from tools.drift_deploy.drift_prepare import run as prepare_run
		return prepare_run(effective_argv[1:])

	if effective_argv and effective_argv[0] == "deploy":
		from tools.drift_deploy.drift_deploy import run as deploy_run
		return deploy_run(effective_argv[1:])

	if effective_argv and effective_argv[0] == "build":
		from tools.drift_deploy.drift_build import run as build_run
		return build_run(effective_argv[1:])

	# `drift manifest <subcmd>` — authored manifest maintenance.
	# Current subcommands: `migrate` (v1 → v2).  Nothing here reads
	# drift/lock.json; lock regeneration stays in `drift prepare`.
	if effective_argv and effective_argv[0] == "manifest":
		rest = effective_argv[1:]
		if not rest or rest[0] in ("-h", "--help"):
			print(
				"usage: drift manifest <subcommand> [options]\n\n"
				"subcommands:\n"
				"  migrate   Convert drift/manifest.json from v1 to v2 in place\n",
				file=sys.stderr if not rest else sys.stdout,
			)
			return 0 if rest else 1
		if rest[0] == "migrate":
			from tools.drift_deploy.drift_manifest import run as migrate_run
			return migrate_run(rest[1:])
		print(
			f"error: unknown `drift manifest` subcommand: {rest[0]!r}\n"
			f"       known subcommands: migrate",
			file=sys.stderr,
		)
		return 1

	# `drift lock <subcmd>` — read-only lock inspection.  Landed
	# 0.31.7 to give test runners a supported way to thread
	# resolved `--dep` flags into `driftc` without parsing
	# `drift/lock.json` on the bash side.  No write surface here;
	# the lock is authored only by `drift prepare`.
	if effective_argv and effective_argv[0] == "lock":
		rest = effective_argv[1:]
		if not rest or rest[0] in ("-h", "--help"):
			print(
				"usage: drift lock <subcommand> [options]\n\n"
				"subcommands:\n"
				"  emit      Emit `--dep PKG@M.N.P` flags for an artifact's resolved graph\n",
				file=sys.stderr if not rest else sys.stdout,
			)
			return 0 if rest else 1
		if rest[0] == "emit":
			from tools.drift_deploy.drift_lock import run as lock_emit_run
			return lock_emit_run(rest[1:])
		print(
			f"error: unknown `drift lock` subcommand: {rest[0]!r}\n"
			f"       known subcommands: emit",
			file=sys.stderr,
		)
		return 1

	# Intercept "trust <file>.author-profile" — consumer trust flow.
	# Known subcommands are dispatched normally; only .author-profile extension
	# triggers profile trust (no path-existence magic on bare words).
	_TRUST_SUBCOMMANDS = {"list", "revoke"}
	if (
		len(effective_argv) >= 2
		and effective_argv[0] == "trust"
		and effective_argv[1] not in _TRUST_SUBCOMMANDS
		and effective_argv[1].endswith(".author-profile")
	):
		return _trust_profile_flow(effective_argv[1], effective_argv[2:])

	p = _build_parser()
	args = p.parse_args(argv)

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

	if args.cmd == "init":
		if args.yes or not sys.stdin.isatty():
			return _init_noninteractive(args)
		return _init_interactive(args)

	if args.cmd == "trust":
		if args.trust_cmd == "list":
			opts = TrustListOptions(trust_store_path=args.trust_store)
			obj = list_trust_store(opts)
			if args.json:
				print(json.dumps(obj, sort_keys=True, separators=(",", ":")))
			else:
				print(json.dumps(obj, indent=2, sort_keys=True))
			return 0

		if args.trust_cmd == "revoke":
			opts = TrustRevokeOptions(trust_store_path=args.trust_store, kid=args.kid, reason=args.reason)
			try:
				revoke_kid_in_trust_store(opts)
				return 0
			except Exception as err:
				p.error(str(err))
				return 2

		if args.trust_cmd == "add":
			opts = TrustAddKeyOptions(
				trust_store_path=args.trust_store,
				namespace=args.namespace,
				pubkey_b64=args.pubkey_b64,
				kid=args.kid,
				role=args.role,
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
				role=args.role,
			)
			try:
				report = import_sidecar_keys_to_trust_store(opts)
				if args.json:
					print(json.dumps(report, sort_keys=True, separators=(",", ":")))
				else:
					print(f"imported from: {report['source']}")
					print(f"  namespace:        {report['namespace']}")
					print(f"  package_id:       {report['package_id']}")
					print(f"  imported_kids:    {report['imported_kids']}")
					if report["missing_pubkeys"]:
						print(f"  missing_pubkeys:  {report['missing_pubkeys']}")
						print("  (kids whose pubkey is not in the trust store yet; run `drift trust add` for each)")
				return 0
			except Exception as err:
				p.error(str(err))
				return 2

		raise AssertionError("unreachable")

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

	if args.cmd == "doc":
		from tools.drift_doc.drift_doc import generate_docs
		source = Path(args.source)
		output = Path(args.output)
		modules = generate_docs(source_root=source, output_dir=output)
		if not modules:
			print("[drift doc] no modules documented", file=sys.stderr)
			return 1
		print(f"[drift doc] documented {len(modules)} module(s)", flush=True)
		return 0

	raise AssertionError("unreachable")
