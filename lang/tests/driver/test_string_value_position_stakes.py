# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""B-arch-1b pins: String copy stakes at VALUE POSITIONS (ctor fields,
variant/result payloads, exc-ABI strings, array literals/elements) are
materialized as ledger-visible `CopyValue` MIR before string_arc,
replacing string_arc's late `value_position_retain`.

Behavior contract unchanged from B-arch-1a: refcount sequences are
byte-identical (String CopyValue lowers to drift_string_retain); locals
stay usable after being copied into a value position; ASAN rows prove
no leak / no double-drop; the audit pin proves the stake moved out of
C2 and — for return-reaching composites — out of the C4 allowlist into
c1_agree (the ledger no longer models the local as moved because
CopyValue breaks the return-consumed-load chain).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

# (1) Ctor fields from live locals, return-reaching (the exact C4 shape
# from the B-arch-0 corpus: std.cli::parser's about/app/version).
_CTOR_SOURCE = """\
module main;

struct Cfg { app: String, version: String }

fn build(app: String, version: String) nothrow -> Cfg {
	return Cfg(app = app, version = version);
}

pub fn main() nothrow -> Int {
	val a = "tool";
	val v = "1.0";
	val c = build(a, v);
	if c.app == "tool" {
		if c.version == "1.0" {
			if a == "tool" { return 0; }
			return 3;
		}
		return 2;
	}
	return 1;
}
"""

# (2) Variant payload from a live local.
_VARIANT_SOURCE = """\
module main;

pub fn main() nothrow -> Int {
	val name = "Ann";
	val opt: Optional<String> = Some(name);
	val code = match opt {
		Some(v) => {
			match v == "Ann" {
				true => { 0 },
				false => { 2 },
			}
		},
		None => { 1 },
	};
	if name == "Ann" { return code; }
	return 3;
}
"""

# (3) Result-Ok payload: a throws fn returning a String wraps the value
# in FnResult.Ok — a value position. `cached` stays live past the
# return (used in the else-arm shape below), so the Ok payload is a
# copy stake.
_RESULT_OK_SOURCE = """\
module main;

pub error FetchError {
	what: String,
}

fn fetch(flag: Bool) throws -> String {
	val cached = "hit";
	if flag { return cached; }
	throw FetchError(what = cached);
}

pub fn main() nothrow -> Int {
	val got = try fetch(true) catch { "fallback" };
	if got == "hit" { return 0; }
	return 1;
}
"""

# (4) Exception params: a String local flows into a declared error's
# String field — the error-envelope params JSON (exc-ABI value
# positions) carries it; the local stays usable after the catch.
_EXC_PARAMS_SOURCE = """\
module main;

pub error DiskError {
	what: String,
}

fn boom(what: String) throws -> Int {
	throw DiskError(what = what);
}

pub fn main() nothrow -> Int {
	val subject = "disk";
	val r = try boom(subject) catch { 0 };
	if r == 0 {
		if subject == "disk" { return 0; }
		return 2;
	}
	return 1;
}
"""

# (5) Array literal + element assign from live locals.
_ARRAY_SOURCE = """\
module main;

pub fn main() nothrow -> Int {
	val a = "x";
	val b = "y";
	var arr = [a, b];
	arr[0] = b;
	if arr[0] == "y" {
		if arr[1] == "y" {
			if a == "x" { return 0; }
			return 3;
		}
		return 2;
	}
	return 1;
}
"""


def _compile(tmp_path: Path, source: str, *extra: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / "test_bin"
	run_env = {**os.environ, **(env or {})}
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 *extra, str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
		env=run_env,
	)


def _run_ok(tmp_path: Path, source: str, *extra: str) -> None:
	res = _compile(tmp_path, source, *extra)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1500:]}"
	out = tmp_path / "test_bin"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert run.returncode == 0, f"expected exit 0, got {run.returncode}; stderr:\n{run.stderr[-800:]}"


def _run_ok_asan(tmp_path: Path, source: str) -> None:
	res = _compile(tmp_path, source, "--sanitize=address,undefined")
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1500:]}"
	out = tmp_path / "test_bin"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(30))
	assert run.returncode == 0, f"expected exit 0, got {run.returncode}; stderr:\n{run.stderr[-800:]}"
	assert "ERROR: AddressSanitizer" not in run.stderr, run.stderr[-800:]


def test_ctor_fields_keep_locals_usable(tmp_path: Path) -> None:
	_run_ok(tmp_path, _CTOR_SOURCE)


def test_ctor_fields_asan(tmp_path: Path) -> None:
	"""ASAN row: two locals copied into a return-reaching ctor — one
	exit release each, struct fields own their +1s."""
	_run_ok_asan(tmp_path, _CTOR_SOURCE)


def test_variant_payload(tmp_path: Path) -> None:
	_run_ok(tmp_path, _VARIANT_SOURCE)


def test_result_ok_payload(tmp_path: Path) -> None:
	_run_ok(tmp_path, _RESULT_OK_SOURCE)


def test_exc_params_string(tmp_path: Path) -> None:
	_run_ok(tmp_path, _EXC_PARAMS_SOURCE)


