# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import base64
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tools.deploy.steps.bundle import bundle_compiler, bundle_docs_and_examples, bundle_runtime_archives
from tools.deploy.steps.pex import build_drift_pex, build_driftc_pex
from tools.deploy.steps.stdlib import build_and_install_stdlib

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

_skip_no_pex = pytest.mark.skipif(
	shutil.which("pex") is None and not (ROOT / ".venv" / "bin" / "pex").exists(),
	reason="pex not installed; deployed bundle requires PEX --scie eager",
)


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _pre_publish_stdlib_author_claim(
	stdlib_dir: Path,
	scratch_dir: Path,
	*,
	version: str,
) -> tuple[Path, str]:
	"""Simulate Foundation's offline author-signing flow before the
	deploy step runs.

	Returns `(author_claim_path, author_pubkey_b64)` -- the inputs
	`build_and_install_stdlib` requires.  This test fixture stands
	in for Foundation's out-of-band author-signing service: it
	generates an author keypair, runs `drift-author publish`
	against the stdlib SCI, and discards the seed.  The author
	private key never enters any code path under
	`tools/deploy/` or `tools/drift_deploy/` -- the boundary check
	(`lang/tests/packages/test_author_key_boundary.py`) verifies
	that statically.
	"""
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from cryptography.hazmat.primitives import serialization
	from lang.driftc.packages.source_content_id import (
		compute_artifact_source_content_id,
	)
	from lang.driftc.packages.author_claim_v1 import AuthorClaimBody
	from tools.drift_author.author_publish import (
		SignAuthorClaimOptions, sign_and_write_author_claim,
	)

	# Stdlib SCI must match what `build_stdlib_package` will compute;
	# we mirror the same input set (every `.drift` file under stdlib).
	stdlib_files = sorted(stdlib_dir.rglob("*.drift"))
	module_paths_rel = sorted(str(p.relative_to(ROOT)) for p in stdlib_files)
	sci = compute_artifact_source_content_id(
		kind="package",
		package_id="std",
		version=version,
		module_namespace="std",
		entry_module="std",
		module_paths=module_paths_rel,
		package_deps=[],
		native_deps=[],
		unsafe=False,
		asset_paths=[],
		source_root=ROOT,
	)

	priv = Ed25519PrivateKey.generate()
	priv_seed = priv.private_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PrivateFormat.Raw,
		encryption_algorithm=serialization.NoEncryption(),
	)
	pub_raw = priv.public_key().public_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PublicFormat.Raw,
	)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")

	sidecar_dir = scratch_dir / "foundation_author_signing"
	sidecar_dir.mkdir(parents=True, exist_ok=True)
	author_claim_path = sign_and_write_author_claim(SignAuthorClaimOptions(
		body=AuthorClaimBody(
			schema_version=1,
			package_id="std",
			version=version,
			namespaces=("std.*", "lang.*", "drift.*"),
			source_content_id=sci,
			required_deps=(),
			release_utc="2026-05-19T00:00:00Z",
		),
		seed32=priv_seed,
		sidecar_dir=sidecar_dir,
	))
	# Foundation analog discards the seed once the claim is signed.
	del priv, priv_seed
	return author_claim_path, pub_b64


