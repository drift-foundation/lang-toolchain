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
		kind="library",
		package_id="std",
		version=version,
		module_namespace="std",
		entry_module="std",
		module_paths=module_paths_rel,
		package_deps=[],
		native_deps=[],
		unsafe=False,
		asset_paths=[],
		target_class="drift-dev",
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
			target_class="library",
			release_utc="2026-05-19T00:00:00Z",
		),
		seed32=priv_seed,
		sidecar_dir=sidecar_dir,
	))
	# Foundation analog discards the seed once the claim is signed.
	del priv, priv_seed
	return author_claim_path, pub_b64


@_skip_no_pex
def test_deployed_wrapper_uses_runtime_archives_without_writing_install_tree(
	tmp_path: Path, pex_scie_base: Path,
) -> None:
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
	for path in [runtime_root, *runtime_root.rglob("*")]:
		if path.is_dir():
			path.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
		else:
			path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

	# Isolate the deployed wrapper's runtime-cache seeding into a
	# test-local directory.  Without this, `pex_entry.py` writes
	# through to `~/.cache/drift/runtime/<variant>/` with the
	# operator's real `$HOME`.  Historically that poisoned operator
	# caches with the 0444 mode inherited from the read-only dist
	# tree; even with the 0o664 fix in `pex_entry.py`, a test has no
	# business touching the operator's real cache.
	rt_cache = tmp_path / "rt_cache"
	run_env = dict(os.environ)
	for key in ("PYTHONPATH", "PYTHONSAFEPATH", "DRIFT_PYTHON", "VIRTUAL_ENV"):
		run_env.pop(key, None)
	run_env["SCIE_BASE"] = str(pex_scie_base)
	run_env["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(rt_cache)
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
		timeout=180,
	)
	assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
	assert out.exists()
	assert not list(runtime_root.rglob(".build.lock"))

	# Coverage pin: the seeded cache archive MUST be owner-writable.
	# This is the exact shape the orch-team report caught — under
	# the old `shutil.copy2` seeding, the archive inherited 0444
	# from the read-only dist tree and every subsequent
	# `ar` rebuild failed with "Permission denied".
	assert rt_cache.is_dir(), (
		f"expected deployed wrapper to seed runtime cache at {rt_cache}, "
		f"but the directory does not exist — DRIFT_RUNTIME_LIB_CACHE_DIR "
		f"handoff broken?"
	)
	_seeded_archives = list(rt_cache.rglob("libdrift_rt*.a"))
	assert _seeded_archives, (
		f"expected at least one runtime archive seeded into {rt_cache}; "
		f"found none — cache-seeding loop in `pex_entry.py` may have "
		f"skipped every variant (read-only install tree is the scenario "
		f"this code exists to support)"
	)
	for _archive in _seeded_archives:
		_mode = stat.S_IMODE(_archive.stat().st_mode)
		assert _mode & stat.S_IWUSR, (
			f"seeded runtime archive {_archive} has mode {oct(_mode)} — "
			f"missing owner-write bit.  `pex_entry.py` must copy "
			f"with content-only semantics and force 0o664, not "
			f"`shutil.copy2` which preserves the read-only source "
			f"mode of a 0444 install tree.  Next rebuild attempt "
			f"through ar would fail with 'Permission denied' and "
			f"poison the operator cache."
		)


@_skip_no_pex
def test_deployed_wrapper_repairs_poisoned_runtime_cache(
	tmp_path: Path, pex_scie_base: Path,
) -> None:
	"""Pin the poisoned-cache recovery path.

	Operators who hit the pre-fix `shutil.copy2` bug now have a
	0444 archive sitting in `~/.cache/drift/runtime/<variant>/`.
	The corrected `pex_entry.py` must chmod those archives back to
	0o664 on subsequent invocations so the user recovers without
	manual `chmod u+w` / `rm` intervention.  This test pre-creates
	a 0444 archive in the test-local cache, runs the deployed
	wrapper, and asserts the archive is repaired to owner-writable.
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

	# Pre-seed a poisoned cache: copy an archive from the dist tree
	# into the test-local cache and chmod it to 0444 to simulate a
	# pre-fix operator cache state.
	runtime_root = dist / "lib" / "runtime"
	rt_cache = tmp_path / "rt_cache"
	_poisoned_archives: list[Path] = []
	for _variant_dir in sorted(runtime_root.iterdir()):
		if not _variant_dir.is_dir():
			continue
		for _ar in _variant_dir.glob("libdrift_rt*.a"):
			_cache_variant = rt_cache / _variant_dir.name
			_cache_variant.mkdir(parents=True, exist_ok=True)
			_cache_ar = _cache_variant / _ar.name
			shutil.copyfile(_ar, _cache_ar)
			_cache_ar.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
			_poisoned_archives.append(_cache_ar)
			break
	assert _poisoned_archives, "precondition: at least one variant must seed a poisoned archive"

	run_env = dict(os.environ)
	for key in ("PYTHONPATH", "PYTHONSAFEPATH", "DRIFT_PYTHON", "VIRTUAL_ENV"):
		run_env.pop(key, None)
	run_env["SCIE_BASE"] = str(pex_scie_base)
	run_env["DRIFT_RUNTIME_LIB_CACHE_DIR"] = str(rt_cache)
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
		timeout=180,
	)
	assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

	# Every pre-poisoned archive must now be owner-writable.  The
	# repair path in `pex_entry.py` runs on every invocation when
	# the cache archive already exists — this is the self-healing
	# contract the orch-team report asked for.
	for _archive in _poisoned_archives:
		_mode = stat.S_IMODE(_archive.stat().st_mode)
		assert _mode & stat.S_IWUSR, (
			f"poisoned cache archive {_archive} still has mode "
			f"{oct(_mode)} after deployed-wrapper invocation.  "
			f"`pex_entry.py` must chmod to 0o664 even when the "
			f"archive already exists, so operators recover from "
			f"the pre-fix 0444 state automatically."
		)
