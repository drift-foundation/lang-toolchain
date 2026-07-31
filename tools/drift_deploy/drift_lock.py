# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
drift lock — read-only inspection helpers for `drift/lock.json`.

Current subcommand:

    drift lock emit --artifact <name> [--format driftc] [--manifest <path>]
                    [--source-rebuild [--run-snapshot <path>]
                     [--package-root <dir>]...] [--json]

Emits exact `--dep PKG@M.N.P` flags for the named artifact's resolved
graph.  Use in test runners / ad-hoc compile paths that need to pass
the lock's resolved versions to `driftc` without parsing JSON on the
bash side:

    DEP_FLAGS=$(drift lock emit --artifact singular)
    driftc $DEP_FLAGS --package-root <lib> tests/foo_test.drift -o build/foo

Certification lane (0.33.92, the drift-workflows/build-orchestrator
consumer contract): `--source-rebuild` derives the flags by FRESH
resolution via `tools.drift_deploy.source_rebuild.resolve_source_rebuild`
— the same single authority `drift build/deploy/prepare --check`
consume — against the snapshot-gated candidate pool, so a consumer
repo's cert gate execs the run's own toolchain binary instead of
sys.path-importing a drift-lang source checkout.  Under the documented
cert env contract (DRIFT_RUN_SNAPSHOT + DRIFT_PKG_ROOT set by the
orchestrator) the announced invocation needs no extra flags:

    DEP_FLAGS=$(drift lock emit --artifact singular --source-rebuild)

stdout is the flags contract (exactly the `--dep` list, nothing else);
evidence and diagnostics go to stderr; any authority error means
non-zero exit with empty stdout.  The committed lock is EVIDENCE, not
the graph authority, in this lane.  DRIFT_CERT_MODE alone does NOT
select the lane (unlike build/deploy/prepare) — gate recipes choose
explicitly via the flag, so strict-lane callers keep their stdout
contract inside certification-run environments.

Why this exists (0.31.7):

Before manifest schema v2 (0.29.0) a runner could read
`drift/manifest.json::package_deps[].version` directly and pass each
entry as `--dep`, because v1 manifests carried exact `M.N.P` pins.
v2 made those entries owner-declared RANGES (`"M"` / `"M.N"`), and
the resolved exact versions now live only in `drift/lock.json`
(schema v4).  `drift build` / `drift deploy` already use the same
in-process `expand_to_dep_flags` helper to thread `--dep` flags
through to `driftc`; this subcommand is the read-only CLI surface on
top of that same helper, so every library's test runner has one
supported way to get the resolved graph.

Format stability: the `driftc` format (sorted `--dep PKG@M.N.P`
flags, space-separated on stdout, deterministic) is stable across
toolchain versions.  Lock schema migrations (v3 → v4 happened at
0.30.1) are absorbed here — the output format stays the same.

Contract: `drift lock emit` emits EVERY resolved entry in the
artifact's graph, including entries with `dep_type: "co-artifact"`
(peer library artifacts in the same manifest).  This matches the
flag list `drift build` / `drift deploy` pass to `driftc`
internally — "the emitted flags are exactly what drift build
would pass."  The invariant matters for runners that want to
reproduce a build step's dep-resolution view exactly.

Caller responsibility: the flags are only useful if every pinned
package is visible under `--package-root`.  For co-artifacts —
packages built by the same manifest — this means the runner must
have built those co-artifacts first (or be running after a
`drift deploy` that published them to a shared `pkg/` tree).
Calling `drift lock emit` on a manifest that declares multiple
library artifacts and passing the flags to `driftc` against an
empty package root will produce "package not found" errors for
the co-artifact pins — as expected.  Single-artifact libraries
(the common case for library test runners) never have
co-artifact entries and don't need to worry about this.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from tools.drift_deploy.build_cmd import CertModeError, UserPath
from tools.drift_deploy.lockfile import expand_to_dep_flags, read_lock
from tools.drift_deploy.manifest import ManifestError, load_manifest


class LockEmitError(Exception):
	"""Fatal error while emitting lock data (missing lock, missing
	artifact, malformed lock)."""
	pass


_VALID_FORMATS = ("driftc",)

_JSON_SCHEMA = "drift-lock-emit/v0"


