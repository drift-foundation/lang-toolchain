# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Codegen inline/alloca policy teeth (2026-07-25 review round).

1. STRUCTURAL inlinehint eligibility (IR-level; exact boundary teeth
   live in lang/tests/stage2/test_inline_hint_eligibility.py): hinted
   iff SMALL hot path (<= 48 MIR instructions outside
   Unreachable-terminated fail arms) AND an accessor SHAPE — a
   VARIANT return (Result/Optional accessors) or a cold-failure
   block.  Positives: a small accessor with a cold assert arm; a
   small Result-returning accessor.  Negatives: a mid-size hot
   function AND a small ORDINARY hot function (no shape) — this is
   deliberately not a blanket small-function inline policy (the
   rejected blanket hint grew binaries ~7%).

2. Entry-block scratch allocas: codegen's function bodies contain NO
   non-entry static allocas (LLVM marks such functions "never inline:
   dynamic alloca").  The scratch slots are registered by their OWNING
   lowering sites (transient, fully re-stored before each use, address
   never escapes the emitting sequence) — not by a global textual
   rewrite.

3. ADDRESS-TAKEN loop-local control (narrow claim: the pointer is
   taken and consumed WITHIN each iteration — Drift's borrow rules
   forbid a loop-local address escaping its iteration, so a true
   escape control is not constructible from source): per-iteration
   values through the taken address stay correct even though codegen
   may reuse one entry slot for the local across iterations.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]

POLICY_SRC = r"""module main;

import std.core as core;

// POSITIVE: accessor-sized hot path + a COLD assert arm (Unreachable
// block) — must be hinted.
pub fn small_accessor(x: Int) nothrow -> Int {
	assert(x >= 0, "negative input");
	return x + 1;
}

// NEGATIVE: mid-size straight-line HOT function (no cold arms; well
// over the 48-hot-instruction bound) — must NOT be hinted.
pub fn big_hot(x: Int) nothrow -> Int {
	var a = x + 1;
	a = a * 3 + 7;   a = a * 5 + 11;  a = a * 7 + 13;  a = a * 11 + 17;
	a = a * 13 + 19; a = a * 17 + 23; a = a * 19 + 29; a = a * 23 + 31;
	a = a * 29 + 37; a = a * 31 + 41; a = a * 37 + 43; a = a * 41 + 47;
	a = a * 43 + 53; a = a * 47 + 59; a = a * 53 + 61; a = a * 59 + 67;
	a = a * 61 + 71; a = a * 67 + 73; a = a * 71 + 79; a = a * 73 + 83;
	a = a * 79 + 89; a = a * 83 + 97; a = a * 89 + 101; a = a * 97 + 103;
	a = a * 101 + 107; a = a * 103 + 109; a = a * 107 + 113; a = a * 109 + 127;
	a = a * 113 + 131; a = a * 127 + 137; a = a * 131 + 139; a = a * 137 + 149;
	a = a * 139 + 151; a = a * 149 + 157; a = a * 151 + 163; a = a * 157 + 167;
	return a;
}

// NEGATIVE: small ORDINARY hot function — no variant return, no cold
// arm — must NOT be hinted despite being tiny.
pub fn small_plain(x: Int) nothrow -> Int {
	return x * 3 + 1;
}

// POSITIVE: small VARIANT-RETURNING accessor (Result-style shape;
// its "failure" arm returns, so it is not Unreachable-cold).
pub fn small_variant(x: Int) nothrow -> core.Result<Int, Int> {
	if x < 0 {
		return core.Result::Err(x);
	}
	return core.Result::Ok(x + 1);
}

pub fn main() nothrow -> Int {
	if small_accessor(1) != 2 { return 1; }
	if big_hot(1) == 0 { return 2; }
	if small_plain(2) != 7 { return 3; }
	match small_variant(2) {
		Ok(v) => { if v != 3 { return 4; } },
		Err(e) => { return 5; }
	}
	return 0;
}
"""

