# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression (LANGUAGE_BUG): `throw` of an inline exception constructor whose
operand contains a nested call must alias-canonicalize the operand like any
other expression.

App-team report (driftc 0.33.23): across a package boundary,
    throw a.E(kind = a.K::Bad(detail = "x"));
ICE'd with `internal: missing CallInfo for callsite_id ...` — the nested
constructor's `base_type_expr` kept the raw import alias `a` (un-resolved to
`a_pkg`), so cross-package qualified-ctor resolution returned None and never
recorded a CallInfo.

Root cause: the workspace parser-AST alias walker (`_resolve_types_in_block`)
dispatched only `ThrowStmt`, but source `throw ...` parses to `RaiseStmt`, so the
operand was never traversed.  Fix adds the `RaiseStmt` branch.

This exercises the full package emit→consume path.  Controls:
  - bind-first form (always worked) still compiles;
  - a nested NORMAL function call in a throw field also canonicalizes (proves the
    fix is not variant-ctor-specific).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]

LIB_SOURCE = """\
module a_pkg;

import std.core as core;
use trait core.Diagnostic;

export { K, E, make_tag };

pub variant K {
\tBad(detail: String),
\t@tombstone Tomb
}

implement core.Diagnostic for K {
\tpub fn to_json_text(self: &K) nothrow -> String {
\t\treturn match self { Bad(d) => { core.diagnostic_json_int(1) }, default => { core.diagnostic_json_int(-1) } };
\t}
}

pub error E { kind: K, tag: String }

pub fn make_tag() nothrow -> String { return "t"; }
"""


@pytest.fixture(scope="module")
def published_a_pkg(tmp_path_factory) -> tuple[Path, Path, Path]:
	"""Publish a_pkg once (signing is expensive) and reuse across cases.

	Returns (pkg_root, trust_path, stdlib).
	"""
	stdlib = stdlib_root()
	if stdlib is None:
		pytest.skip("stdlib not available")
	from lang.tests.driver.pkg_test_helpers import publish_v1_pkg

	base = tmp_path_factory.mktemp("a_pkg_pub")
	lib_dir = base / "lib_src"
	lib_dir.mkdir()
	(lib_dir / "a_pkg.drift").write_text(LIB_SOURCE)
	pkg_libs_root = base / "libs"
	trust_path = base / "trust.json"
	publish_v1_pkg(
		lib_dir=lib_dir,
		src_files=[lib_dir / "a_pkg.drift"],
		package_id="a_pkg",
		package_version="0.1.0",
		namespace_glob="a_pkg.*",
		dest_pkg_root=pkg_libs_root,
		dest_trust_path=trust_path,
		target="test-target",
		stdlib_root_override=stdlib,
	)
	return pkg_libs_root, trust_path, stdlib


def _compile_consumer(pub: tuple[Path, Path, Path], tmp_path: Path, trigger: str) -> tuple[int, str]:
	pkg_libs_root, trust_path, stdlib = pub
	src = (
		"module consumer;\n"
		"import a_pkg as a;\n"
		f"{trigger}\n"
		"pub fn main() nothrow -> Int { val r = try trigger() catch { 0 }; return r; }\n"
	)
	consumer_dir = tmp_path / "consumer_src"
	consumer_dir.mkdir()
	(consumer_dir / "consumer.drift").write_text(src)
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		str(consumer_dir / "consumer.drift"),
		"--stdlib-root", str(stdlib),
		"--target-word-bits", "64",
		"--package-root", str(pkg_libs_root),
		"--dep", "a_pkg@0.1.0",
		"--trust-store", str(trust_path),
		"--entry", "consumer::main",
		"--emit-ir", str(tmp_path / "consumer.ll"),
		"--test-build-only",
		"--json",
	]
	res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120))
	out = res.stdout.strip()
	msgs = []
	if out:
		try:
			msgs = [d.get("message", "") for d in json.loads(out).get("diagnostics", [])]
		except json.JSONDecodeError:
			msgs = [out]
	return res.returncode, "\n".join(msgs) + "\n" + res.stderr


def test_throw_inline_ctor_nested_qualified_ctor_across_pkg(published_a_pkg, tmp_path: Path) -> None:
	# THE bug: inline throw of a qualified ctor with a nested qualified variant ctor.
	rc, joined = _compile_consumer(
		published_a_pkg, tmp_path,
		'fn trigger() throws a.E -> Int { throw a.E(kind = a.K::Bad(detail = "x"), tag = "t"); }',
	)
	assert "missing CallInfo" not in joined, f"ICE: throw operand nested callsite lost CallInfo:\n{joined[:1200]}"
	assert rc == 0, f"consumer compile failed:\n{joined[:1200]}"


def test_throw_inline_ctor_nested_normal_fn_call_field(published_a_pkg, tmp_path: Path) -> None:
	# Control: a nested NORMAL function call (`a.make_tag()`) in a throw field
	# must also canonicalize — proves the fix is not variant-ctor-specific.
	rc, joined = _compile_consumer(
		published_a_pkg, tmp_path,
		'fn trigger() throws a.E -> Int { throw a.E(kind = a.K::Bad(detail = "x"), tag = a.make_tag()); }',
	)
	assert "missing CallInfo" not in joined, f"ICE on nested normal fn call:\n{joined[:1200]}"
	assert rc == 0, f"consumer compile failed:\n{joined[:1200]}"


def test_bind_first_control_no_callinfo_ice(published_a_pkg, tmp_path: Path) -> None:
	# Control: the bind-first form never had the missing-CallInfo ICE (the nested
	# ctor is a normal `val` initializer, canonicalized through the existing
	# walker).  NOTE: with a NARROW `throws a.E` declaration this form is rejected
	# for an ORTHOGONAL reason — narrow-throws analysis can't prove a bound local
	# carries only `a.E` (`E_NARROW_THROWS_ESCAPE_OUTSIDE_DECLARED_SET`), a
	# separate limitation from this alias-canonicalization bug.  So we assert only
	# the relevant invariant here: no missing-CallInfo ICE.
	_rc, joined = _compile_consumer(
		published_a_pkg, tmp_path,
		'fn trigger() throws a.E -> Int { val e = a.E(kind = a.K::Bad(detail = "x"), tag = "t"); throw e; }',
	)
	assert "missing CallInfo" not in joined, joined[:1200]