def build_arg_parser() -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(
		prog="drift lock emit",
		description=(
			"Emit exact `--dep PKG@M.N.P` flags for an artifact's "
			"resolved dependency graph.  Reads `drift/lock.json` "
			"(v4) and writes the flags space-separated to stdout in "
			"sorted order.  Intended for test runners / ad-hoc "
			"compile paths that need the resolved versions without "
			"parsing the lock JSON themselves.  Emits EVERY "
			"resolved entry including co-artifacts (peer library "
			"artifacts in the same manifest) — matches the flag "
			"list `drift build` passes to `driftc`.  Callers are "
			"responsible for ensuring every pinned package is "
			"visible under `--package-root`; co-artifacts must be "
			"built or deployed first."
		),
	)
	p.add_argument(
		"--artifact", "-a", type=str, required=True,
		help="Artifact name from the manifest whose resolved graph to emit.",
	)
	p.add_argument(
		"--format", "-f", type=str, default="driftc", choices=_VALID_FORMATS,
		help="Output format (default: driftc).  Currently only `driftc` is supported.",
	)
	p.add_argument(
		"--manifest", "-m", type=UserPath,
		default=Path("drift") / "manifest.json",
		help="Path to drift/manifest.json (default: ./drift/manifest.json).  "
		     "The lock is read from the same directory as the manifest.",
	)
	p.add_argument(
		"--source-rebuild", action="store_true",
		help="Certification-lane dep derivation: fresh-resolve the "
		     "artifact via the single source-rebuild authority "
		     "(`resolve_source_rebuild` — the same call path `drift "
		     "build/deploy/prepare --check` take) against the snapshot-"
		     "gated package index, instead of reading the committed "
		     "lock.  stdout stays exactly the `--dep` flags; lock-vs-"
		     "fresh evidence and diagnostics go to stderr.  Requires "
		     "a run snapshot (`--run-snapshot` or DRIFT_RUN_SNAPSHOT) "
		     "and a candidate pool (`--package-root` or "
		     "DRIFT_PKG_ROOT).  NOTE: unlike build/deploy/"
		     "prepare, the DRIFT_CERT_MODE env var alone does NOT "
		     "select this lane — gate recipes choose it explicitly "
		     "with this flag, so a strict-lane `drift lock emit` "
		     "keeps its stdout contract even inside a certification "
		     "run's environment.",
	)
	p.add_argument(
		"--run-snapshot", type=UserPath, default=None,
		help="(--source-rebuild only) Path to the orch-produced "
		     "certification-run snapshot "
		     "(`tools.drift_deploy.run_snapshot` JSON v0).  Also "
		     "honoured via DRIFT_RUN_SNAPSHOT=<path>; the CLI flag "
		     "wins.  Missing snapshot in source-rebuild mode is a "
		     "hard fail.",
	)
	p.add_argument(
		"--package-root", type=UserPath, action="append", default=None,
		help="(--source-rebuild only, repeatable) Directory to walk "
		     "for `.dmp`/`.zdmp` package discovery — the run's "
		     "candidate pool.  When omitted, DRIFT_PKG_ROOT from the "
		     "certification env contract is used (os.pathsep-"
		     "separated list accepted); explicit flags win over the "
		     "env var.",
	)
	p.add_argument(
		"--json", action="store_true",
		help=f"Emit a structured `{_JSON_SCHEMA}` JSON object on "
		     "stdout instead of the space-separated flag line.  "
		     "Works in both lanes; the `evidence` key is present "
		     "only in source-rebuild mode (the strict lane has no "
		     "lock-vs-fresh comparison to report).",
	)
	return p


def run(argv: list[str] | None = None) -> int:
	"""Main entry point for ``drift lock emit``.  Returns exit code.

	0 — flags emitted to stdout.
	1 — lock missing / manifest missing / artifact not in lock /
	    malformed lock.  Error message points at `drift prepare`.
	2 — CLI misuse (argparse errors surface this automatically).
	"""
	parser = build_arg_parser()
	args = parser.parse_args(argv)
	try:
		return _run_impl(args)
	except LockEmitError as e:
		print(f"error: {e}", file=sys.stderr)
		return 1
	except ManifestError as e:
		print(f"error: {e}", file=sys.stderr)
		return 1
	except CertModeError as e:
		print(f"error: {e}", file=sys.stderr)
		return 1
	except KeyboardInterrupt:
		print("\ninterrupted", file=sys.stderr)
		return 130


