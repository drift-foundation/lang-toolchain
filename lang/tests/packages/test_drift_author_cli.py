# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
End-to-end CLI regression for `drift-author publish` / `cosign`.

Exercises the argparse layer directly via `cli.main(argv)` so we
don't fork a subprocess in the unit tier.  Confirms:

  - `publish` produces a sidecar at the canonical name with a
    well-formed claim;
  - mutually-exclusive `--key-file` / `--key-text` is enforced;
  - missing required body fields exit non-zero;
  - `cosign` appends a co-author signature against an existing
    sidecar;
  - `--json` emits a machine-readable handle to the written path.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from lang.driftc.packages.author_claim_v1 import load_author_claim_json
from lang.driftc.packages.sidecar_naming import author_claim_filename
from tools.drift_author.cli import main as author_cli_main


def _seed_b64() -> str:
	return base64.b64encode(bytes(range(32))).decode("ascii")


def _publish_argv(tmp_path: Path, key_text: str) -> list[str]:
	"""Raw-mode invocation.  After the CLI rename, the manifest-less
	(hand-entered-SCI) path is `publish-raw`; `publish` is the
	manifest-aware default exercised by `TestPublishFromManifest`.
	"""
	return [
		"publish-raw",
		"--sidecar-dir", str(tmp_path),
		"--package-id", "demo.lib",
		"--version", "1.0.0",
		"--namespace", "demo.lib",
		"--source-content-id", "sha256:" + ("a" * 64),
		"--release-utc", "2026-05-19T00:00:00Z",
		"--required-dep", "std=^1",
		"--key-text", key_text,
	]