def test_exc_params_string_asan(tmp_path: Path) -> None:
	"""ASAN row: local copied into throw params-json (runtime takes
	ownership per exc ABI) — no leak on the throw path, local usable in
	the catch."""
	_run_ok_asan(tmp_path, _EXC_PARAMS_SOURCE)


def test_array_literal_and_elem_assign(tmp_path: Path) -> None:
	_run_ok(tmp_path, _ARRAY_SOURCE)


def test_array_literal_and_elem_assign_asan(tmp_path: Path) -> None:
	_run_ok_asan(tmp_path, _ARRAY_SOURCE)


def test_audit_value_position_stakes_materialized(tmp_path: Path) -> None:
	"""Acceptance pin: the ctor shape compiles with ZERO
	value_position_retain events, the former C4 entries for the
	return-reaching ctor convert to c1 agreement, and no gate counter
	regresses."""
	audit = tmp_path / "audit.jsonl"
	res = _compile(
		tmp_path, _CTOR_SOURCE,
		env={
			"DRIFT_STRING_ARC_AUDIT": "1",
			"DRIFT_STRING_ARC_AUDIT_VERBOSE": "1",
			"DRIFT_STRING_ARC_AUDIT_FILE": str(audit),
		},
	)
	assert res.returncode == 0, res.stderr[-1200:]
	recs = [json.loads(line.split("] ", 1)[1]) for line in audit.read_text().splitlines()]
	builds = [r for r in recs if r.get("record") == "fn" and r.get("fn", "").split("::")[-1] == "build"]
	assert builds, "build fn audit record expected"
	b = builds[0]
	assert b.get("site_class:value_position_retain", 0) == 0, b
	assert b.get("c4_allowlisted", 0) == 0, b
	assert b.get("c1_agree", 0) >= 2, b
	agg = [r for r in recs if r.get("record") == "aggregate"][0]
	assert agg.get("c1_must_drop_without_release", 0) == 0, agg
	assert agg.get("post_ledger_build_failed", 0) == 0, agg
	assert agg.get("unclassified", 0) == 0 and agg.get("untagged", 0) == 0, agg


# Package boundary: the constructed struct TYPE is package-loaded, and
# the pkg fn body (compiled from pkg HIR with remapped tids) has its own
# ctor value positions. The pass is boundary-proof by construction — the
# candidate test reads only the operand's LOCAL type via the semantic
# String predicate, never the composite's type id — pinned here anyway.
_PKG_LIB_SOURCE = """\
module cfglib;

export { Cfg, mk };

pub struct Cfg { pub app: String }

pub fn mk(app: String) nothrow -> Cfg {
	return Cfg(app = app);
}
"""

_PKG_APP_SOURCE = """\
module main;
import cfglib;

pub fn main() nothrow -> Int {
	val name = "tool";
	val c1 = cfglib.mk(name);
	val c2 = cfglib.Cfg(app = name);
	if c1.app == "tool" {
		if c2.app == "tool" {
			if name == "tool" { return 0; }
			return 3;
		}
		return 2;
	}
	return 1;
}
"""


def test_pkg_boundary_ctor_value_position(tmp_path: Path) -> None:
	lib_dir = tmp_path / "lib"
	lib_dir.mkdir()
	lib_src = lib_dir / "cfglib.drift"
	lib_src.write_text(_PKG_LIB_SOURCE)
	pkg_root = tmp_path / "pkgs"
	dmp_dir = pkg_root / "cfglib" / "0.0.1"
	dmp_dir.mkdir(parents=True)
	emit = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 "-M", str(lib_dir), str(lib_src),
		 "--package-id", "cfglib", "--package-version", "0.0.1",
		 "--package-target", "test-target",
		 "--emit-package", str(dmp_dir / "cfglib.dmp")],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	assert emit.returncode == 0, f"pkg emit failed:\n{emit.stderr[-1200:]}"
	src = tmp_path / "main.drift"
	src.write_text(_PKG_APP_SOURCE)
	out = tmp_path / "test_bin"
	audit = tmp_path / "audit.jsonl"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 "--package-root", str(pkg_root),
		 "--dep", "cfglib@0.0.1",
		 "--allow-unsigned-from", str(pkg_root),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
		env={**os.environ,
			"DRIFT_STRING_ARC_AUDIT": "1",
			"DRIFT_STRING_ARC_AUDIT_VERBOSE": "1",
			"DRIFT_STRING_ARC_AUDIT_FILE": str(audit)},
	)
	assert res.returncode == 0, f"consumer compile failed:\n{res.stderr[-1500:]}"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert run.returncode == 0, f"expected exit 0, got {run.returncode}"
	recs = [json.loads(line.split("] ", 1)[1]) for line in audit.read_text().splitlines()]
	for fn_tail in ("main", "mk"):
		fns = [r for r in recs if r.get("record") == "fn" and r.get("fn", "").split("::")[-1] == fn_tail]
		assert fns, f"{fn_tail} audit record expected"
		assert fns[0].get("site_class:value_position_retain", 0) == 0, fns[0]
		assert fns[0].get("site_class:call_arg_retain", 0) == 0, fns[0]