def _emit(args: argparse.Namespace, flags: list[str], *, mode: str,
          evidence: dict | None = None) -> int:
	"""stdout contract for both lanes: exactly the flags (one
	space-separated line), or the `--json` v0 object.  Nothing else
	ever goes to stdout."""
	if args.json:
		payload: dict = {
			"schema": _JSON_SCHEMA,
			"artifact": args.artifact,
			"mode": mode,
			"dep_flags": flags,
		}
		if evidence is not None:
			payload["evidence"] = evidence
		print(json.dumps(payload, indent=2, sort_keys=True))
	else:
		# Space-separated flags on a single stdout line, trailing
		# newline.  Empty graph → empty line (no `--dep` flags),
		# still newline-terminated ($FLAGS expansion becomes a
		# no-op in the caller's shell, which is what we want).
		print(" ".join(flags))
	return 0


def _run_impl(args: argparse.Namespace) -> int:
	if not args.source_rebuild:
		if args.run_snapshot is not None:
			raise LockEmitError(
				"--run-snapshot has no meaning without --source-rebuild "
				"(the strict lane reads the committed lock; nothing is "
				"resolved).  Add --source-rebuild or drop the flag."
			)
		if args.package_root:
			raise LockEmitError(
				"--package-root has no meaning without --source-rebuild "
				"(the strict lane reads the committed lock; no package "
				"discovery happens).  Add --source-rebuild or drop the "
				"flag."
			)
	manifest_path: Path = args.manifest
	if not manifest_path.exists():
		raise LockEmitError(
			f"manifest not found: {manifest_path}"
		)
	# Load the manifest primarily to validate the artifact name
	# early with a clear error.  The lock is the authority for
	# resolved versions; the manifest only tells us which artifact
	# names are legitimate.
	manifest = load_manifest(manifest_path)
	artifact_names = {a.name for a in manifest.artifacts}
	if args.artifact not in artifact_names:
		known = ", ".join(sorted(artifact_names)) or "(none)"
		raise LockEmitError(
			f"artifact {args.artifact!r} not found in {manifest_path}.  "
			f"Known artifacts: {known}"
		)

	if args.source_rebuild:
		return _run_source_rebuild(args, manifest, manifest_path)

	lock_path = manifest_path.resolve().parent / "lock.json"
	if not lock_path.exists():
		raise LockEmitError(
			f"{lock_path} not found.  Run `drift prepare` to resolve "
			f"dependencies and write the lock before emitting flags."
		)
	try:
		lock = read_lock(lock_path)
	except ValueError as e:
		# read_lock raises ValueError with actionable text on
		# malformed / old-schema locks; pass it through with the
		# standard prepare pointer prepended for consistency.
		raise LockEmitError(
			f"failed to read {lock_path}: {e}"
		)

	if args.artifact not in lock:
		raise LockEmitError(
			f"artifact {args.artifact!r} not present in {lock_path}.  "
			f"The manifest declares it, but the lock has no entry — "
			f"likely stale.  Run `drift prepare` to refresh."
		)

	resolved = lock[args.artifact]
	flags = expand_to_dep_flags(resolved)
	return _emit(args, flags, mode="strict")