LOOP_LOCAL_SRC = r"""module main;

import std.core as core;
import std.mem as mem;
import std.console as cons;

fn read_through_ptr(p: mem.Ptr<Int>) nothrow -> Int {
	unsafe {
		return mem.ptr_read<type Int>(p);
	}
}

pub fn main() nothrow -> Int {
	// A loop-local whose ADDRESS is taken and used within each
	// iteration; the values must stay per-iteration correct even
	// though codegen may reuse one entry slot for the local.
	var total = 0;
	var i = 0;
	while i < 100 {
		var slot = i * 3 + 1;
		unsafe {
			val p = mem.ptr_from_ref<type Int>(slot);
			total = total + read_through_ptr(p);
		}
		i = i + 1;
	}
	// sum of (3i + 1) for i in 0..100 = 3*4950 + 100
	if total != 14950 { return 1; }
	cons.println("loop-local OK");
	return 0;
}
"""


def _compile(tmp_path: Path, src_text: str, name: str):
	src = tmp_path / f"{name}.drift"
	src.write_text(src_text)
	out_bin = tmp_path / f"{name}.bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev", "--allow-unsafe",
		 "--stdlib-root", str(stdlib), str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	assert res.returncode == 0, f"compile failed:\n{(res.stdout + res.stderr)[:2000]}"
	return out_bin


def test_inlinehint_structural_eligibility(tmp_path: Path) -> None:
	out_bin = _compile(tmp_path, POLICY_SRC, "policy")
	ir = Path(str(out_bin) + ".ll").read_text()
	small = re.search(r'define[^\n]*@small_accessor\([^)]*\)([^\n{]*)\{', ir)
	big = re.search(r'define[^\n]*@big_hot\([^)]*\)([^\n{]*)\{', ir)
	assert small and big, "probe functions not found in IR"
	assert "inlinehint" in small.group(1), (
		f"small accessor with cold assert arm must be hinted: {small.group(0)}"
	)
	assert "inlinehint" not in big.group(1), (
		f"mid-size hot function must NOT be hinted: {big.group(0)}"
	)
	plain = re.search(r'define[^\n]*@small_plain\([^)]*\)([^\n{]*)\{', ir)
	variant = re.search(r'define[^\n]*@small_variant\([^)]*\)([^\n{]*)\{', ir)
	assert plain and variant, "shape probe functions not found in IR"
	assert "inlinehint" not in plain.group(1), (
		f"ORDINARY small hot function must NOT be hinted (no accessor shape): {plain.group(0)}"
	)
	assert "inlinehint" in variant.group(1), (
		f"small variant-returning accessor must be hinted: {variant.group(0)}"
	)


def test_no_non_entry_static_allocas(tmp_path: Path) -> None:
	out_bin = _compile(tmp_path, POLICY_SRC, "allocas")
	ir = Path(str(out_bin) + ".ll").read_text()
	bad: list[str] = []
	for m in re.finditer(r'define[^\n]*\{(.*?)\n\}', ir, re.S):
		body = m.group(1).split("\n")
		labels_seen = 0
		for line in body:
			if line.endswith(":") and not line.startswith(" "):
				labels_seen += 1
			elif "= alloca " in line and "," not in line.split("= alloca ", 1)[1] and labels_seen > 1:
				bad.append(line.strip())
	assert not bad, f"non-entry static allocas found (never-inline hazard): {bad[:5]}"


def _scan_alloca_emissions(source_text: str):
	"""AST-based exhaustive scan: every string literal (plain or
	f-string) containing "= alloca", ANYWHERE in the module —
	independent of append/insert/extend/list-literal emission form.
	Returns [(enclosing_function_stack, lineno, is_docstring)]."""
	import ast as _ast
	tree = _ast.parse(source_text)
	found: list[tuple[tuple[str, ...], int, bool]] = []

	class _V(_ast.NodeVisitor):
		def __init__(self) -> None:
			self.stack: list[str] = []

		def _body_docstrings(self, node) -> set[int]:
			out = set()
			body = getattr(node, "body", [])
			if body and isinstance(body[0], _ast.Expr) and isinstance(body[0].value, _ast.Constant) \
					and isinstance(body[0].value.value, str):
				out.add(id(body[0].value))
			return out

		def visit_FunctionDef(self, node):
			self.stack.append(node.name)
			self._doc_ids = getattr(self, "_doc_ids", set()) | self._body_docstrings(node)
			self.generic_visit(node)
			self.stack.pop()

		visit_AsyncFunctionDef = visit_FunctionDef

		def visit_Constant(self, node):
			if isinstance(node.value, str) and "= alloca" in node.value:
				is_doc = id(node) in getattr(self, "_doc_ids", set())
				found.append((tuple(self.stack), node.lineno, is_doc))

		def visit_JoinedStr(self, node):
			text = "".join(
				part.value for part in node.values
				if isinstance(part, _ast.Constant) and isinstance(part.value, str)
			)
			if "= alloca" in text:
				found.append((tuple(self.stack), node.lineno, False))
			# do not descend: inner constants would double-count

	_V().visit(tree)
	return found


