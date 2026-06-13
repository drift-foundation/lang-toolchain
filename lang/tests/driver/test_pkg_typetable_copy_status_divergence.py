# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression pin: package-loaded vs source-loaded stdlib must produce
identical `type_table.copy_status(ty)` answers for the same logical
Drift type.

**Origin (2026-04-24)**: investigation of the whole-scrutinee
migration boundary bug surfaced a TypeId-identity divergence between
raw-stdlib (`--stdlib-root`) and signed-package-stdlib (`.dmp`) compile
modes.  Specifically: `Optional<String>` queries `copy_status=False`
when stdlib is loaded from source, and `copy_status=True` when stdlib
is loaded from a package — for the SAME logical Drift type compiled
from the SAME stdlib source.

The behavioral consequence of this divergence:
  - `compute_drop_policy(type_table, Optional<String>).needs_drop`
    branches on `copy_status` first.  When pkg says `copy=True`, the
    policy short-circuits to `needs_drop=False`, skipping the
    `has_drop`/`contains_dv` check that raw correctly walks.
  - Any `cleanup_authoring`-style emission point that consults
    `compute_drop_policy.needs_drop` for an `Optional<String>` (or
    similar Optional<T>-of-destructible) candidate will SKIP the
    drop in pkg builds while emitting it in raw builds.
  - Concrete leak surface: the `bookkeeper pattern`
    (`logger.info(_, {"port": fmt.format_int(...)})`) under the
    whole-scrutinee migration leaks the format_int-result String per
    call, because some Optional<...> on the std.log emit path is
    classified Copy in pkg → no drop authored → leak.  Pinned in
    `lang/tests/memcheck/test_pkg_vs_raw_stdlib_drop_policy_divergence.py`.

This test pins the divergence at the type-table query level (faster,
not memcheck-dependent) so a fix to the package-load / type-link
boundary can be verified without compile-and-run cycles.

The test is expected to FAIL on a clean checkpoint of the broken
boundary.  When the boundary bug is fixed (Vector 4: type_table_link_v0
Copy linking), this test should PASS.  Until then, it stays as the
regression carrier per K's 2026-04-24 protocol.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from lang.tests.driver.pkg_test_helpers import _build_signed_stdlib, STD_VERSION, ROOT

from lang.codegen.llvm.test_utils import sanitizer_timeout


_CONSUMER = """\
module {module_name};

import std.core as core;
import std.format as fmt;
import std.log as log;

pub fn main() nothrow -> Int {{
\tvar cb = log.config_builder();
\tcb.sink(log.stderr_sink());
\tcb.min_level(log.Level::Debug());
\tval cfg = cb.build();
\tval logger = log.create_logger("test", move cfg);
\tval _ = logger.info("startup", {{"port": fmt.format_int(18100)}});
\treturn 0;
}}
"""


_TYPE_QUERY_RE = re.compile(r"^\[drift:type-query\] (.*)$", re.MULTILINE)


def _parse_type_queries(stderr: str) -> list[dict]:
	"""Parse `[drift:type-query]` lines emitted by driftc when
	`DRIFT_DUMP_TYPE_QUERIES=1` is set."""
	out = []
	for m in _TYPE_QUERY_RE.finditer(stderr):
		try:
			out.append(json.loads(m.group(1)))
		except json.JSONDecodeError:
			pass
	return out


def _run_compile_with_dump(cmd: list[str]) -> list[dict]:
	env = os.environ.copy()
	env["DRIFT_DUMP_TYPE_QUERIES"] = "1"
	res = subprocess.run(cmd, capture_output=True, text=True, timeout=sanitizer_timeout(180), env=env, cwd=ROOT)
	assert res.returncode == 0, f"compile failed: {res.stderr[-500:]}"
	return _parse_type_queries(res.stderr)


def _shape_key(rec: dict) -> str:
	"""Build a comparison key from name + module + type_arg_names so
	a record can be matched between raw and pkg without depending on
	the per-build TypeId integers."""
	name = rec.get("name") or ""
	module = rec.get("module") or ""
	args = ",".join(rec.get("type_arg_names") or [])
	return f"{module}::{name}<{args}>"


def _compile_raw(tmp_path: Path) -> list[dict]:
	tmp_path.mkdir(parents=True, exist_ok=True)
	src = tmp_path / "main.drift"
	src.write_text(_CONSUMER.format(module_name="main"))
	out_bin = tmp_path / "raw_bin"
	return _run_compile_with_dump([
		sys.executable, "-m", "lang.driftc.driftc", "--dev",
		"--stdlib-root", str(ROOT / "stdlib"),
		str(src), "--entry", "main::main",
		"-o", str(out_bin),
	])


def _compile_pkg(tmp_path: Path) -> list[dict]:
	tmp_path.mkdir(parents=True, exist_ok=True)
	pkg_root, trust_path, core_trust_path, empty_stdlib = _build_signed_stdlib(tmp_path)
	consumer_dir = tmp_path / "consumer_src"
	consumer_dir.mkdir(exist_ok=True)
	(consumer_dir / "consumer.drift").write_text(_CONSUMER.format(module_name="consumer"))
	out_bin = tmp_path / "pkg_bin"
	return _run_compile_with_dump([
		sys.executable, "-m", "lang.driftc.driftc", "--dev",
		str(consumer_dir / "consumer.drift"),
		"--stdlib-root", str(empty_stdlib),
		"--package-root", str(pkg_root),
		"--dep", f"std@{STD_VERSION}",
		"--trust-store", str(trust_path),
		"--dev-core-trust-store", str(core_trust_path),
		"--target-word-bits", "64",
		"--entry", "consumer::main",
		"-o", str(out_bin),
	])


