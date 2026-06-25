# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
drift lock — read-only inspection helpers for `drift/lock.json`.

Current subcommand:

    drift lock emit --artifact <name> [--format driftc] [--manifest <path>]

Emits exact `--dep PKG@M.N.P` flags for the named artifact's resolved
graph.  Use in test runners / ad-hoc compile paths that need to pass
the lock's resolved versions to `driftc` without parsing JSON on the
bash side:

    DEP_FLAGS=$(drift lock emit --artifact singular)
    driftc $DEP_FLAGS --package-root <lib> tests/foo_test.drift -o build/foo

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
import sys
from pathlib import Path

from tools.drift_deploy.build_cmd import UserPath
from tools.drift_deploy.lockfile import expand_to_dep_flags, read_lock
from tools.drift_deploy.manifest import ManifestError, load_manifest


class LockEmitError(Exception):
	"""Fatal error while emitting lock data (missing lock, missing
	artifact, malformed lock)."""
	pass


_VALID_FORMATS = ("driftc",)


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
	except KeyboardInterrupt:
		print("\ninterrupted", file=sys.stderr)
		return 130


def _run_impl(args: argparse.Namespace) -> int:
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

	# Space-separated flags on a single stdout line, trailing
	# newline.  Empty graph → empty line (no `--dep` flags), still
	# newline-terminated.  Empty-artifact case ($FLAGS expansion
	# becomes a no-op in the caller's shell, which is what we want).
	print(" ".join(flags))
	return 0


if __name__ == "__main__":
	sys.exit(run())
