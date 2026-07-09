# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""B-arch-1a pins: by-value String call-argument copy stakes are
materialized as ledger-visible `CopyValue` MIR before string_arc
(`stage2/string_stakes.py`), replacing string_arc's late
`call_arg_retain`.

Behavior contract (refcount sequence byte-identical to the late
retain): caller keeps its String local usable after the call; callee
owns its +1; `move arg` still MOVES (no extra copy pair); ASAN rows
prove no leak / no double-drop on the direct and return-reaching
shapes. The audit pin proves the stake actually moved out of C2:
`site_class:call_arg_retain == 0` for the pinned fn while behavior
holds.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

# (1) Direct call; the local stays usable after two by-value passes.
_DIRECT_SOURCE = """\
module main;

fn shout(s: String) nothrow -> String {
	return s + "!";
}

pub fn main() nothrow -> Int {
	val name = "Ann";
	val a = shout(name);
	val b = shout(name);
	if a == "Ann!" {
		if b == "Ann!" {
			if name == "Ann" { return 0; }
			return 3;
		}
		return 2;
	}
	return 1;
}
"""

# (2) `move arg` still moves — the moved local must be unusable-free
# (no double release at exit) and the callee result correct.
_MOVE_ARG_SOURCE = """\
module main;

fn take(s: String) nothrow -> Int {
	if s == "gone" { return 0; }
	return 1;
}

pub fn main() nothrow -> Int {
	var s = "gone";
	return take(move s);
}
"""

# (3) Return-reaching call arg: the call result feeds the return while
# the local is released at scope exit.
_RETURN_REACHING_SOURCE = """\
module main;

fn wrap(s: String) nothrow -> String {
	return "[" + s + "]";
}

fn describe() nothrow -> String {
	val tag = "core";
	return wrap(tag);
}

pub fn main() nothrow -> Int {
	if describe() == "[core]" { return 0; }
	return 1;
}
"""

# (4) Indirect call through a callback with a by-value String param.
_INDIRECT_SOURCE = """\
module main;

import std.core as core;

pub fn main() nothrow -> Int {
	val f = core.callback1(|s: String| nothrow => s + "?");
	val q = "why";
	val a = f.call(q);
	if a == "why?" {
		if q == "why" { return 0; }
		return 2;
	}
	return 1;
}
"""

# (5) Interface call with a by-value String param.
_IFACE_SOURCE = """\
module main;

interface Sink {
	fn put(self: &Self, v: String) nothrow -> Int;
}

struct Counter { n: Int }

implement Sink for Counter {
	fn put(self: &Counter, v: String) nothrow -> Int {
		if v == "x" { return self.n + 1; }
		return self.n;
	}
}

fn feed(s: &Sink, v: String) nothrow -> Int {
	return s.put(v);
}

pub fn main() nothrow -> Int {
	val c = Counter(n = 41);
	val payload = "x";
	val r = feed(&c, payload);
	if r == 42 {
		if payload == "x" { return 0; }
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


def test_direct_call_arg_keeps_local_usable(tmp_path: Path) -> None:
	_run_ok(tmp_path, _DIRECT_SOURCE)


def test_direct_call_arg_asan(tmp_path: Path) -> None:
	"""ASAN row: two by-value passes + reuse — no leak, no double-drop."""
	_run_ok_asan(tmp_path, _DIRECT_SOURCE)


def test_move_arg_still_moves(tmp_path: Path) -> None:
	_run_ok(tmp_path, _MOVE_ARG_SOURCE)


def test_return_reaching_call_arg(tmp_path: Path) -> None:
	_run_ok(tmp_path, _RETURN_REACHING_SOURCE)


def test_return_reaching_call_arg_asan(tmp_path: Path) -> None:
	"""ASAN row: local copied into a return-reaching call — exactly one
	exit release for the local, callee/result stakes balanced."""
	_run_ok_asan(tmp_path, _RETURN_REACHING_SOURCE)


def test_indirect_call_arg(tmp_path: Path) -> None:
	_run_ok(tmp_path, _INDIRECT_SOURCE)


def test_iface_call_arg(tmp_path: Path) -> None:
	_run_ok(tmp_path, _IFACE_SOURCE)


def test_audit_shows_stake_materialized(tmp_path: Path) -> None:
	"""The acceptance pin: with the stake pass in the pipeline, the
	audited compile of the direct shape emits ZERO call_arg_retain
	events for main (the stake predates the ledger as CopyValue), with
	no leak candidates and no gate-counter regressions."""
	audit = tmp_path / "audit.jsonl"
	res = _compile(
		tmp_path, _DIRECT_SOURCE,
		env={
			"DRIFT_STRING_ARC_AUDIT": "1",
			"DRIFT_STRING_ARC_AUDIT_VERBOSE": "1",
			"DRIFT_STRING_ARC_AUDIT_FILE": str(audit),
		},
	)
	assert res.returncode == 0, res.stderr[-1200:]
	recs = [json.loads(line.split("] ", 1)[1]) for line in audit.read_text().splitlines()]
	agg = [r for r in recs if r.get("record") == "aggregate"]
	assert agg, "aggregate record expected"
	a = agg[0]
	assert a.get("site_class:call_arg_retain", 0) == 0, a
	assert a.get("c1_must_drop_without_release", 0) == 0, a
	assert a.get("post_ledger_build_failed", 0) == 0, a
	assert a.get("unclassified", 0) == 0 and a.get("untagged", 0) == 0, a


# Cross-package: the callee lives in a PACKAGE, so its by-value String
# param type id is package-loaded (remapped) rather than the canonical
# local String tid. The stake pass must match it via string_arc's
# semantic SCALAR/"String" predicate, not tid equality (review finding,
# B-arch-1a round 1).
_PKG_LIB_SOURCE = """\
module shoutlib;

export { shout };

pub fn shout(s: String) nothrow -> String {
	return s + "!";
}
"""

_PKG_APP_SOURCE = """\
module main;
import shoutlib;

pub fn main() nothrow -> Int {
	val name = "Ann";
	val a = shoutlib.shout(name);
	val b = shoutlib.shout(name);
	if a == "Ann!" {
		if b == "Ann!" {
			if name == "Ann" { return 0; }
			return 3;
		}
		return 2;
	}
	return 1;
}
"""


def test_pkg_boundary_call_arg_stake(tmp_path: Path) -> None:
	"""By-value String arg to a PACKAGE fn: behavior (caller keeps its
	local) + audit pin (zero call_arg_retain — the pkg param's String
	tid materializes through the semantic predicate)."""
	lib_dir = tmp_path / "lib"
	lib_dir.mkdir()
	lib_src = lib_dir / "shoutlib.drift"
	lib_src.write_text(_PKG_LIB_SOURCE)
	pkg_root = tmp_path / "pkgs"
	dmp_dir = pkg_root / "shoutlib" / "0.0.1"
	dmp_dir.mkdir(parents=True)
	emit = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 "-M", str(lib_dir), str(lib_src),
		 "--package-id", "shoutlib", "--package-version", "0.0.1",
		 "--package-target", "test-target",
		 "--emit-package", str(dmp_dir / "shoutlib.dmp")],
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
		 "--dep", "shoutlib@0.0.1",
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
	mains = [r for r in recs if r.get("record") == "fn" and r.get("fn", "").split("::")[-1] == "main"]
	assert mains, "main fn audit record expected"
	assert mains[0].get("site_class:call_arg_retain", 0) == 0, mains[0]