def test_optional_string_copy_status_matches_raw_and_pkg(tmp_path: Path) -> None:
	"""K-requested regression (2026-04-24): `Optional<String>` must
	have the SAME `copy_status` answer regardless of whether stdlib
	is source-loaded or package-loaded.  Pre-fix: raw=False, pkg=True
	(the bug).  Post-fix: both equal — typically False (Optional
	containing a refcounted String is not bitcopyable / Copy)."""
	raw_recs = _compile_raw(tmp_path / "raw")
	pkg_recs = _compile_pkg(tmp_path / "pkg")

	def _opt_string(recs: list[dict]) -> dict | None:
		for r in recs:
			if r.get("name") == "Optional" and r.get("type_arg_names") == ["String"]:
				return r
		return None

	raw_opt = _opt_string(raw_recs)
	pkg_opt = _opt_string(pkg_recs)
	assert raw_opt is not None, (
		"raw-stdlib build did not produce a type-query record for "
		"Optional<String>.  Fixture must instantiate this type "
		"transitively via std.log.  See logger.info call in fixture."
	)
	assert pkg_opt is not None, (
		"pkg-stdlib build did not produce a type-query record for "
		"Optional<String>.  Same as above; if stdlib's package-load "
		"path doesn't surface this instantiation, the test needs a "
		"different fixture."
	)

	assert raw_opt["copy_status"] == pkg_opt["copy_status"], (
		f"Optional<String> copy_status divergence (boundary bug):\n"
		f"  raw-stdlib:  copy_status={raw_opt['copy_status']!r}, "
		f"has_drop={raw_opt['has_drop']!r}, "
		f"is_destructible={raw_opt['is_destructible']!r}\n"
		f"  pkg-stdlib:  copy_status={pkg_opt['copy_status']!r}, "
		f"has_drop={pkg_opt['has_drop']!r}, "
		f"is_destructible={pkg_opt['is_destructible']!r}\n"
		f"\n"
		f"Per `compute_drop_policy`, copy_status=True short-circuits "
		f"`needs_drop=False`, skipping has_drop/contains_dv checks.  "
		f"For an Optional containing a refcounted String, "
		f"copy_status MUST be False — the variant is not bitcopyable.  "
		f"The pkg side returning True indicates the package-load "
		f"path's Copy-impl association for the inner String (or the "
		f"variant container) is incorrect.\n"
		f"\n"
		f"Investigation notes: work/ownership-ledger/whole-scrutinee-investigation.md"
	)


def test_optional_arg_copy_status_matches_for_all_shared_instantiations(tmp_path: Path) -> None:
	"""Broader pin (companion): for every Optional<T> instantiation
	that appears in BOTH raw and pkg builds (matched by type_arg_names),
	`copy_status` must match.  Catches regressions beyond the
	specific Optional<String> case while still avoiding false
	positives from build-specific instantiation differences."""
	raw_recs = _compile_raw(tmp_path / "raw")
	pkg_recs = _compile_pkg(tmp_path / "pkg")

	# Group by shape; multiple records per shape are possible (separate
	# instantiations at different sites).  Aggregate to a single
	# (copy_status, has_drop, is_destructible) tuple per shape — they
	# should all agree within a single build.
	def _by_shape(recs: list[dict]) -> dict[str, set[tuple]]:
		out: dict[str, set[tuple]] = {}
		for r in recs:
			if r.get("name") != "Optional":
				continue
			key = ",".join(r.get("type_arg_names") or [])
			facts = (r["copy_status"], r["has_drop"], r["is_destructible"])
			out.setdefault(key, set()).add(facts)
		return out

	raw_shapes = _by_shape(raw_recs)
	pkg_shapes = _by_shape(pkg_recs)

	mismatches: list[str] = []
	shared = sorted(set(raw_shapes.keys()) & set(pkg_shapes.keys()))
	for shape in shared:
		# `Optional<>` (no args) is a generic placeholder; skip — no
		# concrete copy/drop semantics until instantiated.
		if not shape:
			continue
		# `Optional<T>` / `Optional<V>` / `Optional<T0>` etc. are
		# uninstantiated typevar slots; skip.
		if shape in ("T", "V", "T0", "T1", "T2"):
			continue
		raw_facts = raw_shapes[shape]
		pkg_facts = pkg_shapes[shape]
		if raw_facts != pkg_facts:
			mismatches.append(f"Optional<{shape}>: raw={sorted(raw_facts)}, pkg={sorted(pkg_facts)}")

	assert not mismatches, (
		"Optional<T> copy_status / has_drop / is_destructible divergence "
		"between raw-stdlib and pkg-stdlib builds:\n  "
		+ "\n  ".join(mismatches)
		+ "\n\nThis surface is the type-link boundary bug surfaced during "
		"the whole-scrutinee migration investigation (2026-04-24).  See "
		"work/ownership-ledger/whole-scrutinee-investigation.md."
	)