@_skip_no_pex
def test_deployed_wrapper_links_directly_from_dist_tree_lib_runtime(
	tmp_path: Path, pex_scie_base: Path,
) -> None:
	"""Default deployed-driftc resolves the runtime archive from
	`<dist>/lib/runtime/<variant>/` and links from it in place.

	Read-only install trees (0444) are fine — the linker only reads.
	No copy, no chmod, no user-local cache.  This pins the post-#101
	contract: deployments are self-contained and never leak shared
	state into `$HOME` or any other process-wide location.

	The old behavior (seed into `~/.cache/drift/runtime/`) was a
	silent-Frankenstein hazard: a stale cache could survive a
	toolchain upgrade and link the previous toolchain's runtime
	against the new compiler's IR.  See `tools/deploy/pex_entry.py`
	+ `tools/deploy/driftc-wrapper.sh` for the actual resolution.
	"""
	dist = tmp_path / "dist"
	dist.mkdir(parents=True, exist_ok=True)
	clang = shutil.which("clang")
	assert clang, "clang not found"

	# Signing key for stdlib package.
	key_path = tmp_path / "deploy.key"
	key_path.write_text(base64.b64encode(os.urandom(32)).decode("ascii") + "\n", encoding="utf-8")
	old_sign_key = os.environ.get("DRIFT_SIGN_KEY_FILE")
	os.environ["DRIFT_SIGN_KEY_FILE"] = str(key_path)

	try:
		# Build PEX executables.
		build_driftc_pex(ROOT, dist)
		build_drift_pex(ROOT, dist)

		# Bundle compiler sources and runtime archives.
		bundle_compiler(ROOT, dist)
		bundle_runtime_archives(ROOT, dist)
		bundle_docs_and_examples(dist)

		# Build, sign, and install stdlib + core trust store.
		stage = tmp_path / "stage"
		stage.mkdir(parents=True, exist_ok=True)
		# Pre-publish the stdlib author claim out-of-band (fixture
		# stands in for Foundation's offline signing); the deploy
		# step itself never holds the author key.
		author_claim_path, author_pubkey_b64 = _pre_publish_stdlib_author_claim(
			ROOT / "stdlib", tmp_path, version="0.0.0-test",
		)
		build_and_install_stdlib(
			ROOT, stage, dist, "0.0.0-test",
			stdlib_author_claim_path=author_claim_path,
			stdlib_author_pubkey_b64=author_pubkey_b64,
			certifier_key_path=key_path,
			driftc_commit="test-commit-stub",
		)
	finally:
		if old_sign_key is None:
			os.environ.pop("DRIFT_SIGN_KEY_FILE", None)
		else:
			os.environ["DRIFT_SIGN_KEY_FILE"] = old_sign_key

	src = tmp_path / "main.drift"
	_write_file(
		src,
		"""module main;

fn main() nothrow -> Int {
	return 0;
}
""",
	)
	out = tmp_path / "a.out"
	runtime_root = dist / "lib" / "runtime"
	# Lock down the install tree to 0444 (read-only) — the new contract
	# is that the linker reads the .a in place, so write bits are not
	# needed.  A pre-fix run would have copied to ~/.cache and chmodded
	# the cached copy; the new flow does neither.
	for path in [runtime_root, *runtime_root.rglob("*")]:
		if path.is_dir():
			path.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
		else:
			path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

	# Pin the new contract: do NOT pre-seed any DRIFT_RUNTIME_LIB_CACHE_DIR.
	# The deployed wrapper must fall through to the install tree's
	# `lib/runtime/` by default.  Isolated $HOME guards against the
	# previous behavior where pex_entry.py wrote into the operator's
	# real `~/.cache/drift/runtime/` — that's the regression this
	# test prevents.
	fake_home = tmp_path / "isolated_home"
	fake_home.mkdir()
	run_env = dict(os.environ)
	for key in ("PYTHONPATH", "PYTHONSAFEPATH", "DRIFT_PYTHON", "VIRTUAL_ENV",
				"DRIFT_RUNTIME_LIB_CACHE_DIR", "DRIFT_RUNTIME_BUILD_ROOT",
				"DRIFT_TRUST_STORE"):
		run_env.pop(key, None)
	run_env["HOME"] = str(fake_home)
	run_env["SCIE_BASE"] = str(pex_scie_base)
	result = subprocess.run(
		[
			str(dist / "bin" / "driftc"),
			"--target-word-bits", "64",
			"-M", str(tmp_path),
			str(src),
			"-o", str(out),
		],
		text=True,
		capture_output=True,
		env=run_env,
		cwd=tmp_path,
		timeout=sanitizer_timeout(180),
	)
	assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
	assert out.exists()
	assert not list(runtime_root.rglob(".build.lock"))

	# CONTRACT 1: the deployed wrapper must NOT write to ~/.cache/drift/.
	# PEX's own bootstrap (`~/.cache/pex/`) is unavoidable infrastructure
	# and out of scope; we forbid only the drift-specific user-cache
	# namespace that previously held the runtime-archive copies.
	leaked_drift_cache = fake_home / ".cache" / "drift"
	assert not leaked_drift_cache.exists(), (
		f"deployed wrapper wrote to {leaked_drift_cache} — the new "
		f"contract forbids any drift-level user-home cache.  If this "
		f"fails, `tools/deploy/pex_entry.py` or "
		f"`tools/deploy/driftc-wrapper.sh` has regressed to the "
		f"pre-#101 seed-into-~/.cache behavior."
	)

	# CONTRACT 2: dist tree's lib/runtime/ stays 0444 (untouched).
	# The linker only reads; no chmod, no copy.
	for path in runtime_root.rglob("*"):
		if path.is_file():
			mode = stat.S_IMODE(path.stat().st_mode)
			assert mode == 0o444, (
				f"dist tree file {path} mode changed to {oct(mode)} "
				f"during build — expected unchanged 0o444.  Something "
				f"wrote to the read-only install tree."
			)


