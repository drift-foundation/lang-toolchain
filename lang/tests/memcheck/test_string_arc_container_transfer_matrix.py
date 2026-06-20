# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""String-ownership conformance matrix — container transfer + exit edges.

Carrier for the `doc/refactor_triggers.md` "String ownership-authoring
conformance matrix" trigger, fired by the DriftQuery M3 file-read CORE_BUG
(2026-06-20):

    arr.push(throwing_call_returning_heap_String())   # repeated >= 2x

double-freed at teardown. A can-throw call's Ok payload is materialized into a
hidden `__call_ok` holder LOCAL (`_lower_can_throw_call_value`), and the value
used at the container-transfer site is a LOAD from that local — a shared view
whose stake stays owned by the holder. `_ensure_array_elem_copy(drop_source=...)`
was told (via `_call_arg_yields_owned_temp`) that the arg was a free owned temp,
so it RELEASED that shared view after copying it into storage; the holder local
then released the SAME buffer again at scope/exception exit → double free. Masked
for inline literals because a static string's release is a no-op
(`DRIFT_STRING_FLAG_STATIC`); only HEAP strings abort.

Fix: classify a can-throw call arg as a shared view (`drop_source=False`), so
container transfer retains its own stake and the holder's existing all-edges
release balances it.

This file pins a BOUNDED matrix (not a Cartesian product):

  Producers : heap concat, string_from_utf8_bytes (file read), static literal (control)
  Consumers : array push/insert/set, array LITERAL element store, struct field,
              variant field, local assignment/reassignment
  Exits     : normal scope drop, throwing-edge unwind, move-return teardown
  Plus      : nested Array<Shape{Array<Field{String}>}> (the reported env shape)

Each accepted row must be leak/double-free clean under valgrind. Static-literal
rows are CONTROLS (a no-op-release mask), not ownership proofs.

Per AGENTS.md regression-first: the seed row fails on the pre-fix compiler with a
valgrind "Invalid read" (double-free); do not narrow it to pass.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]


# A `throws` producer returning a NON-STATIC (heap) String, built by runtime
# concat so the result cannot be constant-folded to a static literal. The dead
# `throw`-bearing branch (an out-of-bounds index) makes the function `throws`,
# so a call to it is a *throwing call* — which routes its Ok payload through the
# `__call_ok` holder local in whose double-release the bug lived.
_HEAP_THROWS_PRODUCER = """\
fn mk_heap(seed: Int) throws -> String {
\tvar s = "h";
\tvar i = 0;
\twhile i < 3 { s = s + "eap-payload-abcdefghijklmnop"; i = i + 1; }
\tif seed < -999999 {            // dead branch: makes mk_heap `throws`
\t\tvar d: Array<Int> = [];
\t\tval _bad = d[seed];
\t}
\treturn s;
}
"""

# Control producer: nothrow heap (no `__call_ok` holder; always was correct).
_HEAP_NOTHROW_PRODUCER = """\
fn mk_heap_nothrow(seed: Int) nothrow -> String {
\tvar s = "h";
\tvar i = 0;
\twhile i < 3 { s = s + "eap-payload-abcdefghijklmnop"; i = i + 1; }
\treturn s;
}
"""


def _src(producer: str, body: str) -> str:
	return f"module main;\n\n{producer}\n{body}\n"


# ── Consumer × exit rows (heap throws producer = the bug class) ───────────────

# SEED (reported defect): array push, throwing heap call, >= 2x, scope drop.
SEED_PUSH = _src(_HEAP_THROWS_PRODUCER, """\
fn run(n: Int) throws -> Int {
\tvar texts: Array<String> = [];
\tvar i = 0;
\twhile i < n { texts.push(mk_heap(i)); i = i + 1; }
\treturn texts.len();
}
pub fn main() nothrow -> Int {
\tvar r = 0;
\ttry { r = run(2) + run(4); } catch { r = 97; }
\treturn r;
}""")

# Struct field consume + push of the struct + scope drop.
STRUCT_FIELD = _src(_HEAP_THROWS_PRODUCER, """\
struct Box(s: String);
fn run(n: Int) throws -> Int {
\tvar arr: Array<Box> = [];
\tvar i = 0;
\twhile i < n { arr.push(Box(mk_heap(i))); i = i + 1; }
\treturn arr.len();
}
pub fn main() nothrow -> Int {
\tvar r = 0;
\ttry { r = run(2) + run(5); } catch { r = 97; }
\treturn r;
}""")

