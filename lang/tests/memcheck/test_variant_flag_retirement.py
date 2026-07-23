# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""zero-storage-safe drop-flag retirement (Arm B) — site-3 Variant
regression pins.  LANDED BEFORE the admission change per review
amendment 4; must be green on the pre-change tree (flags present,
cleanup already unguarded) AND the post-change tree (flags retired).

CAUSALITY CONTRACT (checkpoint §5.3): cleanup_authoring's existing
unguarded `MoveOut` transitions the REBUILT ledger's state to
MOVED_OUT before the Return, so the site-3 generic destructible
consultation adds the local to `skip_cleanup_locals` BEFORE
`_flag_managed_at_return` is formed — the flag skip's retirement is
statically subsumed by the ledger verdict.  The runtime zero-backing
proves the authored drop is SAFE on the unconsumed path; it is NOT
what causes the verdict.

Rows (both condition outcomes each, exactly-once destroy asserted by
exact stdout, then the whole binary re-run under valgrind):

- Row A — the standing `std.json::parse_located sp` shape: an
  `Optional<D>` local, None-initialized, `&mut`-filled by a callee,
  match-consumed on ONE path and left unconsumed on the other →
  PATH_DEPENDENT at the fn-exit hook.
- Row B — the §1.2a fixture-specific shape (`Array.pop` Optional):
  `val popped = arr.pop();` consumed on one path only.

Failure directions: a DOUBLE destroy (site-3 re-drop after the
authored cleanup — the subsumption claim wrong) shows as a duplicated
marker and/or Invalid read under valgrind; a MISSED drop on the
unconsumed path shows as a missing marker and definitely-lost bytes.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout, valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]

SOURCE = """\
module m;

import std.core as core;
import std.console as console;
import std.format as fmt;

struct D { name: String }

implement core.Destructible for D {
	pub fn destroy(var self: D) nothrow -> Void {
		console.print("destroy:");
		console.print(self.name);
		console.print("\\n");
		return;
	}
}

fn fill(slot: &mut Optional<D>, name: String) nothrow -> Void {
	*slot = Optional::Some(D(name = move name));
	return;
}

// Row A: the parse_located `sp` shape.
fn row_a(consume: Bool, name: String) nothrow -> Int {
	var sp: Optional<D> = Optional::None();
	fill(&mut sp, move name);
	if consume {
		match sp {
			Optional::Some(tree) => { return tree.name.byte_length(); },
			Optional::None() => { return 0 - 1; }
		}
	}
	return 0 - 2;
}

// Row B: the Array.pop Optional shape (§1.2a fixture-specific class).
fn row_b(consume: Bool, name: String) nothrow -> Int {
	var arr: Array<D> = [];
	arr.push(D(name = move name));
	val popped = arr.pop();
	if consume {
		match popped {
			Optional::Some(d) => { return d.name.byte_length(); },
			Optional::None() => { return 0 - 1; }
		}
	}
	return 0 - 2;
}

pub fn main() nothrow -> Int {
	var acc = 0;
	var i = 1;
	while i < 4 {
		val tag = fmt.format_int(i);
		acc = acc + row_a(true, "aT-" + tag);
		acc = acc + row_a(false, "aF-" + tag);
		acc = acc + row_b(true, "bT-" + tag);
		acc = acc + row_b(false, "bF-" + tag);
		i = i + 1;
	}
	if acc != 0 { return 0; }
	return 1;
}
"""


def _expected_stdout() -> str:
	lines = []
	for i in (1, 2, 3):
		for tag in (f"aT-{i}", f"aF-{i}", f"bT-{i}", f"bF-{i}"):
			lines.append(f"destroy:{tag}")
	return "\n".join(lines) + "\n"


def test_variant_flag_retirement_rows(tmp_path: Path) -> None:
	assert shutil.which("valgrind") is not None, "valgrind required"
	src = tmp_path / "main.drift"
	src.write_text(SOURCE)
	out_bin = tmp_path / "test_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "m::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(240),
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:1500]}"
	run = subprocess.run(
		[str(out_bin)], capture_output=True, text=True,
		timeout=sanitizer_timeout(10),
	)
	assert run.returncode == 0, (
		f"exit {run.returncode}; stderr:\n{run.stderr[-800:]}"
	)
	assert run.stdout == _expected_stdout(), (
		"destroy sequence regressed — exactly ONE destroy per call on "
		"BOTH outcomes (double = site-3 re-drop after authored cleanup; "
		f"missing = unconsumed-path drop lost):\n"
		f"expected: {_expected_stdout()!r}\nactual:   {run.stdout!r}"
	)
	vg_log = tmp_path / "valgrind.log"
	vg = subprocess.run(
		valgrind_cmd("--leak-check=full",
			"--show-leak-kinds=definite,indirect",
			"--errors-for-leak-kinds=definite,indirect",
			"--error-exitcode=97",
			f"--log-file={vg_log}",
			str(out_bin)),
		capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	vg_output = vg_log.read_text() if vg_log.exists() else ""
	assert vg.returncode == 0, (
		f"exit {vg.returncode} under valgrind.\n{vg_output[-1500:]}"
	)
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	assert lost == 0, f"{lost} bytes definitely lost.\n{vg_output[-1500:]}"
	for bad in ("Invalid read", "Invalid write", "Invalid free"):
		assert bad not in vg_output, f"{bad}:\n{vg_output[-1500:]}"