def test_publish_writes_canonical_sidecar(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	rc = author_cli_main(_publish_argv(tmp_path, _seed_b64()))
	assert rc == 0
	written = tmp_path / author_claim_filename("demo.lib")
	assert written.is_file()
	claim = load_author_claim_json(written.read_text(encoding="utf-8"))
	assert claim.body.package_id == "demo.lib"
	assert claim.body.version == "1.0.0"
	assert len(claim.signatures) == 1


def test_publish_json_emits_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	rc = author_cli_main(_publish_argv(tmp_path, _seed_b64()) + ["--json"])
	assert rc == 0
	out = json.loads(capsys.readouterr().out)
	assert Path(out["sidecar"]).is_file()


def test_publish_requires_exactly_one_key(tmp_path: Path) -> None:
	"""Both --key-file and --key-text together must fail; neither
	must also fail."""
	argv_both = _publish_argv(tmp_path, _seed_b64()) + ["--key-file", "/tmp/x.seed"]
	with pytest.raises(SystemExit):
		author_cli_main(argv_both)

	argv_neither = [a for a in _publish_argv(tmp_path, _seed_b64())
		if a != "--key-text" and a != _seed_b64()]
	with pytest.raises(SystemExit):
		author_cli_main(argv_neither)


def test_publish_refuses_overwrite_by_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	assert author_cli_main(_publish_argv(tmp_path, _seed_b64())) == 0
	# Second publish without --overwrite should exit non-zero with a
	# helpful message routed to stderr.
	rc = author_cli_main(_publish_argv(tmp_path, _seed_b64()))
	assert rc != 0
	captured = capsys.readouterr()
	assert "cosign" in captured.err or "overwrite" in captured.err


def test_publish_overwrite_replaces(tmp_path: Path) -> None:
	"""Per `--overwrite`, the CLI should let the user replace an
	existing sidecar (e.g. correcting a wrong-body publish before
	any consumers fetched it).  Replacement discards prior signatures."""
	assert author_cli_main(_publish_argv(tmp_path, _seed_b64())) == 0
	rc = author_cli_main(_publish_argv(tmp_path, _seed_b64()) + ["--overwrite"])
	assert rc == 0
	claim = load_author_claim_json(
		(tmp_path / author_claim_filename("demo.lib")).read_text(encoding="utf-8")
	)
	assert len(claim.signatures) == 1


def test_publish_requires_namespace(tmp_path: Path) -> None:
	argv = _publish_argv(tmp_path, _seed_b64())
	# Strip --namespace and its value.
	while "--namespace" in argv:
		i = argv.index("--namespace")
		del argv[i:i + 2]
	with pytest.raises(SystemExit):
		author_cli_main(argv)


def test_cosign_appends_signature(tmp_path: Path) -> None:
	seed_a_b64 = _seed_b64()
	seed_b_b64 = base64.b64encode(
		bytes((b ^ 0xFF) for b in bytes(range(32)))
	).decode("ascii")
	assert author_cli_main(_publish_argv(tmp_path, seed_a_b64)) == 0
	rc = author_cli_main([
		"cosign",
		"--sidecar-dir", str(tmp_path),
		"--package-id", "demo.lib",
		"--key-text", seed_b_b64,
	])
	assert rc == 0
	claim = load_author_claim_json(
		(tmp_path / author_claim_filename("demo.lib")).read_text(encoding="utf-8")
	)
	assert len(claim.signatures) == 2
	# Two distinct kids confirm two distinct signers.
	assert len({s.kid for s in claim.signatures}) == 2


def test_required_dep_must_be_name_equals_range(tmp_path: Path) -> None:
	argv = _publish_argv(tmp_path, _seed_b64())
	# Replace good dep spec with malformed one.
	i = argv.index("--required-dep")
	argv[i + 1] = "no-equals-sign"
	with pytest.raises(SystemExit):
		author_cli_main(argv)


# ── publish (manifest-aware default) ──────────────────────────────────────────


class TestPublishFromManifest:
	"""Pin the manifest-aware publish flow.

	Triggered by the cert-team finding: humans should not hand-enter
	SCI on the `drift-author publish` command line; the CLI should
	read `drift/manifest.json`, compute SCI via the shared helper
	(`compute_artifact_sci`), and sign — so the digest in the author
	claim is byte-identical to the digest `drift build`/`drift deploy`
	stamp into the `.dmp` manifest, satisfying the trust-v1 §3.5
	three-way equality.
	"""

	def _layout(self, tmp_path: Path, *, name: str = "myrepo") -> tuple[Path, Path]:
		"""Build `<repo>/drift/manifest.json` + sources + asset.

		Returns `(manifest_dir, project_root)`.
		"""
		project_root = tmp_path / name
		drift = project_root / "drift"
		drift.mkdir(parents=True)
		(project_root / "src").mkdir()
		(project_root / "src" / "lib.drift").write_text(
			f"module {name};\n", encoding="utf-8",
		)
		(project_root / "src" / "util.drift").write_text(
			f"module {name}.util;\n", encoding="utf-8",
		)
		(project_root / "assets").mkdir()
		(project_root / "assets" / "cfg.toml").write_text(
			"[a]\nb=1\n", encoding="utf-8",
		)
		manifest = {
			"schema_version": 2,
			"project": {"name": name, "license": "MIT"},
			"artifacts": [{
				"kind": "library",
				"name": name,
				"version": "0.1.0",
				"description": "demo",
				"entry_module": "src/lib.drift",
				"modules": ["src/lib.drift", "src/util.drift"],
				"assets": ["assets/cfg.toml"],
				"native_deps": [{"lib": "ssl"}],
				"package_deps": [{"name": "core.foo", "version": "0.3"}],
			}],
		}
		(drift / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
		return drift, project_root

	def _seed_file(self, tmp_path: Path) -> Path:
		seed = tmp_path / "k.seed"
		seed.write_text(_seed_b64() + "\n", encoding="utf-8")
		return seed

	def test_sci_matches_shared_helper(self, tmp_path: Path) -> None:
		"""**Core regression**: the SCI signed into the claim must
		equal exactly what `compute_artifact_sci` would compute.
		Drift between the two breaks trust-v1.md §3.5 equality at
		consumer-verify time.
		"""
		from tools.drift_deploy.manifest import load_manifest
		from tools.drift_deploy.build_cmd import compute_artifact_sci
		drift, _ = self._layout(tmp_path)
		seed = self._seed_file(tmp_path)
		rc = author_cli_main([
			"publish",
			"--manifest", str(drift / "manifest.json"),
			"--key-file", str(seed),
		])
		assert rc == 0
		written = drift / author_claim_filename("myrepo")
		assert written.is_file(), (
			f"sidecar must default to manifest dir; expected {written}"
		)
		claim = load_author_claim_json(written.read_text(encoding="utf-8"))
		art = load_manifest(drift / "manifest.json").artifacts[0]
		expected_sci = compute_artifact_sci(art, manifest_dir=drift)
		assert claim.body.source_content_id == expected_sci, (
			f"signed SCI {claim.body.source_content_id!r} != helper-computed "
			f"SCI {expected_sci!r}; the manifest-aware publish must use the "
			f"same canonical inputs `drift deploy` will use, or three-way "
			f"equality at verify time fails"
		)

	def test_derives_package_id_version_and_required_deps_from_manifest(
		self, tmp_path: Path,
	) -> None:
		"""Body fields are derived; the operator does not hand-enter them."""
		drift, _ = self._layout(tmp_path)
		seed = self._seed_file(tmp_path)
		rc = author_cli_main([
			"publish",
			"--manifest", str(drift / "manifest.json"),
			"--key-file", str(seed),
		])
		assert rc == 0
		claim = load_author_claim_json(
			(drift / author_claim_filename("myrepo")).read_text(encoding="utf-8")
		)
		assert claim.body.package_id == "myrepo"
		assert claim.body.version == "0.1.0"
		# Default namespace derivation: `<module_namespace>.*`.
		assert claim.body.namespaces == ("myrepo.*",)
		# package_deps from manifest map 1:1 onto required_deps.
		assert len(claim.body.required_deps) == 1
		assert claim.body.required_deps[0].name == "core.foo"
		assert claim.body.required_deps[0].version_range == "0.3"

	def test_release_utc_defaults_to_now(self, tmp_path: Path) -> None:
		"""The release timestamp is captured at publish time, not a
		synthetic constant.
		"""
		from datetime import datetime, timezone, timedelta
		drift, _ = self._layout(tmp_path)
		seed = self._seed_file(tmp_path)
		t_before = datetime.now(timezone.utc)
		rc = author_cli_main([
			"publish",
			"--manifest", str(drift / "manifest.json"),
			"--key-file", str(seed),
		])
		t_after = datetime.now(timezone.utc)
		assert rc == 0
		claim = load_author_claim_json(
			(drift / author_claim_filename("myrepo")).read_text(encoding="utf-8")
		)
		parsed = datetime.strptime(
			claim.body.release_utc, "%Y-%m-%dT%H:%M:%SZ",
		).replace(tzinfo=timezone.utc)
		assert t_before - timedelta(seconds=5) <= parsed <= t_after + timedelta(seconds=5)

	def test_namespace_override_repeatable(self, tmp_path: Path) -> None:
		"""Packages that own additional namespace patterns pass them
		via repeated --namespace (e.g. stdlib's std.*/lang.*/drift.*).
		"""
		drift, _ = self._layout(tmp_path)
		seed = self._seed_file(tmp_path)
		rc = author_cli_main([
			"publish",
			"--manifest", str(drift / "manifest.json"),
			"--key-file", str(seed),
			"--namespace", "myrepo.*",
			"--namespace", "myrepo.deep.*",
		])
		assert rc == 0
		claim = load_author_claim_json(
			(drift / author_claim_filename("myrepo")).read_text(encoding="utf-8")
		)
		assert tuple(sorted(claim.body.namespaces)) == (
			"myrepo.*", "myrepo.deep.*",
		)

	def test_refuses_overwrite_by_default(self, tmp_path: Path) -> None:
		drift, _ = self._layout(tmp_path)
		seed = self._seed_file(tmp_path)
		assert author_cli_main([
			"publish",
			"--manifest", str(drift / "manifest.json"),
			"--key-file", str(seed),
		]) == 0
		# Second run without --overwrite must refuse.
		assert author_cli_main([
			"publish",
			"--manifest", str(drift / "manifest.json"),
			"--key-file", str(seed),
		]) == 1
		# With --overwrite, succeeds.
		assert author_cli_main([
			"publish",
			"--manifest", str(drift / "manifest.json"),
			"--key-file", str(seed),
			"--overwrite",
		]) == 0

	def test_multi_library_manifest_requires_artifact(self, tmp_path: Path) -> None:
		"""When a manifest declares 2+ library artifacts, the operator
		must disambiguate via --artifact.
		"""
		drift, project_root = self._layout(tmp_path)
		# Inject a second library artifact into the manifest.
		mf = json.loads((drift / "manifest.json").read_text())
		second = dict(mf["artifacts"][0])
		second["name"] = "myrepo-extra"
		second["assets"] = []
		second["native_deps"] = []
		mf["artifacts"].append(second)
		(drift / "manifest.json").write_text(json.dumps(mf), encoding="utf-8")
		seed = self._seed_file(tmp_path)
		# No --artifact: must error.
		with pytest.raises(SystemExit, match="multiple library artifacts"):
			author_cli_main([
				"publish",
				"--manifest", str(drift / "manifest.json"),
				"--key-file", str(seed),
			])
		# With --artifact: success, sidecar is named after the picked artifact.
		assert author_cli_main([
			"publish",
			"--manifest", str(drift / "manifest.json"),
			"--key-file", str(seed),
			"--artifact", "myrepo-extra",
		]) == 0
		assert (drift / author_claim_filename("myrepo-extra")).is_file()

	def test_body_carries_no_target_class(self, tmp_path: Path) -> None:
		"""Spec-correction pin: the manifest-aware path must not emit
		`body.target_class` (the field was removed in 2026-05-20).
		"""
		drift, _ = self._layout(tmp_path)
		seed = self._seed_file(tmp_path)
		assert author_cli_main([
			"publish",
			"--manifest", str(drift / "manifest.json"),
			"--key-file", str(seed),
		]) == 0
		raw = json.loads((drift / author_claim_filename("myrepo")).read_text())
		assert "target_class" not in raw["body"]
