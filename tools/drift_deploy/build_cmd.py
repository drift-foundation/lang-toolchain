# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Shared artifact-to-driftc command builders.

Used by both ``drift build`` (local builds) and ``drift deploy``
(full publish pipeline).  Functions return ``list[str]`` command
vectors — callers handle execution, environment, and error reporting.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from tools.drift_deploy.lockfile import expand_to_dep_flags
from tools.drift_deploy.manifest import Artifact
from tools.drift_deploy.resolver import ResolvedDep


def UserPath(s: str) -> Path:
	"""Argparse type that expands ``~`` in path arguments."""
	return Path(s).expanduser()


def env_true(name: str) -> bool:
	"""Truthy check matching the rest of the toolchain's env-flag idiom."""
	return os.environ.get(name, "") in ("1", "true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON")


# ── Certification-run role (DRIFT_CERT_MODE) ─────────────────────────
#
# Normal teams do NOT set `DRIFT_CERT_MODE`.  `just test` / `drift
# build` / `drift deploy` run with the env unset and behave as plain
# local commands (strict lock semantics, producer-side publish).
#
# `DRIFT_CERT_MODE` is an ORCH CERTIFICATION-RUN signal.  Orch sets
# it for the whole run to name the PHASE explicitly, so command
# behaviour tracks phase instead of guessing from command name /
# flag presence:
#
#   stage    — orch certification staging phase.  Producer role.
#              Build/deploy a package into the run's `lib/`.  No run
#              snapshot required (the snapshot does not exist yet;
#              staging is what produces it).  Behaviourally
#              equivalent to "unset" today; exists so orch can
#              assert the phase without risking an accidental
#              certify-mode trigger.
#   certify  — orch certification verification phase.  Consumer
#              role.  Consume the staged graph; lock is evidence
#              only; resolver may float within declared compatible
#              ranges; disk packages must match the run snapshot.
#              Requires `DRIFT_RUN_SNAPSHOT=<path>` or
#              `--run-snapshot <path>`.  The value is `certify`
#              rather than `verify` because this is the orch
#              certification lane (fresh staged graph, range float,
#              lock drift as evidence), not generic verification.
#   unset    — normal local behaviour (strict lock semantics).
#
# The legacy `DRIFT_SOURCE_REBUILD=1` env var (landed 0.31.1,
# retired 0.31.5 after repeated phase-ordering bugs) is hard-
# rejected with migration guidance — see `cert_mode_from_env`.

CERT_MODE_STAGE = "stage"
CERT_MODE_CERTIFY = "certify"
_VALID_CERT_MODES: tuple[str, ...] = (CERT_MODE_STAGE, CERT_MODE_CERTIFY)


class CertModeError(Exception):
	"""Invalid `DRIFT_CERT_MODE` value, or use of a retired env var.

	Raised by `cert_mode_from_env` so callers (drift build / drift
	deploy / drift prepare) can convert to their native error type
	in a single place."""
	pass


def cert_mode_from_env() -> str | None:
	"""Parse `DRIFT_CERT_MODE`.

	Returns `"stage"` / `"certify"` / `None`.  Unrecognised values
	raise `CertModeError` so typos surface immediately.  The retired
	`DRIFT_SOURCE_REBUILD` env also raises `CertModeError` with a
	migration message pointing at `DRIFT_CERT_MODE`."""
	legacy = os.environ.get("DRIFT_SOURCE_REBUILD", "").strip()
	if legacy:
		raise CertModeError(
			"DRIFT_SOURCE_REBUILD was retired in 0.31.5.  The "
			"certification-run role is now named explicitly via "
			"DRIFT_CERT_MODE:\n"
			"  - DRIFT_CERT_MODE=stage    (orch certification "
			"staging; producer phase; no snapshot required)\n"
			"  - DRIFT_CERT_MODE=certify  (orch certification "
			"lane; consumer phase; requires DRIFT_RUN_SNAPSHOT or "
			"--run-snapshot)\n"
			"Normal local `just test` / `drift build` / `drift "
			"deploy` do NOT set this env — leave it unset for "
			"standard strict-lock behaviour.  See doc/history.md "
			"entry 0.31.5 for the redesign rationale."
		)
	raw = os.environ.get("DRIFT_CERT_MODE", "").strip()
	if not raw:
		return None
	if raw not in _VALID_CERT_MODES:
		raise CertModeError(
			f"DRIFT_CERT_MODE={raw!r} is not a valid value.  Expected "
			f"one of {list(_VALID_CERT_MODES)!r} or unset.  Leave the "
			f"env unset for normal local behaviour; only orch "
			f"certification runs set this env."
		)
	return raw


