# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""W3 final gates: lane-divergent dependency stamps + post-deploy
extraction.

Gate 1 (lane divergence): binaries built through the PRODUCTION argv
builder (`build_app_cmd`) with each lane's resolved dependency graph —
strict = the committed lock's pin, certify = a REAL
`resolve_source_rebuild` fresh resolution against a snapshot-gated pool
carrying a newer in-range version — must carry DIFFERENT `dependencies`
stamps, proven by extracting build-info from the RESULTING BINARIES via
the shared production reader.

Gate 2 (post-deploy): after the production deploy processing steps run
against a real stamped binary — baseline smoke (`_run_baseline_smoke_app`),
cert-claim signing (`_emit_cert_claim_for_artifact`), and publish
(`_publish_app` copytree) — the PUBLIC `drift inspect build-info` CLI
must extract the intact document from the FINAL PUBLISHED binary.
"""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.build_info import extract_build_info
from lang.driftc.packages.manifest import Artifact
from tools.drift_deploy.build_cmd import build_app_cmd

ROOT = Path(__file__).resolve().parents[2]


def _driftc(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc"] + args,
		capture_output=True, text=True, cwd=ROOT,
		timeout=sanitizer_timeout(timeout))
	assert res.returncode == 0, res.stderr[-900:]
	return res


def _emit_dep(pool: Path, version: str) -> None:
	lib = pool / f"src-{version}"
	lib.mkdir(parents=True)
	(lib / "extlib.drift").write_text(
		"module extlib;\n"
		"export { answer };\n"
		f"pub fn answer() nothrow -> Int {{ return 0; }}  // v{version}\n")
	_driftc([
		"--target-word-bits", "64", "-M", str(pool),
		str(lib / "extlib.drift"),
		"--emit-package", str(pool / f"extlib-{version}.dmp"),
		"--package-id", "extlib", "--package-version", version,
		"--package-target", "test-target"])


def _write_project(tmp_path: Path) -> Path:
	"""App project with a manifest declaring extlib range "1.0" —
	parsed by the PRODUCTION manifest loader."""
	proj = tmp_path / "proj"
	(proj / "drift").mkdir(parents=True)
	(proj / "main.drift").write_text(
		"module main;\n"
		"import extlib as extlib;\n"
		"pub fn main() nothrow -> Int { return extlib.answer(); }\n")
	(proj / "drift" / "manifest.json").write_text(json.dumps({
		"schema_version": 2,
		"project": {"name": "laneproj", "license": "MIT"},
		"artifacts": [{
			"kind": "app", "name": "laneapp", "version": "0.1.0",
			"description": "lane divergence probe", "license": "MIT",
			"entry_module": "main.drift", "modules": ["main.drift"],
			"entry_point": "main::main",
			"package_deps": [{"name": "extlib", "version": "1.0"}],
		}],
	}))
	return proj


def _build_app(tmp_path: Path, proj: Path, pool: Path,
               resolved: dict, out_name: str) -> Path:
	from lang.driftc.packages.manifest import load_manifest
	art = load_manifest(proj / "drift" / "manifest.json").artifacts[0]
	out = tmp_path / out_name
	cmd = build_app_cmd(
		art,
		driftc=Path(sys.executable),
		target="native",
		resolved_deps=resolved,
		output_path=out,
		manifest_dir=proj / "drift",
		package_roots=[pool],
		extra_flags=["-M", str(proj), "--allow-unsigned-from", str(pool)],
	)
	exec_cmd = [cmd[0], "-m", "lang.driftc.driftc"] + cmd[1:]
	res = subprocess.run(exec_cmd, capture_output=True, text=True, cwd=ROOT,
	                     timeout=sanitizer_timeout(180))
	assert res.returncode == 0, res.stderr[-900:]
	assert out.exists()
	return out


class TestLaneDivergentStamps:
	def test_strict_vs_certify_stamps_differ(self, tmp_path) -> None:
		"""The real recertification flow: `drift prepare` writes the
		lock against the strict pool (only 1.0.0 exists); the pool then
		MOVES (certify pool carries only 1.0.1, snapshot-authorised);
		BOTH lanes resolve through the production
		`drift_build._resolve_deps` — strict from the lock, certify via
		the source-rebuild authority with the now-stale lock as
		evidence — and the binaries' EXTRACTED stamps diverge."""
		from unittest.mock import patch as _patch
		from tools.drift_deploy.drift_build import _resolve_deps
		from tools.drift_deploy.drift_prepare import (
			_run_impl as prepare_run_impl,
			build_arg_parser as prepare_parser,
		)
		from tools.drift_deploy.run_snapshot import (
			SnapshotEntry, write_run_snapshot, load_run_snapshot,
		)
		from lang.driftc.packages.manifest import load_manifest

		# Actual pool movement: two DISTINCT pools.
		pool_strict = tmp_path / "pool-strict"
		pool_certify = tmp_path / "pool-certify"
		_emit_dep(pool_strict, "1.0.0")
		_emit_dep(pool_certify, "1.0.1")

		proj = _write_project(tmp_path)
		manifest_path = proj / "drift" / "manifest.json"
		art = load_manifest(manifest_path).artifacts[0]

		scid = "sha256:" + "a" * 64
		ak = "ed25519:orch-sig-kid"
		sak = "ed25519:orch-sak-kid"
		snap = tmp_path / "run-snapshot.json"
		write_run_snapshot(snap, run_id="20260731-lane-divergence", entries={
			("extlib", "1.0.1"): SnapshotEntry(
				source_content_id=scid, author_key=ak,
				source_attestation_key=sak),
		})

		with _patch("tools.drift_deploy.resolver._read_author_key",
		            return_value=ak), \
		     _patch("tools.drift_deploy.resolver._read_source_attestation_meta",
		            return_value=(scid, sak)):
			# ── strict lane: PRODUCTION lock path — prepare writes
			# the lock against the strict pool, build reads+verifies ──
			rc = prepare_run_impl(prepare_parser().parse_args([
				"--manifest", str(manifest_path),
				"--package-root", str(pool_strict)]))
			assert rc == 0
			strict_graph = _resolve_deps(
				art, proj / "drift", [pool_strict])
			assert strict_graph["extlib"].version == "1.0.0"

			# ── certify lane: the pool MOVED; same (stale) lock is
			# evidence; the source-rebuild authority fresh-resolves ──
			certify_graph = _resolve_deps(
				art, proj / "drift", [pool_certify],
				source_rebuild=True,
				run_snapshot=load_run_snapshot(snap))
			assert certify_graph["extlib"].version == "1.0.1"

		strict_bin = _build_app(
			tmp_path, proj, pool_strict, strict_graph, "app-strict")
		certify_bin = _build_app(
			tmp_path, proj, pool_certify, certify_graph, "app-certify")

		# ── the gate: extracted stamps DIFFER per lane ──
		strict_doc = json.loads(extract_build_info(strict_bin))
		certify_doc = json.loads(extract_build_info(certify_bin))
		assert strict_doc["dependencies"] == [
			{"name": "extlib", "version": "1.0.0"}]
		assert certify_doc["dependencies"] == [
			{"name": "extlib", "version": "1.0.1"}]
		assert strict_doc["dependencies"] != certify_doc["dependencies"]
		# Same identity stamp either way — only the lane-resolved
		# dependency set moved.
		assert strict_doc["artifact"] == certify_doc["artifact"]


