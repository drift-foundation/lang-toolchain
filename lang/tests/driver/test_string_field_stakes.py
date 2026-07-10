# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""B-arch-1c pins: MIR FIELD/VIEW producers feeding String value-position
stakes are materialized as ledger-visible `CopyValue` before string_arc.

Scope per the accepted checkpoint (premise-corrected): NOT surface field
syntax — user `self.field`/`obj.name` copies already materialize
upstream — but MIR producers whose dest is a borrowed String VALUE view:
`StructGetField` (String field_ty), `LoadRef` (String inner_ty),
`LoadField` (String-typed dest), bare storage operands, all resolved
FN-WIDE through AssignSSA. `VariantGetField` is NOT a view (its dest is
already owned — codegen retains at extraction; review-pinned). Address producers
(AddrOfField/AddrOfArrayElem) never qualify — an address is not a
String value — and remain itemized residuals.

The rank-1 corpus population is compiler-synthesized
`<Error>::Throw::throw_self` envelope builders (StructGetField reads of
error String fields into exc-ABI positions): pinned here via a declared
error thrown/caught with its String field intact. The
`StrictJsonCursor::field`-style shape is pinned as a field-read feeding
a ctor inside a loop. `arr[i].name` (the 0.33.58 alias class) gets ASAN
AND Valgrind rows.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import asan_active, sanitizer_timeout, valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]

# (1) throw_self shape: declared error with String fields, thrown and
# caught — the synthesized envelope builder reads the fields via
# StructGetField into exc-ABI value positions.
_THROW_SELF_SOURCE = """\
module main;

pub error DiskError {
	device: String,
	op: String,
}

fn boom(dev: String) throws -> Int {
	throw DiskError(device = dev, op = "read" + "");
}

pub fn main() nothrow -> Int {
	val d = "sda1";
	val r = try boom(d) catch { 7 };
	if r == 7 {
		if d == "sda1" { return 0; }
		return 2;
	}
	return 1;
}
"""

# (2) cursor-like: struct String-field read feeding a ctor inside a
# loop (repeated view → stake per iteration).
_CURSOR_LOOP_SOURCE = """\
module main;

struct Row { key: String }
struct Out { tag: String }

fn collect(rows: &Array<Row>) nothrow -> Int {
	var n = 0;
	var i = 0;
	while i < rows.len {
		val o = Out(tag = rows[i].key);
		if o.tag == "k" { n = n + 1; }
		i = i + 1;
	}
	return n;
}

pub fn main() nothrow -> Int {
	var rows: Array<Row> = [];
	rows.push(Row(key = "k"));
	rows.push(Row(key = "k"));
	rows.push(Row(key = "x"));
	if collect(&rows) == 2 {
		if rows[0].key == "k" { return 0; }
		return 2;
	}
	return 1;
}
"""

# (3) ref-path field read: &self method whose field access lowers via
# AddrOfField+LoadRef, feeding a value position.
_REF_PATH_SOURCE = """\
module main;

struct Person { name: String }
struct Tag { label: String }

implement Person {
	fn tag(self: &Person) nothrow -> Tag {
		return Tag(label = self.name);
	}
}

pub fn main() nothrow -> Int {
	val p = Person(name = "bob");
	val t = p.tag();
	val u = p.tag();
	if t.label == "bob" {
		if u.label == "bob" {
			if p.name == "bob" { return 0; }
			return 3;
		}
		return 2;
	}
	return 1;
}
"""

# (4) Cross-block note: no v1 SURFACE syntax places a cross-block SSA
# operand at a value position — match/try results must bind through
# locals (inline match-as-ctor-kwarg is rejected by the parser), and a
# bound local resolves via the LoadLocal rule. Fn-wide resolution is
# exercised by the corpus (synthesized builders) and gated by the audit
# counters; this pin keeps the bind-first surface shape green.
_CROSS_BLOCK_SOURCE = """\
module main;

struct Person { name: String }
struct Tag { label: String }

pub fn main() nothrow -> Int {
	val p = Person(name = "ann");
	val q = Person(name = "zoe");
	val c = true;
	val lbl = match c {
		true => { p.name },
		false => { q.name },
	};
	val t = Tag(label = lbl);
	if t.label == "ann" {
		if p.name == "ann" { return 0; }
		return 2;
	}
	return 1;
}
"""