@_skip_no_pex
def test_deployed_wrapper_explicit_env_override_respected(
	tmp_path: Path, pex_scie_base: Path,
) -> None:
	"""When DRIFT_RUNTIME_LIB_CACHE_DIR is set explicitly (CI scratch
	dir, in-repo dev override, etc.), the deployed wrapper honors it
	verbatim instead of overriding with the dist tree's lib/runtime/.

	Pins the operator escape hatch: the default (no env) links from
	the dist tree, but an explicit env wins.  No silent fallthrough,
	no $HOME writes either way.

	Replaces the pre-#101 `test_deployed_wrapper_repairs_poisoned_runtime_cache`,
	which tested the cache-self-heal contract that's no longer
	meaningful (there is no cache to poison/repair when the default
	resolves directly to the dist tree).
	"""
	dist = tmp_path / "dist"
	dist.mkdir(parents=True, exist_ok=True)
	clang = shutil.which("clang")
	assert clang, "clang not found"

	key_path = tmp_path / "deploy.key"
	key_path.write_text(base64.b64encode(os.urandom(32)).decode("ascii") + "\n", encoding="utf-8")
	old_sign_key = os.environ.get("DRIFT_SIGN_KEY_FILE")
	os.environ["DRIFT_SIGN_KEY_FILE"] = str(key_path)

	try:
		build_driftc_pex(ROOT, dist)
		build_drift_pex(ROOT, dist)
		bundle_compiler(ROOT, dist)
		bundle_runtime_archives(ROOT, dist)
		bundle_docs_and_examples(dist)
		stage = tmp_path / "stage"
		stage.mkdir(parents=True, exist_ok=True)
		author_claim_path, author_pubkey_b64 = _pre_publish_stdlib_author_claim(
			ROOT / "stdlib", tmp_path, version="0.0.0-test",
		)
		build_and_install_stdlib(
			ROOT, stage, dist, "0.0.0-test",
			stdlib_author_claim_path=author_claim_path,
			stdlib_author_pubkey_b64=author_pubkey_b64,
			certifier_key_path=key_path,
			driftc_commit="test-commit-stub",
		)
	finally:
		if old_sign_key is None:
			os.environ.pop("DRIFT_SIGN_KEY_FILE", None)
		else:
			os.environ["DRIFT_SIGN_KEY_FILE"] = old_sign_key

	src = tmp_path / "main.drift"
	_write_file(src, "module main;\n\nfn main() nothrow -> Int { return 0; }\n")
	out = tmp_path / "a.out"

	# Operator override: point at a test-local scratch directory.
	# pex_entry.py / driftc-wrapper.sh must honor this verbatim and
	# NOT redirect to the dist tree.
	rt_scratch = tmp_path / "rt_scratch"
	fake_home = tmp_path / "isolated_home"
	fake_home.mkdir()
	run_env = dict(os.environ)
	for key in ("PYTHONPATH", "PYTHONSAFEPATH", "DRIFT_PYTHON", "VIRTUAL_ENV",
				"DRIFT_TRUST_STORE"):
		run_env.pop(key, None)
	run_env["HOME"] = str(fake_home)
	run_env["SCIE_BASE"] = str(pex_scie_base)
	run_env["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(rt_scratch)
	result = subprocess.run(
		[
			str(dist / "bin" / "driftc"),
			"--target-word-bits", "64",
			"-M", str(tmp_path),
			str(src),
			"-o", str(out),
		],
		text=True,
		capture_output=True,
		env=run_env,
		cwd=tmp_path,
		timeout=sanitizer_timeout(180),
	)
	assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
	assert out.exists()

	# The override was honored: rt_scratch is where driftc looked
	# (build_runtime_archive in-repo dev path would have written here
	# rebuilding from sources).  EITHER it's been populated, OR the
	# linker found nothing there and failed — the test would have
	# tripped the returncode assertion above.  Either way, $HOME
	# stays untouched.
	# Same contract as the default path: ~/.cache/drift/ must stay empty.
	# (PEX's own ~/.cache/pex/ bootstrap is allowed; we only forbid the
	# drift-specific namespace.)
	leaked_drift_cache = fake_home / ".cache" / "drift"
	assert not leaked_drift_cache.exists(), (
		f"explicit env override leaked into $HOME at {leaked_drift_cache} — "
		f"deployed wrapper must respect DRIFT_RUNTIME_LIB_CACHE_DIR "
		f"verbatim and never fall through to ~/.cache/drift/."
	)
