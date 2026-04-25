# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
LANGUAGE_BUG regression: `compute_drop_policy.needs_drop` is wrong
for `Copy && has_drop` cases (post-link).

**Origin (2026-04-24)**: surfaced during the whole-scrutinee
migration boundary-bug investigation.  After fixing Bug 1 (trait
canonicalization across the .dmp boundary), raw and pkg builds
agree that `Optional<String>.copy_status = True` — the trait
system's correct answer per the registered impls.  But that makes
`compute_drop_policy(Optional<String>).needs_drop = False` because
the policy short-circuits on `copy_status`:

```python
if contains_dv: needs_drop = True
elif copy_status is True: needs_drop = False  # ← wrong for String
else: needs_drop = raw_has_drop
```

For `String`: `copy_status=True` (the bytes ARE bitcopyable —
DriftString is `{len, ptr}`) AND `has_drop=True` (the underlying
refcount must be released via `drift_string_release`).  The
current rule treats Copy as "no drop needed" — leaks every
refcount transfer when consumed by a cleanup site reading
`compute_drop_policy.needs_drop`.

The `compute_drop_policy` docstring (`hir_to_mir.py:230+`)
acknowledges the contract: "Refcounted scalar (`String`),
structural-with-drop (`Optional<String>`, `Array<String>`, structs
with droppable fields), and user-`Destructible` types are True"
for `needs_drop`.  The implementation does not match that
contract.