# (5) arr[i].name into ctor — the 0.33.58 borrowed-element-view class.
_ARRAY_ELEM_FIELD_SOURCE = """\
module main;

struct Person { name: String }
struct Tag { label: String }

pub fn main() nothrow -> Int {
	var people: Array<Person> = [];
	people.push(Person(name = "ann"));
	people.push(Person(name = "bob"));
	val t = Tag(label = people[1].name);
	if t.label == "bob" {
		if people[1].name == "bob" { return 0; }
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


def test_throw_self_field_stakes(tmp_path: Path) -> None:
	_run_ok(tmp_path, _THROW_SELF_SOURCE)


def test_throw_self_field_stakes_asan(tmp_path: Path) -> None:
	"""ASAN row: error String fields copied into the envelope on the
	throw path; caller's local intact in the catch."""
	_run_ok_asan(tmp_path, _THROW_SELF_SOURCE)


def test_cursor_loop_field_stakes(tmp_path: Path) -> None:
	_run_ok(tmp_path, _CURSOR_LOOP_SOURCE)


def test_cursor_loop_field_stakes_asan(tmp_path: Path) -> None:
	_run_ok_asan(tmp_path, _CURSOR_LOOP_SOURCE)


def test_ref_path_field_stakes(tmp_path: Path) -> None:
	_run_ok(tmp_path, _REF_PATH_SOURCE)


def test_ref_path_field_stakes_asan(tmp_path: Path) -> None:
	_run_ok_asan(tmp_path, _REF_PATH_SOURCE)


def test_cross_block_operand_stakes(tmp_path: Path) -> None:
	_run_ok(tmp_path, _CROSS_BLOCK_SOURCE)


def test_array_elem_field_stakes(tmp_path: Path) -> None:
	_run_ok(tmp_path, _ARRAY_ELEM_FIELD_SOURCE)


def test_array_elem_field_stakes_asan(tmp_path: Path) -> None:
	"""ASAN row for the 0.33.58 class: element-view field copy, exactly
	one release for array/struct/copy stakes."""
	_run_ok_asan(tmp_path, _ARRAY_ELEM_FIELD_SOURCE)


@pytest.mark.skipif(shutil.which("valgrind") is None, reason="valgrind required")
@pytest.mark.skipif(asan_active(), reason="valgrind cannot run ASan-instrumented binaries")
def test_array_elem_field_stakes_valgrind(tmp_path: Path) -> None:
	"""Valgrind row (review-required): arr[i].name into a ctor — no
	leak, no UAF, definitely-lost 0."""
	res = _compile(tmp_path, _ARRAY_ELEM_FIELD_SOURCE)
	assert res.returncode == 0, res.stderr[-1200:]
	out = tmp_path / "test_bin"
	vg_log = tmp_path / "valgrind.log"
	vg = subprocess.run(
		valgrind_cmd("--leak-check=full",
			"--show-leak-kinds=definite,indirect",
			"--errors-for-leak-kinds=definite,indirect",
			"--error-exitcode=97",
			f"--log-file={vg_log}",
			str(out)),
		capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	vg_output = vg_log.read_text() if vg_log.exists() else ""
	lost = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	definitely_lost = int(lost.group(1).replace(",", "")) if lost else 0
	assert vg.returncode == 0, f"valgrind errors:\n{vg_output[-1200:]}"
	assert definitely_lost == 0, f"definitely lost: {definitely_lost} bytes"


def test_audit_field_stakes_materialized(tmp_path: Path) -> None:
	"""Acceptance pin: the throw_self compile shows ZERO
	value_position_retain in the synthesized envelope builder (the
	rank-1 residual population), with all hard gates at 0 and
	store_value_retain untouched by this slice's mechanism."""
	audit = tmp_path / "audit.jsonl"
	res = _compile(
		tmp_path, _THROW_SELF_SOURCE,
		env={
			"DRIFT_STRING_ARC_AUDIT": "1",
			"DRIFT_STRING_ARC_AUDIT_VERBOSE": "1",
			"DRIFT_STRING_ARC_AUDIT_FILE": str(audit),
		},
	)
	assert res.returncode == 0, res.stderr[-1200:]
	recs = [json.loads(line.split("] ", 1)[1]) for line in audit.read_text().splitlines()]
	builders = [
		r for r in recs
		if r.get("record") == "fn" and "DiskError" in r.get("fn", "") and "throw_self" in r.get("fn", "")
	]
	assert builders, "synthesized DiskError throw_self audit record expected"
	b = builders[0]
	assert b.get("site_class:value_position_retain", 0) == 0, b
	agg = [r for r in recs if r.get("record") == "aggregate"][0]
	assert agg.get("c1_must_drop_without_release", 0) == 0, agg
	assert agg.get("post_ledger_build_failed", 0) == 0, agg
	assert agg.get("unclassified", 0) == 0 and agg.get("untagged", 0) == 0, agg
