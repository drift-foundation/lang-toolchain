# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Direct regression for HIGH #9: `_read_co_artifact_identity` must
reject sibling sidecars whose bodies do not bind to the
(package_id, version) we are looking up.

Without these checks a stale or corrupt sibling sidecar at
`staged_pkg_root/<dep>/<ver>/` could leak its own
(artifact_sha256, source_content_id) into the dependent's
`dep_graph`.  A downstream consumer might catch the mismatch at
verify time, but the deploy must refuse to sign a dependent cert
claim with a misbound sibling identity in the first place.

Exercised checks:
  - author claim body.package_id == dep_pkg_id
  - author claim body.version    == dep_version
  - cert claim body.package_id   == dep_pkg_id
  - cert claim body.version      == dep_version
  - author claim SCI == cert claim SCI
"""

from __future__ import annotations

import base64
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from lang.driftc.packages.author_claim_v1 import (
	AuthorClaimBody,
	RequiredDep,
)
from lang.driftc.packages.cert_claim_v1 import (
	CertClaimBody,
	CertSuite,
	DepGraphEntry,
	Toolchain,
)
from lang.driftc.packages.sidecar_naming import (
	author_claim_filename,
	cert_claim_filename,
)
from tools.drift_author.author_publish import (
	SignAuthorClaimOptions,
	sign_and_write_author_claim,
)
from tools.drift_deploy.cert_emit import (
	SignCertClaimOptions,
	sign_and_write_cert_claim,
)
from tools.drift_deploy.drift_deploy import _read_co_artifact_identity


# ── Fixture builders ──────────────────────────────────────────────


def _seed() -> bytes:
	return bytes(range(32))


def _make_sibling(
	dep_dir: Path,
	*,
	# Canonical filenames are computed from these IDs (i.e. these
	# control where on disk the sidecars LIVE).
	disk_pkg_id: str,
	disk_version: str,
	# Body-level identity (signed) -- can differ from the disk IDs
	# to simulate a misbound or stale sidecar that survived a
	# previous release without being cleaned up.
	author_pkg_id: str | None = None,
	author_version: str | None = None,
	cert_pkg_id: str | None = None,
	cert_version: str | None = None,
	author_sci: str = "sha256:" + ("a" * 64),
	cert_sci: str = "sha256:" + ("a" * 64),
) -> None:
	"""Build a (corruptable) sibling sidecar pair at `dep_dir`.

	The body identities default to the disk identities (well-formed
	case).  Override `author_*` / `cert_*` to simulate misbound
	bodies that still live at the canonical filename for
	`disk_pkg_id` -- this is the K HIGH #9 scenario (sidecar's body
	says one thing, the surrounding `staged_pkg_root` layout says
	another).
	"""
	author_pkg_id_body = author_pkg_id if author_pkg_id is not None else disk_pkg_id
	author_version_body = author_version if author_version is not None else disk_version
	cert_pkg_id_body = cert_pkg_id if cert_pkg_id is not None else disk_pkg_id
	cert_version_body = cert_version if cert_version is not None else disk_version

	dep_dir.mkdir(parents=True, exist_ok=True)
	author_body = AuthorClaimBody(
		schema_version=1,
		package_id=author_pkg_id_body,
		version=author_version_body,
		namespaces=(author_pkg_id_body,),
		source_content_id=author_sci,
		required_deps=(),
		release_utc="2026-05-19T00:00:00Z",
	)
	# Write under whatever filename the writer derives from the
	# (possibly mismatched) body, then rename onto the canonical
	# disk filename for `disk_pkg_id`.  This produces the exact
	# "right filename, wrong body" shape K's HIGH #9 names.
	written = sign_and_write_author_claim(SignAuthorClaimOptions(
		body=author_body, seed32=_seed(), sidecar_dir=dep_dir,
	))
	target_author = dep_dir / author_claim_filename(disk_pkg_id)
	if written != target_author:
		written.rename(target_author)

	cert_body = CertClaimBody(
		schema_version=1,
		package_id=cert_pkg_id_body,
		version=cert_version_body,
		artifact_sha256="sha256:" + ("c" * 64),
		source_content_id=cert_sci,
		target="linux-x86_64",
		toolchain=Toolchain(driftc_version="0.31.0", drift_rt_abi=1, driftc_commit=""),
		dep_graph=(),
		cert_suite=CertSuite(
			id="drift-deploy/v1", version="1.0", result="pass",
			result_evidence_sha256="sha256:" + ("f" * 64),
		),
		run_id="run-co-artifact-test",
		run_started_utc="2026-05-19T00:00:00Z",
		evidence_sha256="sha256:" + ("0" * 64),
	)
	cert_written = sign_and_write_cert_claim(SignCertClaimOptions(
		body=cert_body, seed32=_seed(), sidecar_dir=dep_dir,
	))
	# Cert claim filename includes both the body's package_id AND
	# the signer kid.  Rename onto the canonical `disk_pkg_id`
	# prefix while preserving the kid suffix the helper scans for.
	if cert_pkg_id is not None and cert_pkg_id != disk_pkg_id:
		from lang.driftc.packages.sidecar_naming import cert_claim_filename_prefix
		old_prefix = cert_claim_filename_prefix(cert_pkg_id_body)
		new_prefix = cert_claim_filename_prefix(disk_pkg_id)
		new_name = cert_written.name.replace(old_prefix, new_prefix, 1)
		cert_written.rename(dep_dir / new_name)


# ── Happy path ─────────────────────────────────────────────────────


def test_well_bound_sibling_returns_identity(tmp_path: Path) -> None:
	"""When the sibling sidecars bind to (dep_pkg_id, dep_version),
	the helper returns the cert body's artifact_sha256 + SCI plus
	both kids."""
	staged_pkg_root = tmp_path / "staged"
	dep_dir = staged_pkg_root / "shared" / "1.0.0"
	_make_sibling(dep_dir, disk_pkg_id="shared", disk_version="1.0.0")
	result = _read_co_artifact_identity(staged_pkg_root, "shared", "1.0.0")
	assert result is not None
	artifact_sha, sci, author_kid, cert_kid = result
	assert artifact_sha == "sha256:" + ("c" * 64)
	assert sci == "sha256:" + ("a" * 64)
	assert author_kid.startswith("ed25519:")
	assert cert_kid.startswith("ed25519:")


# ── Missing-sidecar paths ──────────────────────────────────────────


def test_missing_directory_returns_none(tmp_path: Path) -> None:
	assert _read_co_artifact_identity(tmp_path / "nope", "shared", "1.0.0") is None


def test_missing_author_claim_returns_none(tmp_path: Path) -> None:
	staged_pkg_root = tmp_path / "staged"
	dep_dir = staged_pkg_root / "shared" / "1.0.0"
	dep_dir.mkdir(parents=True)
	# Write only the cert claim; no author claim.
	cert_body = CertClaimBody(
		schema_version=1, package_id="shared", version="1.0.0",
		artifact_sha256="sha256:" + ("c" * 64),
		source_content_id="sha256:" + ("a" * 64),
		target="linux-x86_64",
		toolchain=Toolchain(driftc_version="0.31.0", drift_rt_abi=1, driftc_commit=""),
		dep_graph=(),
		cert_suite=CertSuite(
			id="drift-deploy/v1", version="1.0", result="pass",
			result_evidence_sha256="sha256:" + ("f" * 64),
		),
		run_id="x", run_started_utc="2026-05-19T00:00:00Z",
		evidence_sha256="sha256:" + ("0" * 64),
	)
	sign_and_write_cert_claim(SignCertClaimOptions(
		body=cert_body, seed32=_seed(), sidecar_dir=dep_dir,
	))
	assert _read_co_artifact_identity(staged_pkg_root, "shared", "1.0.0") is None


# ── Body-binding rejections (the K HIGH #9 contract) ───────────────


def test_author_claim_package_id_mismatch_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""The author claim FILENAME is the canonical one for
	`shared.author-claim` but the signed body's package_id is
	"other-package" (e.g. a stale sidecar left behind by a rename).
	Helper must reject and stderr-warn naming the mismatch."""
	staged_pkg_root = tmp_path / "staged"
	dep_dir = staged_pkg_root / "shared" / "1.0.0"
	_make_sibling(
		dep_dir,
		disk_pkg_id="shared", disk_version="1.0.0",
		author_pkg_id="other-package",  # body says other-package
	)
	assert _read_co_artifact_identity(staged_pkg_root, "shared", "1.0.0") is None
	err = capsys.readouterr().err
	assert "author claim" in err
	assert "body.package_id" in err
	assert "'other-package'" in err
	assert "expected 'shared'" in err