**Why post-link**: `String.copy_status` returns False on a fresh
TypeTable (the structural-fallback's `if name == "String": return
False` special-case at `types_core.py:2631-2632` is what catches
it).  After `_install_copy_query` (driftc.py:503) installs the
trait-prover callback, `_query_copy(String)` walks the trait world,
finds `implement Copy for String` (`stdlib/std/core/copy.drift:374`),
returns True.  This is the post-link state every real compile sees.

These regressions drive the same compile path (raw or pkg) that
patches a real fixture and queries the post-link `compute_drop_policy`
through the `[drift:type-query]` diagnostic.  The diagnostic dumps
both `copy_status` and the policy axes that determine `needs_drop`
indirectly via `is_cheap_copy`/`has_structural_drop`, so the test
can verify the rule without re-deriving the policy in Python.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from lang.tests.driver.pkg_test_helpers import ROOT


_CONSUMER = """\
module main;

import std.core as core;
import std.format as fmt;

pub fn main() nothrow -> Int {
\tval s: String = fmt.format_int(42);
\tval opt: Optional<String> = Optional<type String>::Some(s);
\tval ints: Optional<Int> = Optional<type Int>::Some(7);
\tmatch opt {
\t\tSome(_) => { },
\t\tNone => { },
\t}
\tmatch ints {
\t\tSome(_) => { return 0; },
\t\tNone => { return 1; },
\t}
}
"""


_TYPE_QUERY_RE = re.compile(r"^\[drift:type-query\] (.*)$", re.MULTILINE)


def _run_compile(tmp_path: Path) -> list[dict]:
	tmp_path.mkdir(parents=True, exist_ok=True)
	src = tmp_path / "main.drift"
	src.write_text(_CONSUMER)
	out_bin = tmp_path / "out_bin"
	env = os.environ.copy()
	env["DRIFT_DUMP_TYPE_QUERIES"] = "1"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main",
		 "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=120, env=env,
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:500]}"
	out: list[dict] = []
	for m in _TYPE_QUERY_RE.finditer(res.stderr):
		try:
			out.append(json.loads(m.group(1)))
		except json.JSONDecodeError:
			pass
	return out


def _find_one(records: list[dict], *, name: str, type_arg_names: list[str] | None = None, module: str | None = None) -> dict | None:
	for r in records:
		if r.get("name") != name:
			continue
		if type_arg_names is not None and r.get("type_arg_names") != type_arg_names:
			continue
		if module is not None and r.get("module") != module:
			continue
		return r
	return None


def test_string_post_link_must_require_drop(tmp_path: Path) -> None:
	"""**LANGUAGE_BUG**: post-link, `compute_drop_policy(String)
	.needs_drop` MUST be True.  The String refcount must be released
	via `drift_string_release` even though `copy_status=True`.

	Pre-fix: `copy_status=True, has_drop=True, policy_needs_drop=False`
	(the `if copy_status is True: needs_drop = False` short-circuit
	in `lang/driftc/stage2/drop_policy_compute.py:60-65` fires).

	Post-fix: `policy_needs_drop=True` — the policy is driven by
	destruction semantics (`has_drop`/`destructible`), not
	short-circuited by `copy_status`."""
	records = _run_compile(tmp_path)
	rec = _find_one(records, name="String")
	assert rec is not None, (
		"expected a `String` type-query record from the compile.  "
		"Either the diagnostic regressed or the fixture stopped "
		"surfacing String."
	)
	assert rec["copy_status"] is True, (
		f"baseline check: String.copy_status MUST be True post-link "
		f"(via `implement Copy for String` in std.core/copy.drift).  "
		f"Got {rec['copy_status']!r}."
	)
	assert rec["has_drop"] is True, (
		f"baseline check: String.has_drop MUST be True (refcount "
		f"release via drift_string_release).  Got {rec['has_drop']!r}."
	)
	assert rec["policy_needs_drop"] is True, (
		f"LANGUAGE_BUG: compute_drop_policy(String).needs_drop MUST "
		f"be True — String has has_drop=True (refcount must be "
		f"released).  Got {rec['policy_needs_drop']!r}.  The "
		f"`if copy_status is True: needs_drop=False` short-circuit "
		f"in `compute_drop_policy` (`drop_policy_compute.py:60-65`) "
		f"is wrong for Copy && has_drop types like String.  See "
		f"`work/ownership-ledger/whole-scrutinee-investigation.md` "
		f"and the DropPolicy docstring at `hir_to_mir.py:230+` "
		f"which says `Refcounted scalar (String)` is True for "
		f"needs_drop."
	)


def test_optional_string_post_link_inherits_drop(tmp_path: Path) -> None:
	"""Companion: `Optional<String>` post-link.  copy_status=True
	(via the `implement<T> Copy for Optional<T> require T is Copy`
	impl with String's Copy proof — see Bug 1 fix).  has_drop=True
	(transitively via String's has_drop).  Same Copy-and-has-drop
	conflict as String itself; the policy bug propagates through the
	container."""
	records = _run_compile(tmp_path)
	rec = _find_one(records, name="Optional", type_arg_names=["String"])
	assert rec is not None, (
		"expected an `Optional<String>` type-query record from the "
		"compile.  Either the fixture stopped surfacing it or the "
		"diagnostic regressed."
	)
	assert rec["copy_status"] is True, (
		f"baseline (post-Bug-1 fix): Optional<String>.copy_status MUST "
		f"be True.  Got {rec['copy_status']!r}.  If this fails, Bug 1 "
		f"(trait-link canonicalization) regressed."
	)
	assert rec["has_drop"] is True, (
		f"baseline: Optional<String>.has_drop MUST be True (the inner "
		f"String has has_drop=True, so the variant's structural drop "
		f"bubbles up).  Got {rec['has_drop']!r}."
	)
	assert rec["policy_needs_drop"] is True, (
		f"LANGUAGE_BUG (transitive): compute_drop_policy(Optional<String>)"
		f".needs_drop MUST be True — the inner String's refcount "
		f"must be released when the variant drops.  Got "
		f"{rec['policy_needs_drop']!r}.  Same root cause as String "
		f"itself: copy_status short-circuit in compute_drop_policy."
	)


def test_optional_int_post_link_no_drop_control(tmp_path: Path) -> None:
	"""Control: `Optional<Int>` post-link.  copy_status=True (Int is
	bitcopy POD).  has_drop=False (no drop work).  Policy should
	correctly return needs_drop=False — confirms the fix doesn't
	regress true-POD cases."""
	records = _run_compile(tmp_path)
	rec = _find_one(records, name="Optional", type_arg_names=["Int"])
	assert rec is not None, (
		"expected an `Optional<Int>` type-query record from the "
		"compile."
	)
	assert rec["copy_status"] is True, (
		f"baseline: Optional<Int>.copy_status MUST be True.  Got "
		f"{rec['copy_status']!r}."
	)
	assert rec["has_drop"] is False, (
		f"baseline: Optional<Int>.has_drop MUST be False (Int is POD; "
		f"variant containing only POD fields has no structural drop).  "
		f"Got {rec['has_drop']!r}.  If this fails, the fix to "
		f"compute_drop_policy regressed and now flags POD types for "
		f"drop."
	)
	assert rec["policy_needs_drop"] is False, (
		f"control: compute_drop_policy(Optional<Int>).needs_drop "
		f"MUST be False (POD inner — no drop work to do).  Got "
		f"{rec['policy_needs_drop']!r}.  If this fails, the fix to "
		f"compute_drop_policy over-drops true-POD types."
	)
