# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""ConstShare structural synthesis — Phase 4 non-generic variants.

Phase 4 scope (per `work/constshare-substrate/post-link-mandatory-design.md`):
  - variants only,
  - non-generic variants (no type_params),
  - concrete payload field types,
  - no implicit `var b = a` value-flow synthesis.

Phase 4 contract: a non-generic variant auto-derives ConstShare iff
EVERY arm's payload fields qualify under the same v1 composition rule
used for structs:
  - field is `core.ConstArc<U>` / another ConstShare type, OR
  - field is `Copy + Frozen` scalar / nominal.

Synthesized method body is a real HIR `match` over `self`:
each arm reconstructs the same variant case with per-field
transformation (`.const_share()` for ConstShare, direct copy for
Copy+Frozen).  No tag-only path tricks.

Generic variants are NOT covered by this slice.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


def _compile(tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str) -> tuple[int, list[dict]]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	rc = driftc_main(["--stdlib-root", "stdlib", "--test-build-only", str(src), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	errs = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	return rc, errs


_PRE = """
module main;

import std.core as core;
import std.core.shareable as shareable;

use trait shareable.ConstShare;

fn assert_cs<T>() nothrow -> Void require T is shareable.ConstShare { }
"""


# ── Positive — variants whose every payload qualifies ────────────


def test_phase4_payload_less_variant_derives(tmp_path, capsys):
	"""All-zero-payload variant: every arm qualifies trivially."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub variant Color {
\tRed,
\tGreen,
\tBlue
}

fn main() nothrow -> Int {
\tassert_cs<type Color>();
\tval c = Color::Red();
\tval c2 = c.const_share();
\treturn 0;
}
""")
	assert rc == 0, (
		f"payload-less variant must derive ConstShare: rc={rc}, errs:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)


def test_phase4_copy_frozen_payload_variant_derives(tmp_path, capsys):
	"""`Pair(a: Int, b: Int)` qualifies via Copy+Frozen on both fields."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub variant Tagged {
\tNone,
\tPair(a: Int, b: Int)
}

fn main() nothrow -> Int {
\tassert_cs<type Tagged>();
\tval t = Tagged::Pair(1, 2);
\tval t2 = t.const_share();
\treturn 0;
}
""")
	assert rc == 0, (
		f"Copy+Frozen-payload variant must derive: rc={rc}, errs:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)


def test_phase4_const_arc_payload_variant_derives(tmp_path, capsys):
	"""`Wrap(handle: ConstArc<String>)` qualifies via the
	ConstShare path on the field."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub variant Carrier {
\tEmpty,
\tWrap(handle: core.ConstArc<String>)
}

fn main() nothrow -> Int {
\tassert_cs<type Carrier>();
\tval inner = core.const_arc<type String>("hi");
\tval c = Carrier::Wrap(inner);
\tval c2 = c.const_share();
\treturn 0;
}
""")
	assert rc == 0, (
		f"ConstArc-payload variant must derive: rc={rc}, errs:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)


def test_phase4_mixed_arms_variant_derives(tmp_path, capsys):
	"""Mixed arms: empty + Copy+Frozen-payload + ConstShare-payload
	all qualify together; whole variant derives; method dispatch
	resolves; round-trip-construct works."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub variant Multi {
\tEmpty,
\tNumber(n: Int),
\tText(handle: core.ConstArc<String>),
\tPair(a: Int, b: Int)
}

fn main() nothrow -> Int {
\tassert_cs<type Multi>();
\tval m1 = Multi::Empty();
\tval m1_2 = m1.const_share();
\tval m2 = Multi::Number(42);
\tval m2_2 = m2.const_share();
\tval inner = core.const_arc<type String>("hello");
\tval m3 = Multi::Text(inner);
\tval m3_2 = m3.const_share();
\tval m4 = Multi::Pair(1, 2);
\tval m4_2 = m4.const_share();
\treturn 0;
}
""")
	assert rc == 0, (
		f"mixed-arms variant must derive: rc={rc}, errs:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)


# ── Negative — any non-qualifying arm blocks derivation ──────────


def _assert_does_not_derive(rc: int, errs: list[dict], label: str) -> None:
	assert rc != 0, f"{label}: must not auto-derive (any non-qualifying arm blocks)"
	rejected = any(
		e.get("code") == "E_REQUIREMENT_NOT_SATISFIED"
		and "ConstShare" in e.get("message", "")
		for e in errs
	)
	assert rejected, (
		f"{label}: expected E_REQUIREMENT_NOT_SATISFIED naming "
		f"ConstShare; got:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)


def test_phase4_arc_payload_blocks_derivation(tmp_path, capsys):
	"""`Arc<T>` is shared-mutable, never ConstShare; any arm
	carrying it blocks the whole variant."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub variant V {
\tA,
\tB(handle: core.Arc<String>)
}

fn main() nothrow -> Int {
\tassert_cs<type V>();
\treturn 0;
}
""")
	_assert_does_not_derive(rc, errs, "Arc payload arm")


def test_phase4_array_payload_blocks_derivation(tmp_path, capsys):
	"""`Array<T>` is owned-mutable; any arm carrying it blocks."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub variant V {
\tA,
\tB(items: Array<Int>)
}

fn main() nothrow -> Int {
\tassert_cs<type V>();
\treturn 0;
}
""")
	_assert_does_not_derive(rc, errs, "Array payload arm")


def test_phase4_ref_payload_blocks_derivation(tmp_path, capsys):
	"""Borrowed `&T` payloads cannot survive const_share's
	value-returning contract; blocks derivation."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub variant V {
\tA,
\tB(r: &Int)
}

fn main() nothrow -> Int {
\tassert_cs<type V>();
\treturn 0;
}
""")
	# &T variants may be rejected outright by the parser/typecheck
	# before synthesis sees them.  Either rc != 0 with a ConstShare-
	# related diagnostic OR rc != 0 with a parser/typecheck refusal
	# of the variant declaration is acceptable; what's NOT acceptable
	# is `rc == 0` (deriving a variant that holds a borrow).
	assert rc != 0, "borrowed-payload variant must not derive"
