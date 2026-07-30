# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""**LANGUAGE_BUG carrier (suspected)**: partial-move match arm with a
Copy-binder of a refcounted-scalar field leaks the slot's +1.

**The shape:**
    pub variant Pair {
        Pair(t: Token, s: String),
    }
    match pair_value {
        Pair(t, s) => { ... }
    }

`Token` is non-Copy and Destructible — its binder takes the MOVE branch
of the binder loop, sets `arm_scrut_payload_moved = True`, and adds the
field index to `moved_field_indices`.  The whole-scrutinee DropValue is
suppressed (it would re-drop the moved Token).

`String` is refcounted-scalar Copy — its binder takes the COPY branch
of the binder loop (`_should_copy_value(String) is True` after the
0.31.11 policy fix) and emits `CopyValue` to retain a +1 for the
binder.  The slot still holds its original +1.

In the per-field cleanup loop (`_visit_expr_HMatchExpr` site 2):
  - Token field: Filter A skips (in `moved_field_indices`).  Correct
    (binder consumed the +1).
  - String field: Filter A doesn't skip (Copy isn't in
    `moved_field_indices`); Filter B doesn't skip (`needs_drop=True`
    for String post-policy-fix).  → CANDIDATE; `__match_partial_drop_N`
    allocated; `MatchCleanupHook` carries it.

`match_cleanup_authoring` queries `field_verdict_at` for the String
field.  The per-field state walker (`_apply_field_state` in
`ownership_ledger.py`) over-reports `MovedOut` for any
`VariantGetFieldAddr` of a tracked named local — the Copy binder
emitted `VariantGetFieldAddr` to read the field address, so the ledger
marks the slot's field as `MovedOut`.  `field_verdict_at` returns
`MUST_NOT_DROP` → authoring SKIPS emission.

Net for the String slot: one retain (slot's original +1) is never
released.  **Leak per match arm execution.**

The whole-variant `DropValue` (codegen `_emit_drop_value`) does NOT
consult the ledger and would correctly release the slot — but the
partial-move branch suppresses the whole-variant drop precisely to
avoid double-dropping moved-out Token bytes.

**Suspected root cause:**
- `_apply_field_state` over-reports MovedOut for any
  VariantGetFieldAddr-of-tracked-local, regardless of whether the
  downstream consumer was a `MoveOut` or a `CopyValue`.  The Move-vs-
  Copy distinction is not modeled at per-field state granularity.

**Pre-existing:** this gap predates the whole-scrutinee migration
landed today.  The partial-move per-field cleanup logic and the
ledger's per-field walker have not changed in this branch; the bug
shape requires Copy-binder of a drop-needing type, which only became
common after 0.31.11's policy fix made String classify
`is_cheap_copy=True AND needs_drop=True` uniformly.

**Per K's protocol**: this regression pin lands FIRST.  If it leaks,
the bug is classified as a LANGUAGE_BUG in the per-field ledger walker
/ partial-move cleanup path, and that gets fixed before any
candidate-set restructuring.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from lang.codegen.llvm.test_utils import valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]


# Carrier: Token (non-Copy, Destructible) + String (refcounted-scalar Copy).
# Token's MOVE binder forces the partial-move branch.  String's Copy binder
# leaves the slot's +1 owned but unreleased.
CARRIER_SOURCE = """\
module main;

import std.core as core;
import std.format as fmt;

pub struct Session {
\tpub drops: Int,
}

pub struct Token {
\tpub session: &mut Session,
}

implement core.Destructible for Token {
\tpub fn destroy(self: Token) nothrow -> Void {
\t\tself.session.drops = self.session.drops + 1;
\t}
}

pub variant Pair {
\tPair(t: Token, s: String),
}

fn run(sess: &mut Session) nothrow -> Int {
\tvar accum: Int = 0;
\tval s: String = fmt.format_int(42);
\tval p: Pair = Pair::Pair(t = Token(session = sess), s = s);
\t// Statement-context match (no value).
\tmatch p {
\t\tPair(t, s_bound) => {
\t\t\t// Both binders used so the binder loop runs each branch.
\t\t\t// Token binder: MOVE (non-Copy) → moved_field_indices, payload_moved=True.
\t\t\t// String binder: COPY (refcounted-scalar Copy) → retain +1, slot keeps its +1.
\t\t\taccum = s_bound.byte_length();
\t\t\tval _ = move t;
\t\t}
\t}
\treturn accum;
}

pub fn main() nothrow -> Int {
\tvar sess: Session = Session(drops = 0);
\tval n1: Int = run(sess);
\tval n2: Int = run(sess);
\tval n3: Int = run(sess);
\treturn n1 + n2 + n3;
}
"""