def source_rebuild_enabled(args) -> bool:
	"""Uniform source-rebuild selector for `drift build`,
	`drift deploy`, and `drift prepare --check`.

	Returns True iff EITHER:
	  - explicit `--source-rebuild` CLI flag is set, OR
	  - `DRIFT_CERT_MODE` is set (stage OR certify).

	Both stage and certify phases consume dependencies under
	source-rebuild semantics (fresh-resolve against the snapshot-
	gated index; lock becomes evidence, not gate).  The difference
	is WHICH packages must be in the snapshot:

	- `stage`  — producer role.  Consumed deps must be in the
	  snapshot, BUT the manifest's library-artifact peers (intra-
	  manifest co-artifacts that this deploy is itself producing)
	  are exempt.  See `producer_output_exemption_active`.
	- `certify` — consumer role.  Every consumed package must be
	  in the snapshot; no exemption.  `drift build` / `drift
	  prepare --check` under certify use this mode; no
	  producer-output exemption applies because those commands
	  aren't publishing.

	Under source-rebuild, each command's `_run_impl` separately
	requires a run snapshot (via `--run-snapshot` or
	`DRIFT_RUN_SNAPSHOT`) and hard-fails if none is supplied — the
	snapshot is a gate, not a hint.

	Unset `DRIFT_CERT_MODE` is normal local behaviour — strict lock
	semantics, no snapshot involved.  Normal teams never set the env.

	Env validation runs FIRST, unconditionally, before the CLI flag
	is consulted.  The retired `DRIFT_SOURCE_REBUILD` env and any
	malformed `DRIFT_CERT_MODE` raise `CertModeError` even when
	`--source-rebuild` is passed — a short-circuit on the flag
	would let a stale env value sit silently in orch / CI shells
	that happen to also pass the flag explicitly, which is the
	exact drift the retirement is meant to catch.  Callers wrap
	`CertModeError` into their native error type."""
	mode = cert_mode_from_env()
	return getattr(args, "source_rebuild", False) or mode in (CERT_MODE_STAGE, CERT_MODE_CERTIFY)


def producer_output_exemption_active() -> bool:
	"""True iff `DRIFT_CERT_MODE=stage`.

	Callers (`drift_deploy._run_impl`) use this to decide whether
	to thread their manifest's library-artifact names as
	`snapshot_exempt_ids` into `build_package_index`.  Stage is the
	only phase where intra-manifest co-artifacts legitimately
	bypass the snapshot gate — because this deploy is PRODUCING
	them.  Certify and manual `--source-rebuild` are pure consumer
	paths: no exemption.

	Separate from `source_rebuild_enabled` so the caller can ask
	the two questions (is source-rebuild active? is the stage
	producer-output exemption active?) without re-parsing the env.
	Raises `CertModeError` on retired env / malformed mode, same
	as the other helpers — env validation stays in one place."""
	return cert_mode_from_env() == CERT_MODE_STAGE


def resolve_driftc(explicit: Path | None = None) -> Path | None:
	"""
	Resolve the driftc binary path.

	Resolution order:
	  1. Explicit path (--driftc flag) — must exist.
	  2. Sibling ``driftc`` next to the running ``drift`` executable.
	     Follows symlinks so deployed layouts resolve correctly
	     (e.g. ``~/opt/drift/toolchain/bin/driftc``).
	  3. ``driftc`` from ``$PATH``.

	Returns the resolved Path, or None if not found.
	Raises ValueError if an explicit path was given but does not exist.
	"""
	# 1. Explicit.
	if explicit is not None:
		if not explicit.exists():
			raise ValueError(f"--driftc path does not exist: {explicit}")
		return explicit

	# 2. Sibling of the running executable.
	drift_exe = Path(os.path.realpath(sys.argv[0]))
	sibling = drift_exe.parent / "driftc"
	if sibling.is_file() and os.access(str(sibling), os.X_OK):
		return sibling

	# 3. PATH lookup.
	on_path = shutil.which("driftc")
	if on_path:
		return Path(on_path)

	return None


# `project_root_for` and `compute_artifact_sci` live in
# `lang/driftc/packages/manifest.py` so both `drift-author` (author
# tool) and this module (orch / deploy) can import them without
# crossing the author/deploy boundary in either direction.  Re-export
# here for back-compat with the many existing in-repo imports.
from lang.driftc.packages.manifest import (
	compute_artifact_sci,
	project_root_for,
	resolve_asset_files,
	resolve_module_files,
)


def build_source_args(art: Artifact, manifest_dir: Path) -> list[str]:
	"""
	Build the source file arguments: entry_module first, then remaining
	modules, deduplicated.

	Paths in the manifest are project-root-relative.  See ``project_root_for``
	for the resolution rule.  A ``modules[]`` entry that names a directory is
	expanded (recursively) to its ``.drift`` files via ``resolve_module_files``
	— the SAME expansion ``compute_artifact_sci`` uses, so the compiled set and
	the signed source identity stay in lock-step.
	"""
	root = project_root_for(manifest_dir)
	args: list[str] = []
	entry = str(root / art.entry_module)
	args.append(entry)
	for mod_path in resolve_module_files(art.modules, source_root=root):
		resolved_mod = str(root / mod_path)
		if resolved_mod != entry:
			args.append(resolved_mod)
	return args


