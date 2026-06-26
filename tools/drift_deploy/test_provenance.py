# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Provenance schema v4 tests.

v4 (clean break) makes `source_content_id` a REQUIRED field — provenance is
the third signed SCI leg (author == cert == provenance) — and holds it to the
same `validate_sci` shape check author/cert claims use.
"""
from __future__ import annotations

import json

import pytest

from tools.drift_deploy.provenance import CompilerInfo, build_provenance

_SCI = "sha256:" + ("a" * 64)
_ART = "sha256:" + ("d" * 64)
_COMPILER = CompilerInfo(version="0.33.56", abi=18, commit="abc123")


def _build(**ov) -> dict:
	kw = dict(
		artifact_name="uflowsd",
		artifact_version="0.2.0",
		artifact_kind="app",
		artifact_sha256=_ART,
		source_content_id=_SCI,
		target="drift-linux-x86_64",
		compiler=_COMPILER,
		resolved_deps={},
	)
	kw.update(ov)
	return json.loads(build_provenance(**kw))


def test_provenance_schema_version_is_4() -> None:
	assert _build()["schema_version"] == 4


def test_provenance_includes_sci_kind_sha() -> None:
	obj = _build()
	assert obj["source_content_id"] == _SCI
	assert obj["artifact_kind"] == "app"
	assert obj["artifact_sha256"] == _ART


def test_provenance_missing_sci_is_required_kwarg_typeerror() -> None:
	"""`source_content_id` is a required keyword-only arg, so OMITTING it is a
	Python TypeError (API contract) — distinct from the schema-validator
	ValueError path exercised by the None/malformed tests below."""
	with pytest.raises(TypeError):
		build_provenance(
			artifact_name="x", artifact_version="0.1.0", artifact_kind="package",
			artifact_sha256=_ART, target="t", compiler=_COMPILER, resolved_deps={},
		)  # no source_content_id


def test_provenance_rejects_none_sci() -> None:
	with pytest.raises(ValueError, match="source_content_id"):
		_build(source_content_id=None)


@pytest.mark.parametrize("bad_sci", [
	"sha256:nothex" + "g" * 58,        # non-hex
	"sha256:" + "a" * 63,              # wrong length
	"sha256:" + "A" * 64,              # uppercase
	"not-prefixed-" + "a" * 64,        # missing prefix
	"",
])
def test_provenance_rejects_malformed_sci(bad_sci: str) -> None:
	with pytest.raises(ValueError, match="source_content_id"):
		_build(source_content_id=bad_sci)


@pytest.mark.parametrize("bad_kind", ["library", "doc", "", "App"])
def test_provenance_rejects_bad_artifact_kind(bad_kind: str) -> None:
	"""`artifact_kind` is signed + cross-checked; only canonical package/app."""
	with pytest.raises(ValueError, match="artifact_kind"):
		_build(artifact_kind=bad_kind)


@pytest.mark.parametrize("bad_sha", [
	"sha256:nothex" + "g" * 58,
	"sha256:" + "a" * 63,
	"sha256:" + "A" * 64,
	"not-prefixed-" + "a" * 64,
	"",
])
def test_provenance_rejects_malformed_artifact_sha(bad_sha: str) -> None:
	"""`artifact_sha256` is signed + cross-checked; same shape as the claims."""
	with pytest.raises(ValueError, match="artifact_sha256"):
		_build(artifact_sha256=bad_sha)