def test_author_claim_version_mismatch_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""A stale author claim from a previous release left in place
	when the sibling was bumped to 1.0.1 -- helper rejects."""
	staged_pkg_root = tmp_path / "staged"
	dep_dir = staged_pkg_root / "shared" / "1.0.1"
	_make_sibling(
		dep_dir,
		disk_pkg_id="shared", disk_version="1.0.1",
		author_version="1.0.0",  # stale prior-release body
	)
	assert _read_co_artifact_identity(staged_pkg_root, "shared", "1.0.1") is None
	err = capsys.readouterr().err
	assert "author claim" in err
	assert "body.version" in err
	assert "'1.0.0'" in err and "'1.0.1'" in err


def test_cert_claim_package_id_mismatch_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""The cert-claim filename lives under the `shared` prefix
	(canonical placement) but its signed body binds to "other".
	Exactly K's HIGH #9 scenario.  Helper must reject."""
	staged_pkg_root = tmp_path / "staged"
	dep_dir = staged_pkg_root / "shared" / "1.0.0"
	_make_sibling(
		dep_dir,
		disk_pkg_id="shared", disk_version="1.0.0",
		cert_pkg_id="other",  # body says other
	)
	assert _read_co_artifact_identity(staged_pkg_root, "shared", "1.0.0") is None
	err = capsys.readouterr().err
	assert "cert claim" in err
	assert "body.package_id" in err
	assert "'other'" in err
	assert "expected 'shared'" in err