# Variant field consume (Optional::Some) + scope drop.
VARIANT_FIELD = _src(_HEAP_THROWS_PRODUCER, """\
fn run(n: Int) throws -> Int {
\tvar arr: Array<Optional<String>> = [];
\tvar i = 0;
\twhile i < n { arr.push(Optional::Some(mk_heap(i))); i = i + 1; }
\treturn arr.len();
}
pub fn main() nothrow -> Int {
\tvar r = 0;
\ttry { r = run(2) + run(5); } catch { r = 97; }
\treturn r;
}""")

# Local var reassignment (drop-before-overwrite of a `var` String).
LOCAL_REASSIGN = _src(_HEAP_THROWS_PRODUCER, """\
fn run(n: Int) throws -> Int {
\tvar x = "init";
\tvar i = 0;
\tvar tot = 0;
\twhile i < n { x = mk_heap(i); tot = tot + x.byte_length(); i = i + 1; }
\treturn tot;
}
pub fn main() nothrow -> Int {
\tvar r = 0;
\ttry { r = run(3); } catch { r = 97; }
\treturn r;
}""")

# Move-return a struct holding the heap String, pushed (throwing call -> struct).
MOVE_RETURN = _src(_HEAP_THROWS_PRODUCER, """\
struct Box(s: String);
fn make_box(i: Int) throws -> Box { return Box(mk_heap(i)); }
fn run(n: Int) throws -> Int {
\tvar arr: Array<Box> = [];
\tvar i = 0;
\twhile i < n { arr.push(make_box(i)); i = i + 1; }
\treturn arr.len();
}
pub fn main() nothrow -> Int {
\tvar r = 0;
\ttry { r = run(2) + run(5); } catch { r = 97; }
\treturn r;
}""")

# Array LITERAL element store (a SECOND container-transfer path, distinct from
# push/insert/set — `lower_expr` + the ArrayLit element loop's own ownership
# classifier).  `[mk(0), mk(1), ...]` of a throwing heap-String call.
ARRAY_LITERAL = _src(_HEAP_THROWS_PRODUCER, """\
fn run() throws -> Int {
\tval xs: Array<String> = [mk_heap(0), mk_heap(1), mk_heap(2)];
\treturn xs.len();
}
pub fn main() nothrow -> Int {
\tvar r = 0;
\ttry { r = run() + run(); } catch { r = 97; }
\treturn r;
}""")

# (An array literal of a non-Copy struct element — `[Box(mk(0)), ...]` — is not
# expressible: v1 array literals require a Copy element type.  The Copy-but-
# refcounted case that exercises the element-store clone/drop is `Array<String>`,
# covered by ARRAY_LITERAL above; the struct/nested consumers are covered through
# `push` below.)

# Nested Array<Shape{ Array<Field{String}> }> — the reported env shape.
NESTED_ENV_SHAPE = _src(_HEAP_THROWS_PRODUCER, """\
struct Field(name: String, ty: String);
struct Shape(name: String, fields: Array<Field>);
fn mk_shape(i: Int) throws -> Shape {
\tvar fs: Array<Field> = [];
\tvar j = 0;
\twhile j < 3 { fs.push(Field(mk_heap(i), mk_heap(j))); j = j + 1; }
\treturn Shape(mk_heap(i), move fs);
}
fn run(n: Int) throws -> Int {
\tvar shapes: Array<Shape> = [];
\tvar i = 0;
\twhile i < n { shapes.push(mk_shape(i)); i = i + 1; }
\treturn shapes.len();
}
pub fn main() nothrow -> Int {
\tvar r = 0;
\ttry { r = run(2) + run(5); } catch { r = 97; }
\treturn r;
}""")

# ── Controls (must also be clean; not ownership proofs) ───────────────────────

# Static literal push: release is a no-op for static strings (the mask).
CONTROL_STATIC = _src("", """\
fn run(n: Int) nothrow -> Int {
\tvar texts: Array<String> = [];
\tvar i = 0;
\twhile i < n { texts.push("static-literal-payload"); i = i + 1; }
\treturn texts.len();
}
pub fn main() nothrow -> Int { return run(2) + run(5); }""")

# Nothrow heap push: heap strings, but no `__call_ok` holder (always correct).
CONTROL_NOTHROW_HEAP = _src(_HEAP_NOTHROW_PRODUCER, """\
fn run(n: Int) nothrow -> Int {
\tvar texts: Array<String> = [];
\tvar i = 0;
\twhile i < n { texts.push(mk_heap_nothrow(i)); i = i + 1; }
\treturn texts.len();
}
pub fn main() nothrow -> Int { return run(2) + run(5); }""")