def build_package_cmd(
	art: Artifact,
	*,
	driftc: Path,
	target: str,
	resolved_deps: dict[str, ResolvedDep],
	output_path: Path,
	manifest_dir: Path,
	package_roots: list[Path],
	native_lib_paths: list[Path] | None = None,
	trust_store: Path | None = None,
	extra_flags: list[str] | None = None,
	source_content_id: str | None = None,
) -> list[str]:
	"""Build the driftc command for a package artifact.

	If `source_content_id` is provided, it is stamped into the emitted
	`.dmp` manifest via `--source-content-id`.  The id is computed
	by drift_deploy from stable source inputs (see
	`lang.driftc.packages.source_content_id.compute_artifact_source_content_id`);
	driftc just records the value verbatim, it does not derive it.
	"""
	cmd = [
		str(driftc),
		"--emit-package", str(output_path),
		"--package-id", art.name,
		"--package-version", art.version,
		"--package-target", target,
	]
	if source_content_id is not None:
		cmd.extend(["--source-content-id", source_content_id])

	# Trust store for verifying co-artifact dependencies.
	if trust_store is not None:
		cmd.extend(["--trust-store", str(trust_store)])

	# Native deps.
	for nd in art.native_deps:
		cmd.extend(["--native-link-lib", nd.lib])

	# Declared assets: pack each into the .dmp as a content-addressed blob
	# under its project-relative logical path.  driftc reads the file bytes
	# and stamps `manifest.assets`; the consumer materializes them via
	# `drift unpack`.  Directory entries are expanded recursively to their
	# files via the SAME `resolve_asset_files` the SCI path uses, so the
	# packed asset blobs and the signed source identity stay in lock-step
	# (anchored at the project root, the SAME anchor as `compute_artifact_sci`).
	asset_root = project_root_for(manifest_dir)
	for asset_rel in resolve_asset_files(list(art.assets), source_root=asset_root):
		cmd.extend(["--asset", asset_rel, str(asset_root / asset_rel)])

	# Package dep declarations: only DIRECT deps (from manifest), and
	# they carry the **manifest's owner-declared range**, NOT the
	# lock's exact version.  This is the published constraint a
	# downstream consumer sees — shipping a producer's exact pin
	# here would leak the producer's local lock into consumer
	# resolution, forcing every intermediate library to republish on
	# every upstream patch bump.  Transitive deps do NOT appear here;
	# they flow to the compiler only via --dep (exact, from lock).
	for pd in art.package_deps:
		cmd.extend(["--package-dep", f"{pd.name}={pd.version}"])

	# Exact resolved versions via --dep (compiler version selection).
	cmd.extend(expand_to_dep_flags(resolved_deps))

	# Package roots.
	for pr in package_roots:
		cmd.extend(["--package-root", str(pr)])

	# Unsafe support for FFI packages.
	if art.unsafe:
		cmd.append("--allow-unsafe")

	# Native library search paths (resolver input, not package metadata).
	for nlp in (native_lib_paths or []):
		cmd.extend(["--link-search", str(nlp)])

	# Extra passthrough flags.
	if extra_flags:
		cmd.extend(extra_flags)

	# Project root for relativizing emitted debug-location source paths, so the
	# .dmp (and thus artifact_sha256) is byte-identical regardless of the
	# absolute checkout/build path.  Same root the source inputs are anchored to.
	cmd.extend(["--package-source-root", str(project_root_for(manifest_dir))])

	# Source inputs.
	cmd.extend(build_source_args(art, manifest_dir))

	return cmd


def build_app_cmd(
	art: Artifact,
	*,
	driftc: Path,
	target: str,
	resolved_deps: dict[str, ResolvedDep],
	output_path: Path,
	manifest_dir: Path,
	package_roots: list[Path],
	native_lib_paths: list[Path] | None = None,
	trust_store: Path | None = None,
	extra_flags: list[str] | None = None,
) -> list[str]:
	"""Build the driftc command for an app artifact."""
	cmd = [str(driftc), "-o", str(output_path)]

	# Native target: emit --target-word-bits for host.
	if target == "native":
		import struct
		word_bits = struct.calcsize("P") * 8
		cmd.extend(["--target-word-bits", str(word_bits)])

	# Custom entry point (e.g. "pushcoin.bookkeeper::main").
	if art.entry_point:
		cmd.extend(["--entry", art.entry_point])

	# Trust store for verifying package dependencies.
	if trust_store is not None:
		cmd.extend(["--trust-store", str(trust_store)])

	# Exact resolved versions via --dep.
	cmd.extend(expand_to_dep_flags(resolved_deps))

	# Package roots.
	for pr in package_roots:
		cmd.extend(["--package-root", str(pr)])

	# Native deps for link-time.
	for nd in art.native_deps:
		cmd.extend(["--link-lib", nd.lib])

	# Unsafe support for FFI apps.
	if art.unsafe:
		cmd.append("--allow-unsafe")

	# Native library search paths (resolver input, not package metadata).
	for nlp in (native_lib_paths or []):
		cmd.extend(["--link-search", str(nlp)])

	# Extra passthrough flags.
	if extra_flags:
		cmd.extend(extra_flags)

	# Source inputs.
	cmd.extend(build_source_args(art, manifest_dir))

	return cmd