class TestPostDeployExtraction:
	def test_public_cli_reads_published_binary(self, tmp_path) -> None:
		from tools.drift_deploy.drift_deploy import (
			CertSuiteOptions,
			_emit_cert_claim_for_artifact,
			_publish_app,
			_run_baseline_smoke_app,
		)
		art = Artifact(
			kind="app", name="pubapp", version="0.2.0",
			description="post-deploy probe ☃", license="MIT",
			entry_module="main.drift", modules=["main.drift"],
			entry_point="main::main")
		# Real stamped build (identity flags exactly as drift build
		# passes them).
		src_dir = tmp_path / "src"
		src_dir.mkdir()
		(src_dir / "main.drift").write_text(
			"module main;\npub fn main() nothrow -> Int { return 0; }\n")
		staged = tmp_path / "staging"
		staged.mkdir()
		staged_bin = staged / art.name
		_driftc([
			str(src_dir / "main.drift"), "-M", str(src_dir),
			"--entry", "main::main", "-o", str(staged_bin),
			"--artifact-name", art.name,
			"--artifact-version", art.version,
			"--artifact-description", art.description,
			"--artifact-license", art.license])

		# ── production deploy processing: smoke → sign → publish ──
		_run_baseline_smoke_app(art, staged_bin=staged_bin)
		seed = tmp_path / "cert.seed"
		seed.write_text(base64.b64encode(bytes(range(32))).decode("ascii"))
		prov = staged / f"{art.name}.provenance.zst"
		prov.write_bytes(b"(provenance bundle stub)")
		sha = "sha256:" + hashlib.sha256(staged_bin.read_bytes()).hexdigest()
		from tools.drift_deploy.provenance import CompilerInfo
		sidecar = _emit_cert_claim_for_artifact(
			staged_bin, cert_key=seed, package_id=art.name,
			package_version=art.version, artifact_kind="app",
			target="linux-x86_64",
			compiler_info=CompilerInfo(version="0.33.93", abi=22, commit=""),
			source_content_id="sha256:" + "a" * 64,
			artifact_sha256=sha,
			resolved_deps={}, direct_dep_ids=set(),
			staged_pkg_root=staged, provenance_path=prov,
			cert_suite_options=CertSuiteOptions(
				id="test/gate", version="1.0", result="pass",
				result_evidence_sha256="sha256:" + "f" * 64,
				no_evidence_sentinel=False))
		assert sidecar.exists()
		app_dest = tmp_path / "published"
		pub_dir = _publish_app(art, staged_install=staged, app_dest=app_dest)
		published_bin = pub_dir / art.name
		assert published_bin.exists()

		# ── the gate: the PUBLIC CLI reads the PUBLISHED binary ──
		code = ("import sys\nfrom lang.drift.cli import main\n"
		        "sys.exit(main(sys.argv[1:]))")
		res = subprocess.run(
			[sys.executable, "-c", code, "inspect", "build-info",
			 str(published_bin), "--json"],
			capture_output=True, cwd=ROOT, timeout=60)
		assert res.returncode == 0, res.stderr[-400:]
		doc = json.loads(res.stdout)
		assert doc["artifact"] == {
			"name": "pubapp", "version": "0.2.0",
			"description": "post-deploy probe ☃", "license": "MIT"}
		# Byte-identical to the staged binary's stamp: smoke/sign/
		# publish never perturbed the section.
		assert res.stdout == extract_build_info(staged_bin).encode("utf-8") + b"\n"