def test_cert_claim_version_mismatch_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""K's second variant: stale cert claim with wrong version."""
	staged_pkg_root = tmp_path / "staged"
	dep_dir = staged_pkg_root / "shared" / "1.0.0"
	_make_sibling(
		dep_dir,
		disk_pkg_id="shared", disk_version="1.0.0",
		cert_version="0.9.0",  # stale prior release
	)
	assert _read_co_artifact_identity(staged_pkg_root, "shared", "1.0.0") is None
	err = capsys.readouterr().err
	assert "cert claim" in err
	assert "body.version" in err
	assert "'0.9.0'" in err and "'1.0.0'" in err


def test_sci_disagreement_between_sidecars_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Author claim and cert claim attest DIFFERENT SCIs for the
	same release.  One of the two sidecars is stale -- propagating
	either value would tie the dependent's dep_graph to a
	self-contradictory upstream attestation set."""
	staged_pkg_root = tmp_path / "staged"
	dep_dir = staged_pkg_root / "shared" / "1.0.0"
	_make_sibling(
		dep_dir,
		disk_pkg_id="shared", disk_version="1.0.0",
		author_sci="sha256:" + ("a" * 64),
		cert_sci="sha256:" + ("b" * 64),  # disagreement
	)
	assert _read_co_artifact_identity(staged_pkg_root, "shared", "1.0.0") is None
	err = capsys.readouterr().err
	assert "SCI mismatch" in err
	assert "author" in err and "cert" in err