# Every alloca-emitting authority in llvm_codegen.py, with its
# entry-placement justification.  An occurrence whose enclosing
# function is not listed here FAILS the inventory.
_ALLOCA_AUTHORITIES: dict[str, str] = {
	# the scratch registries themselves
	"_scratch_alloca": "the _FuncBuilder entry registry",
	"scratch_alloca": "closure registries (clone helper, drop helper)",
	# entry-insertion-index authorities (lines.insert at the entry point)
	"_ensure_local_storage": "locals: inserted at _entry_alloca_insert_index",
	"_ensure_iface_tmp_alloca": "shared iface slot: inserted at _entry_alloca_insert_index",
	"_fresh_iface_alloca": "fresh iface slot: inserted at _entry_alloca_insert_index",
	"_ensure_dbg_keepalive_storage": "debug keepalive: inserted at _entry_alloca_insert_index",
	# entry-prologue list literals (alloca directly under the entry label)
	"emit_argv_entry_wrapper": "argv thunk prologue: alloca under __bb_entry in the define literal",
	"_ensure_interface_drop_helper": "iface drop helper prologue: entry-position literal",
}


def test_scratch_alloca_source_inventory() -> None:
	"""AST-BASED exhaustive inventory (review-mandated): every emitted
	LLVM string containing "= alloca" — regardless of emission form
	(append / insert / extend / list literal / f-string) — must belong
	to a classified ENTRY-PLACEMENT authority.  The generated-IR scan
	(test_no_non_entry_static_allocas) proves fixtures; this proves
	the SOURCE has no unclassified emission site at all."""
	src_text = (ROOT / "lang" / "codegen" / "llvm" / "llvm_codegen.py").read_text()
	unclassified = []
	for stack, lineno, is_doc in _scan_alloca_emissions(src_text):
		if is_doc:
			continue  # documentation text, not an emission
		if any(fn in _ALLOCA_AUTHORITIES for fn in stack):
			continue
		unclassified.append((lineno, "::".join(stack) or "<module>"))
	assert not unclassified, (
		"alloca-emitting strings outside the classified entry-placement "
		f"authorities: {unclassified} — route them through a scratch "
		"registry or add them (with justification) to _ALLOCA_AUTHORITIES"
	)


def test_alloca_scanner_catches_append_form() -> None:
	"""Negative tooth: a plain append-form emission is detected."""
	synthetic = (
		"class C:\n"
		"\tdef bad(self):\n"
		"\t\tself.lines.append(f\"  {x} = alloca i64\")\n"
	)
	hits = _scan_alloca_emissions(synthetic)
	assert [h for h in hits if h[0] == ("bad",)], hits


def test_alloca_scanner_catches_list_and_extend_forms() -> None:
	"""Negative tooth: list-literal and extend-form emissions are
	detected (the naive append-only grep this scan replaced missed
	them)."""
	synthetic = (
		"class C:\n"
		"\tdef bad_list(self):\n"
		"\t\tlines = [\"  %t = alloca i64\"]\n"
		"\tdef bad_extend(self):\n"
		"\t\tself.lines.extend([\"__bb:\", \"  %u = alloca ptr\"])\n"
	)
	hits = _scan_alloca_emissions(synthetic)
	stacks = {h[0] for h in hits}
	assert ("bad_list",) in stacks, hits
	assert ("bad_extend",) in stacks, hits


def test_address_taken_loop_local_control(tmp_path: Path) -> None:
	out_bin = _compile(tmp_path, LOOP_LOCAL_SRC, "looplocal")
	run = subprocess.run([str(out_bin)], capture_output=True, text=True,
		timeout=sanitizer_timeout(60))
	assert run.returncode == 0, f"exit={run.returncode}\n{run.stderr[:400]}"
	assert "loop-local OK" in run.stdout
