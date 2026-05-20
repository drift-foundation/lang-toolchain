# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Pin the producer→consumer round-trip of `FnSignature.declared_throws_event_fqns`.

Builds a tiny producer package with `pub fn f() throws E -> Int` and:
  1. Loads the emitted `.dmp` via `load_package_v1`, finds the signature
     entry for `f` in the module payload, and asserts the raw
     `declared_throws_event_fqns` field reflects the producer's
     `[<producer_pkg>:E]`.  Exercises producer-emit (Step C).
  2. Runs the centralized `decode_declared_throws_event_fqns` validator
     (same function called from both consumer-decode sites in
     `driftc.py`) on the raw field and asserts the typed list.  Hard-
     wires the test to the SAME validator the production decode path
     uses -- a future regression in the validator fails this test.
  3. Drives the malformed-input path against the SAME validator with a
     range of bad shapes (bare string, list-with-non-string, dict).

Plan reference: work/cross-pkg-narrow-throws-metadata/plan.md, §4c.
"""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from lang.driftc.packages.provider_v1 import load_package_v1
from lang.driftc.packages.provisional_dmir_v0 import (
	decode_declared_throws_event_fqns,
)

ROOT = Path(__file__).resolve().parents[3]


def _build_producer_pkg(tmp_path: Path) -> Path:
	"""Build (and sign) a one-module producer pkg containing
	`pub fn f() throws E`.  Returns the `.dmp` path."""
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from lang.drift.crypto import compute_ed25519_kid

	lib_dir = tmp_path / "producer_src"
	lib_dir.mkdir(exist_ok=True)
	(lib_dir / "producer.drift").write_text(
		"""\
module producer_pkg;
import std.core as core;
export { E, f };

pub error E { tag: String }

pub fn f() throws E -> Int { throw E(tag = "x"); }
"""
	)
	pkg_root_dir = tmp_path / "pkg_root" / "producer_pkg" / "0.1.0"
	pkg_root_dir.mkdir(parents=True, exist_ok=True)
	dmp = pkg_root_dir / "producer_pkg.dmp"
	# This test loads the .dmp via `load_package_v1` (the format-
	# only loader, no trust gate), so v1 sidecars / trust JSON are
	# not needed.  We still stamp `--source-content-id` so the
	# manifest is v1-shape -- defensive in case downstream tests
	# share this fixture and DO go through the trust gate.
	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--dev", "-M", str(lib_dir), "--stdlib-root", str(ROOT / "stdlib"),
		str(lib_dir / "producer.drift"),
		"--package-id", "producer_pkg",
		"--package-version", "0.1.0",
		"--package-target", "drift-dev",
		"--source-content-id", "sha256:" + ("0" * 64),
		"--emit-package", str(dmp),
		"--test-build-only",
	]
	res = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=60)
	assert res.returncode == 0, f"build of producer_pkg failed:\n{res.stderr[-1500:]}"
	return dmp


def _find_signature_for(loaded_pkg: object, fn_name: str) -> dict:
	"""Locate the raw payload signature dict whose `name` ends with
	`::<fn_name>` (signatures key on `name`-as-symbol, but we accept
	suffix match for ordinal-suffix forms)."""
	for _mid, mod in loaded_pkg.modules_by_id.items():
		payload_sigs = mod.payload.get("signatures") or {}
		for sym, sd in payload_sigs.items():
			if not isinstance(sd, dict):
				continue
			# Match on the encoded `name` field rather than the dict key
			# (the key is the fn symbol; signatures' `name` is the leaf).
			if sd.get("name") == fn_name:
				return sd
			# Fallback: dict-key suffix
			if isinstance(sym, str) and sym.endswith(f"::{fn_name}"):
				return sd
	raise AssertionError(f"no signature for {fn_name!r} in loaded package")


def test_emit_carries_narrow_throws_event_fqns_in_payload(tmp_path: Path) -> None:
	"""Step C (producer emit): load the `.dmp` and inspect the raw
	signature payload directly.  Asserts the field made it into the
	encoded bytes -- not via any consumer-side compilation."""
	dmp = _build_producer_pkg(tmp_path)
	loaded = load_package_v1(dmp)
	sd = _find_signature_for(loaded, "f")
	raw = sd.get("declared_throws_event_fqns")
	assert isinstance(raw, list), (
		f"expected `declared_throws_event_fqns` to be a list in the "
		f"emitted payload; got {type(raw).__name__}: {raw!r}.  "
		f"Step C (provisional_dmir_v0.py encode_signatures) regressed?"
	)
	assert raw == ["producer_pkg:E"], (
		f"expected canonical [\"producer_pkg:E\"]; got {raw!r}.  "
		f"FQN canonicalization in type_resolver.py:_resolve_declared_throws_types"
		f" regressed?"
	)


def test_decode_helper_round_trips_emitted_payload(tmp_path: Path) -> None:
	"""Step D/E (consumer decode): take the raw emitted payload and run
	it through the SAME validator the consumer-decode sites in driftc.py
	use.  A regression in the validator (e.g., a future refactor that
	loosens the shape check) fails this test."""
	dmp = _build_producer_pkg(tmp_path)
	loaded = load_package_v1(dmp)
	sd = _find_signature_for(loaded, "f")
	decoded = decode_declared_throws_event_fqns(
		sd.get("declared_throws_event_fqns"),
		signature_name=str(sd.get("name", "?")),
	)
	assert decoded == ["producer_pkg:E"], (
		f"decoder reshape regressed: expected [\"producer_pkg:E\"], "
		f"got {decoded!r}"
	)


def test_decode_helper_rejects_malformed_payloads() -> None:
	"""Drive the SAME `decode_declared_throws_event_fqns` helper that
	driftc.py:9349 and driftc.py:10022 call.  Bad shapes must raise
	ValueError; None and list[str] are the only accepted forms."""
	# Accepted forms.
	assert decode_declared_throws_event_fqns(None, signature_name="x") is None
	assert decode_declared_throws_event_fqns([], signature_name="x") == []
	assert decode_declared_throws_event_fqns(
		["m:E", "m:F"], signature_name="x",
	) == ["m:E", "m:F"]

	# Rejected forms.
	for bad in ("oops", ["m:E", 42], {"m:E": 1}, 7, b"bytes", ["m:E", None]):
		with pytest.raises(ValueError):
			decode_declared_throws_event_fqns(bad, signature_name="x")


def test_decode_helper_returns_a_copy() -> None:
	"""The helper must return a defensive copy: mutating the result
	must NOT mutate the input list (the producer's emit must not be
	observable to a future consumer-side mutation)."""
	raw = ["m:E"]
	decoded = decode_declared_throws_event_fqns(raw, signature_name="x")
	assert decoded == ["m:E"]
	assert decoded is not raw
	decoded.append("m:F")
	assert raw == ["m:E"]
