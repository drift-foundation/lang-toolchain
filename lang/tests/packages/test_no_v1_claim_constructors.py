# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Static regression: production code cannot emit a stale-version claim body.

The v2 clean break makes `AuthorClaimBody`/`CertClaimBody` require
`artifact_kind` (+ `artifact_path` for cert) at body `schema_version` 2.
The drift class this guards against:
  1. a literal `schema_version=1` in a claim constructor (the original miss), and
  2. a LOCAL `_BODY_SCHEMA_VERSION = 1` constant shadowing the canonical
     (the drift-author / cert-cli miss — invisible to a literal-1 search).

Defense: production emitters build bodies via the `make_*_claim_body`
factories, which stamp the canonical version internally.  This test asserts
the canonical is 2, that no production module OUTSIDE the two schema modules
defines its own body-schema constant, and that no non-test/non-schema code
passes `schema_version=` to a claim-body constructor at all.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_SCAN_DIRS = [_ROOT / "tools", _ROOT / "lang" / "driftc", _ROOT / "lang" / "drift"]

# The ONLY modules allowed to define a body-schema version / pass schema_version
# to a claim-body constructor.
_SCHEMA_MODULES = {
	_ROOT / "lang" / "driftc" / "packages" / "author_claim_v1.py",
	_ROOT / "lang" / "driftc" / "packages" / "cert_claim_v1.py",
}

_CTOR_RE = re.compile(r"\b(AuthorClaimBody|CertClaimBody)\s*\(")
_LOCAL_CONST_RE = re.compile(r"^\s*_BODY_SCHEMA_VERSION\s*=", re.MULTILINE)
_SCHEMA_KW_RE = re.compile(r"schema_version\s*=")


def _production_py_files() -> list[Path]:
	out: list[Path] = []
	for d in _SCAN_DIRS:
		for p in d.rglob("*.py"):
			parts = set(p.parts)
			if "tests" in parts or p.name.startswith("test_"):
				continue
			out.append(p)
	return out


def test_canonical_body_schema_version_is_2() -> None:
	from lang.driftc.packages import author_claim_v1, cert_claim_v1
	assert author_claim_v1.BODY_SCHEMA_VERSION == 2
	assert cert_claim_v1.BODY_SCHEMA_VERSION == 2


def test_no_local_body_schema_constants_outside_schema_modules() -> None:
	"""A local `_BODY_SCHEMA_VERSION` is exactly the shadowing bug that signed
	a v1 body even after the canonical bumped to 2."""
	offenders = [
		str(p.relative_to(_ROOT))
		for p in _production_py_files()
		if p not in _SCHEMA_MODULES and _LOCAL_CONST_RE.search(p.read_text(encoding="utf-8"))
	]
	assert not offenders, (
		"production module(s) define a local body-schema constant (use the "
		f"canonical from the schema module / the make_*_claim_body factory): {offenders}"
	)


def test_no_production_claim_ctor_passes_schema_version() -> None:
	"""Outside the schema modules, bodies must be built via the
	`make_*_claim_body` factories — never `*ClaimBody(schema_version=..., ...)`."""
	offenders: list[str] = []
	for path in _production_py_files():
		if path in _SCHEMA_MODULES:
			continue
		lines = path.read_text(encoding="utf-8").splitlines()
		for i, line in enumerate(lines):
			if not _CTOR_RE.search(line):
				continue
			window = "\n".join(lines[i : i + 16])
			if _SCHEMA_KW_RE.search(window):
				offenders.append(f"{path.relative_to(_ROOT)}:{i + 1}")
	assert not offenders, (
		"production claim-body constructor passes schema_version directly "
		f"(use make_author_claim_body / make_cert_claim_body): {offenders}"
	)