# Control: both fields are Token (non-Copy, Destructible).
# Both binders take the MOVE branch — both fields end up in moved_field_indices,
# both filtered from the per-field cleanup loop.  No String slot to leak.
CONTROL_SOURCE = """\
module main;

import std.core as core;

pub struct Session {
\tpub drops: Int,
}

pub struct Token {
\tpub session: &mut Session,
}

implement core.Destructible for Token {
\tpub fn destroy(self: Token) nothrow -> Void {
\t\tself.session.drops = self.session.drops + 1;
\t}
}

pub variant Pair2 {
\tPair2(a: Token, b: Token),
}

fn run(sa: &mut Session, sb: &mut Session) nothrow -> Int {
\tval p: Pair2 = Pair2::Pair2(a = Token(session = sa), b = Token(session = sb));
\t// Statement-context match (no value).
\tmatch p {
\t\tPair2(a, b) => {
\t\t\tval _ = move a;
\t\t\tval _ = move b;
\t\t}
\t}
\treturn 0;
}

pub fn main() nothrow -> Int {
\tvar sa: Session = Session(drops = 0);
\tvar sb: Session = Session(drops = 0);
\tval _ = run(sa, sb);
\tval _ = run(sa, sb);
\tval _ = run(sa, sb);
\treturn 0;
}
"""


def _compile_and_valgrind(tmp_path: Path, source: str, *, label: str) -> tuple[int, str]:
	"""Compile `source` under raw stdlib and run under valgrind.

	Returns (definitely_lost_bytes, valgrind_log_text).
	"""
	assert shutil.which("valgrind") is not None, "valgrind required"

	src = tmp_path / "main.drift"
	src.write_text(source)
	out_bin = tmp_path / "test_bin"

	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, f"[{label}] compile failed: {res.stderr[:1000]}"
	assert out_bin.exists(), f"[{label}] binary not produced"

	vg_log = tmp_path / f"valgrind_{label}.log"
	subprocess.run(
		valgrind_cmd("--leak-check=full",
			"--show-leak-kinds=definite,indirect",
			"--errors-for-leak-kinds=definite,indirect",
			"--error-exitcode=97",
			f"--log-file={vg_log}",
			str(out_bin)),
		capture_output=True, text=True, timeout=120,
	)
	vg_output = vg_log.read_text() if vg_log.exists() else ""
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	definitely_lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	return definitely_lost, vg_output


def test_partial_move_copy_binder_string_slot_no_leak(tmp_path: Path) -> None:
	"""**LANGUAGE_BUG carrier**: partial-move arm with Copy-binder of
	String must release the slot's +1.

	If this fails, the per-field ledger walker
	(`_apply_field_state`) is over-reporting `MovedOut` for the
	Copy-bound String field, causing `match_cleanup_authoring` to
	skip the slot's drop.  The leak is proportional to the number
	of `run()` invocations (~24 bytes per call: DriftString header
	+ refcount-arc + small bytes for `format_int(42)`).

	Fix surface (when this fails):
	1. **Tighten `_apply_field_state`** to distinguish Move from
	   Copy at per-field granularity — only mark MovedOut on a
	   confirmed downstream `MoveOut` chain (sketched as a step-3c
	   follow-up at `ownership_ledger.py:729-738`).  THE preferred
	   fix.
	2. Or: have `match_cleanup_authoring` look beyond `field_verdict_at`
	   to disambiguate the Copy case.  Pushes the Move-vs-Copy
	   distinction into authoring, which K rejects (would move the
	   bug into the authority layer).
	3. Or: have HIR-side Filter A populate `moved_field_indices`
	   only with Move binders (already does) and additionally
	   handle Copy-binders-of-drop-needing-types by emitting a
	   field-cleanup record explicitly bypassing the ledger query.
	   Keeps the bug fenced but doesn't fix the per-field walker.

	K's directive on first failure: freeze the failing state and
	diagnose option 1.  Do not restructure candidate-set selection.
	"""
	lost, vg_log = _compile_and_valgrind(tmp_path, CARRIER_SOURCE, label="carrier")
	assert lost == 0, (
		f"LANGUAGE_BUG: partial-move arm with Copy-binder of String "
		f"leaks the slot's +1.\n"
		f"definitely lost: {lost} bytes (across 3 run() invocations).\n"
		f"Likely root cause: `_apply_field_state` in "
		f"`lang/driftc/stage2/ownership_ledger.py` over-reports "
		f"`MovedOut` for the Copy-bound String field; "
		f"`match_cleanup_authoring` queries `field_verdict_at` and "
		f"skips emission.\n\n"
		f"Valgrind log tail:\n{vg_log[-1500:]}"
	)


def test_partial_move_all_move_binders_control(tmp_path: Path) -> None:
	"""Control: partial-move arm with ALL-Move binders (Token + Token)
	must not leak.

	Both binders take the MOVE branch → both field indices in
	`moved_field_indices` → both filtered from per-field cleanup
	(Filter A applies to both).  Whole-variant drop is suppressed
	(correct — would re-drop moved Tokens).  Each binder's destroy
	runs at scope exit on the moved bytes.  No slot retains a +1
	that needs releasing.

	If this control fails, the bug is broader than the Copy-binder
	case — likely a regression in the partial-move filter logic
	itself.
	"""
	lost, vg_log = _compile_and_valgrind(tmp_path, CONTROL_SOURCE, label="control")
	assert lost == 0, (
		f"Control regression: all-Move-binders partial-move arm "
		f"leaks {lost} bytes.  This case should be unaffected by the "
		f"Copy-binder candidate-set issue — both fields are filtered "
		f"by Filter A.  If this fails, the partial-move per-field "
		f"cleanup path has a wider regression than the Copy-binder "
		f"shape.\n\nValgrind log tail:\n{vg_log[-1500:]}"
	)
