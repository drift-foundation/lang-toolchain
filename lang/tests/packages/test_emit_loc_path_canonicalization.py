# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression: emitted package debug-location source paths are project-relative,
so a package's `.dmp` (and thus `artifact_sha256`) is byte-identical regardless
of the absolute checkout/build path.

Background: the serialized HIR embeds Span/Located `loc.file`.  Pre-fix those
were ABSOLUTE paths, so building the same source at `/home/x/...` vs `/build/...`
produced different `.dmp` bytes — churning `artifact_sha256` while
`source_content_id` stayed identical, which forced downstream cert-claim
re-anchoring on every toolchain rebuild even when the source was unchanged.

Fix: at the package-emission boundary, Span/Located `file` is rewritten relative
to `--package-source-root` (project root); with no root it falls back to the
basename, so an absolute build path never reaches the payload.  In-memory Spans
keep absolute paths — local diagnostics are unaffected.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

from lang.driftc.core.span import Span
from lang.driftc.packages.provisional_dmir_v0 import (
	_normalize_emitted_loc_file,
	_to_jsonable,
	decode_span,
	emit_source_root,
)


# ── unit: the normalization helper ──────────────────────────────────

def test_absolute_under_root_becomes_relative_posix():
	with emit_source_root("/proj/root"):
		assert _normalize_emitted_loc_file("/proj/root/src/handlers/h.drift") == "src/handlers/h.drift"


def test_absolute_outside_root_falls_back_to_basename():
	with emit_source_root("/proj/root"):
		# A `..`-escaping path would re-introduce env-specific parent names.
		assert _normalize_emitted_loc_file("/other/place/x.drift") == "x.drift"


def test_relative_path_passes_through_unchanged():
	with emit_source_root("/proj/root"):
		assert _normalize_emitted_loc_file("src/lib.drift") == "src/lib.drift"


def test_no_root_context_strips_to_basename():
	# Outside any emit_source_root block, an absolute path must not leak.
	assert _normalize_emitted_loc_file("/abs/anywhere/lib.drift") == "lib.drift"


def test_non_string_passthrough():
	with emit_source_root("/proj/root"):
		assert _normalize_emitted_loc_file(None) is None


# ── unit: serializer reproducibility across roots ───────────────────

def _span_under(root: str) -> Span:
	return Span(file=f"{root}/src/lib.drift", file_id=7, line=2, column=5,
		end_line=2, end_column=9)

def test_to_jsonable_span_identical_across_roots():
	"""Same relative layout under two different absolute roots serializes to
	identical bytes — the core of cross-machine `.dmp` reproducibility."""
	with emit_source_root("/home/alice/proj"):
		a = _to_jsonable(_span_under("/home/alice/proj"))
	with emit_source_root("/build/ci/proj"):
		b = _to_jsonable(_span_under("/build/ci/proj"))
	assert a == b
	assert a["file"] == "src/lib.drift"  # relative, meaningful


def test_to_jsonable_span_absolute_when_unscoped_is_basename():
	# Without a root scope the reflective serializer still must not leak abs paths.
	s = Span(file="/abs/deep/lib.drift", line=1, column=1)
	out = _to_jsonable(s)
	assert out["file"] == "lib.drift"


# ── diagnostic sanity: the path survives round-trip and stays meaningful ──

def test_round_trip_preserves_relative_diagnostic_path():
	with emit_source_root("/proj/root"):
		obj = _to_jsonable(Span(file="/proj/root/src/dao/store.drift", line=10, column=3))
	# decode_span reads the JSON-ish encoding back into a Span.
	restored = decode_span(obj)
	assert restored is not None
	assert restored.file == "src/dao/store.drift"  # still a meaningful module path
	assert restored.line == 10


# ── end-to-end: identical source under two roots → byte-identical .dmp ──

def _emit_pkg(out: Path, root: Path) -> int:
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 "--emit-package", str(out),
		 "--package-id", "repro.lib",
		 "--package-version", "0.1.0",
		 "--package-target", "drift-dev",
		 "--source-content-id", "sha256:" + ("0" * 64),
		 "--package-source-root", str(root),
		 str(root / "src" / "lib.drift"),
		 str(root / "src" / "handlers" / "h.drift")],
		capture_output=True, text=True,
	).returncode


def _make_tree(root: Path) -> None:
	(root / "src" / "handlers").mkdir(parents=True)
	(root / "src" / "lib.drift").write_text(
		"module repro.lib;\npub fn add(a: Int, b: Int) nothrow -> Int { return a + b; }\n",
		encoding="utf-8")
	(root / "src" / "handlers" / "h.drift").write_text(
		"module repro.lib.handlers.h;\npub fn tag() nothrow -> String { return \"h\"; }\n",
		encoding="utf-8")


def test_identical_source_two_roots_emits_byte_identical_dmp(tmp_path: Path):
	rootA = tmp_path / "home_alice_proj"
	rootB = tmp_path / "build_ci_somewhere_else_proj"
	_make_tree(rootA); _make_tree(rootB)
	outA = tmp_path / "a.dmp"; outB = tmp_path / "b.dmp"
	assert _emit_pkg(outA, rootA) == 0
	assert _emit_pkg(outB, rootB) == 0
	a = outA.read_bytes(); b = outB.read_bytes()
	assert a == b, "identical source under different absolute roots must emit byte-identical .dmp"
	# artifact_sha256 is the sha of the uncompressed .dmp — must match too.
	assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()
	# And no absolute path leaked into the payload.
	assert str(rootA).encode() not in a
	assert b"src/handlers/h.drift" in a  # project-relative path is what's stored