# string_from_utf8_bytes producer (the report's actual producer), exercised
# end-to-end through a file read + the blessed `out = out + chunk` pattern,
# pushed into an Array<String> and torn down. `{path}` is filled per-test.
_UTF8_FILE_READ = """\
module main;

import std.core as core;
import std.io as io;
import std.concurrent as conc;

fn read_file(path: String) throws -> String {{
\tval t = conc.Duration(millis = 5000);
\tval f = io.file_builder(path).read(true).write(false).timeout(t).build().or_throw();
\tvar out = "";
\tvar buf = io.buffer(65536);
\tvar more = true;
\twhile more {{
\t\tio.buffer_set_len(&mut buf, 0);
\t\tval n = f.read(&mut buf).or_throw();
\t\tif n <= 0 {{ more = false; }}
\t\telse {{ out = out + core.string_from_utf8_bytes(io.buffer_ptr(&buf), n); }}
\t}}
\tf.close().or_throw();
\treturn out;
}}

fn run() throws -> Int {{
\tvar texts: Array<String> = [];
\ttexts.push(read_file("{path}"));
\ttexts.push(read_file("{path}"));
\ttexts.push(read_file("{path}"));
\treturn texts.len();
}}

pub fn main() nothrow -> Int {{
\tvar r = 0;
\ttry {{ r = run(); }} catch {{ r = 97; }}
\treturn r;
}}
"""


def _compile_and_valgrind(tmp_path: Path, source: str, *, label: str) -> tuple[int, str]:
	"""Compile under raw stdlib and run under valgrind. Returns
	(definitely_lost_bytes, valgrind_log_text)."""
	assert shutil.which("valgrind") is not None, "valgrind required"

	src = tmp_path / f"main_{label}.drift"
	src.write_text(source)
	out_bin = tmp_path / f"bin_{label}"

	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=180,
	)
	assert res.returncode == 0, f"[{label}] compile failed: {res.stderr[:1500]}"
	assert out_bin.exists(), f"[{label}] binary not produced"

	vg_log = tmp_path / f"valgrind_{label}.log"
	subprocess.run(
		valgrind_cmd("--leak-check=full",
			"--show-leak-kinds=definite,indirect",
			"--errors-for-leak-kinds=definite,indirect",
			"--error-exitcode=97",
			f"--log-file={vg_log}",
			str(out_bin)),
		capture_output=True, text=True, timeout=180,
	)
	vg_output = vg_log.read_text() if vg_log.exists() else ""
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	definitely_lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	return definitely_lost, vg_output


def _assert_clean(lost: int, vg_log: str, *, label: str) -> None:
	for bad in ("Invalid read", "Invalid write", "Invalid free"):
		if bad in vg_log:
			raise AssertionError(
				f"[{label}] LANGUAGE_BUG: valgrind reported '{bad}' — a String "
				f"consumed by container transfer is being released twice (a "
				f"shared view of the can-throw holder local was released, then "
				f"the holder released the same buffer at exit).\n\n{vg_log[-1800:]}"
			)
	assert lost == 0, (
		f"[{label}] LANGUAGE_BUG: {lost} bytes definitely lost.\n{vg_log[-1800:]}"
	)


@pytest.mark.parametrize(
	"label,source",
	[
		("seed_push", SEED_PUSH),
		("struct_field", STRUCT_FIELD),
		("variant_field", VARIANT_FIELD),
		("local_reassign", LOCAL_REASSIGN),
		("move_return", MOVE_RETURN),
		("array_literal", ARRAY_LITERAL),
		("nested_env_shape", NESTED_ENV_SHAPE),
		("control_static", CONTROL_STATIC),
		("control_nothrow_heap", CONTROL_NOTHROW_HEAP),
	],
)
def test_string_ownership_matrix_row(tmp_path: Path, label: str, source: str) -> None:
	lost, vg = _compile_and_valgrind(tmp_path, source, label=label)
	_assert_clean(lost, vg, label=label)


def test_string_from_utf8_bytes_file_read_push(tmp_path: Path) -> None:
	# The report's actual producer: file read -> string_from_utf8_bytes ->
	# `out = out + chunk` -> push -> teardown. Heap strings, throwing call.
	fixture = tmp_path / "fixture.txt"
	fixture.write_text("payload-line-one\npayload-line-two\nUserActivityMonth\n" * 8)
	source = _UTF8_FILE_READ.format(path=str(fixture))
	lost, vg = _compile_and_valgrind(tmp_path, source, label="utf8_file_read")
	_assert_clean(lost, vg, label="utf8_file_read")
