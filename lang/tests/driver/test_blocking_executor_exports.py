# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Export-surface regressions for the 0.33.80 blocking-FFI facility
(issues/blocking-executor-missing-from-concurrent-exports, fixed in
0.33.81).

The six facility declarations were `pub` from day one but absent from
std.concurrent's `export {}` list. Functions RESOLVED cross-module by
inference, masking the omission — but the TYPE `conc.BlockingExecutor`
was unnameable in user signatures/fields (E-AUTO-0fd5b919), blocking
the store-your-subsystem-executor integration shape the facility's own
boundary guidance prescribes (drift-query Slice 12's exact blocker).

Pinned here: the type is nameable in a struct FIELD, a function
PARAMETER, and a RETURN type (each was independently broken); the
pre-fix function-resolution shape still compiles AND RUNS; and a
genuinely private name stays rejected (the export gate itself still
works)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

# The store-your-executor integration shape, end to end: named executor
# held in a struct field, passed as &param, returned from a builder fn —
# and actually used through the stored handle.
_TYPE_NAMEABLE_SOURCE = """\
module main;

import std.core as core;
import std.concurrent as conc;
import std.console as console;

struct StorageAdapter {
	name: String,
	exec: conc.BlockingExecutor
}

fn make_adapter(name: String) -> conc.BlockingExecutor {
	var b = conc.blocking_executor_builder();
	b.min_threads(1);
	b.max_threads(1);
	return conc.build_blocking_executor(b.build(), move name);
}

fn run_op(exec: &conc.BlockingExecutor, label: String) nothrow -> Int {
	match conc.run_blocking_on(exec, move label, core.callback0(|| => { return 42; })) {
		core.Result::Ok(v) => { return v; },
		core.Result::Err(_) => { return -1; },
	}
}

pub fn main() nothrow -> Int {
	val adapter = StorageAdapter(
		name = "storage-lmdb",
		exec = make_adapter("storage-lmdb")
	);
	if run_op(adapter.exec, "lmdb.write_txn") == 42 {
		console.println("type-nameable-ok");
		return 0;
	}
	return 1;
}
"""

# The pre-fix WORKING shape (local val + inference only, type never
# named) — must keep compiling and running.
_INFERENCE_SHAPE_SOURCE = """\
module main;

import std.core as core;
import std.concurrent as conc;
import std.console as console;

pub fn main() nothrow -> Int {
	var b = conc.blocking_executor_builder();
	b.min_threads(1);
	b.max_threads(1);
	val ex = conc.build_blocking_executor(b.build(), "inference-demo");
	match conc.run_blocking_on(ex, "demo.op", core.callback0(|| => { return 7; })) {
		core.Result::Ok(v) => {
			match v == 7 { true => { console.println("inference-ok"); return 0; }, false => { return 2; } }
		},
		core.Result::Err(_) => { return 1; },
	}
}
"""

# Negative: a genuinely private name must stay rejected — the export
# gate itself still works after widening the list.  The private name is
# used in a TYPE position only (struct field), never constructed: a
# construction attempt could fail for the wrong reason (generic args,
# ctor fields) even if the type were accidentally exported, proving
# nothing about the gate.
_PRIVATE_NAME_SOURCE = """\
module main;

import std.concurrent as conc;

struct UsesPrivate {
	state: &conc.ResultState<Int>
}

pub fn main() nothrow -> Int {
	return 0;
}
"""


def _compile(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / "test_bin"
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(240), env=os.environ.copy(),
	)


def test_type_nameable_in_field_param_and_return(tmp_path: Path) -> None:
	res = _compile(tmp_path, _TYPE_NAMEABLE_SOURCE)
	assert res.returncode == 0, f"compile failed (the 0.33.80 regression):\n{res.stderr[-1800:]}"
	run = subprocess.run([str(tmp_path / "test_bin")], capture_output=True,
	                     text=True, timeout=sanitizer_timeout(20))
	assert run.returncode == 0, f"exit {run.returncode}; stderr:\n{run.stderr[-800:]}"
	assert "type-nameable-ok" in run.stdout


def test_inference_shape_still_works(tmp_path: Path) -> None:
	res = _compile(tmp_path, _INFERENCE_SHAPE_SOURCE)
	assert res.returncode == 0, res.stderr[-1500:]
	run = subprocess.run([str(tmp_path / "test_bin")], capture_output=True,
	                     text=True, timeout=sanitizer_timeout(20))
	assert run.returncode == 0, run.stderr[-800:]
	assert "inference-ok" in run.stdout


def test_private_names_still_rejected(tmp_path: Path) -> None:
	res = _compile(tmp_path, _PRIVATE_NAME_SOURCE)
	assert res.returncode != 0, "private name compiled — export gate broken"
	# The SPECIFIC export-gate diagnostic, naming the type — a failure
	# for any other reason (ctor shape, generics) must not satisfy this.
	assert "does not export type 'ResultState'" in res.stderr, res.stderr[-800:]