def _run_source_rebuild(args: argparse.Namespace, manifest,
                        manifest_path: Path) -> int:
	"""Certification-lane emit: fresh-resolve via the single
	source-rebuild authority; the lock (if present) is evidence
	only.  Mirrors `drift_prepare._run_impl`'s `--check
	--source-rebuild` snapshot/exemption handling exactly, so the
	resolver semantics a gate sees from this command are by
	construction those of the `driftc` build it feeds.

	stdout contract (the reason this lane exists — consumer cert
	gates exec this instead of sys.path-importing a drift-lang
	source checkout): exactly the `--dep` flags (or the --json
	object); everything diagnostic goes to stderr; any authority
	error ⇒ non-zero exit with empty stdout.
	"""
	from tools.drift_deploy.build_cmd import (
		producer_output_exemption_active,
	)
	from tools.drift_deploy.resolver import PackageEntry, ResolutionError
	from tools.drift_deploy.run_snapshot import load_run_snapshot
	from tools.drift_deploy.semver import parse_version
	from tools.drift_deploy.source_rebuild import (
		print_evidence,
		resolve_source_rebuild,
	)

	# Candidate pool: explicit --package-root flags win; otherwise
	# the certification env contract's DRIFT_PKG_ROOT (os.pathsep-
	# separated list) — the orchestrator supplies it, so the
	# announced invocation `drift lock emit --artifact X
	# --source-rebuild` works with no extra flags.
	package_roots = list(args.package_root or [])
	if not package_roots:
		env_roots = os.environ.get("DRIFT_PKG_ROOT", "")
		package_roots = [
			Path(p) for p in env_roots.split(os.pathsep) if p.strip()
		]
	if not package_roots:
		raise LockEmitError(
			"--source-rebuild requires a candidate package pool to "
			"walk for .dmp/.zdmp discovery: pass `--package-root "
			"<dir>` (repeatable) or set DRIFT_PKG_ROOT "
			"(os.pathsep-separated)."
		)

	snap_path = args.run_snapshot
	if snap_path is None:
		env_path = os.environ.get("DRIFT_RUN_SNAPSHOT", "")
		if env_path:
			snap_path = Path(env_path)
	if snap_path is None:
		raise LockEmitError(
			"--source-rebuild requires a run snapshot.  Pass "
			"`--run-snapshot <path>` or set DRIFT_RUN_SNAPSHOT=<path>.  "
			"The snapshot pins source identity per certification run; "
			"source-rebuild dep derivation cannot proceed without it."
		)
	try:
		run_snapshot = load_run_snapshot(Path(snap_path))
	except (ValueError, OSError) as e:
		raise LockEmitError(f"run snapshot load failed: {e}")

	artifact = next(a for a in manifest.artifacts if a.name == args.artifact)

	# Co-artifact overlays + stage-mode exemptions: identical to
	# `drift_prepare._run_impl`.  Certify (or unset DRIFT_CERT_MODE)
	# is the pure-consumer contract: no exemptions — every consumed
	# package must be in the snapshot.
	co_artifact_names = {a.name for a in manifest.artifacts if a.kind == "package"}
	exempt_ids: set[str] | None = (
		set(co_artifact_names)
		if producer_output_exemption_active()
		else None
	)
	co_artifact_entries: dict[str, PackageEntry] = {}
	for art in manifest.artifacts:
		if art.kind == "package":
			co_artifact_entries[art.name] = PackageEntry(
				package_id=art.name,
				version=parse_version(art.version),
				path=Path("/dev/null"),  # no .dmp yet
				sha256="",
				required_deps=[(d.name, d.version) for d in art.package_deps],
			)

	# Existing lock is EVIDENCE ONLY here — the certify pool is
	# candidate-only and the lock is never the graph authority under
	# source-rebuild.  A missing/unreadable lock therefore does not
	# fail the emit; it just means no drift evidence.
	lock_path = manifest_path.resolve().parent / "lock.json"
	existing_lock_graph = None
	if lock_path.exists():
		try:
			existing_lock_graph = read_lock(lock_path).get(args.artifact, {})
		except (ValueError, OSError) as e:
			print(
				f"drift lock emit --source-rebuild: note: {lock_path} "
				f"unreadable ({e}); emitting without lock-drift evidence",
				file=sys.stderr,
			)

	try:
		result = resolve_source_rebuild(
			artifact=artifact,
			package_roots=[Path(p) for p in package_roots],
			manifest_dir=manifest_path.resolve().parent,
			existing_lock_graph=existing_lock_graph,
			co_artifact_names=co_artifact_names,
			run_snapshot=run_snapshot,
			co_artifact_entries=co_artifact_entries,
			snapshot_exempt_ids=exempt_ids,
		)
	except ResolutionError as e:
		# Index-time failure (snapshot mismatch / missing snapshot
		# entry / discovery error) — same hard-fail class as
		# authority errors: nothing on stdout.
		raise LockEmitError(
			f"source-rebuild package index failed: {e}"
		)

	print_evidence(
		art_name=args.artifact,
		channel="drift lock emit",
		evidence=result.evidence,
		out=sys.stderr,
	)

	if result.errors:
		for err in result.errors:
			print(f"error: {err}", file=sys.stderr)
		return 1

	flags = expand_to_dep_flags(result.resolved_graph)
	ev = result.evidence
	evidence_json = {
		"added": [list(t) for t in ev.added],
		"removed": [list(t) for t in ev.removed],
		"version_changed": [list(t) for t in ev.version_changed],
		"sha_drift": [list(t) for t in ev.sha_drift],
		"signer_drift": [list(t) for t in ev.signer_drift],
	}
	return _emit(args, flags, mode="source-rebuild", evidence=evidence_json)


if __name__ == "__main__":
	sys.exit(run())
