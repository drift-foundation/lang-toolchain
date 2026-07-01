# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression suite for the compile-timing instrumentation
(`lang/driftc/_events.py`, the `--timing` CLI flag on driftc, and the
sink lifecycle inside `driftc.main` / `compile_to_llvm_ir_for_tests`).

What these tests pin:

1. `events` module surface: install_sink/current_sink/timed/EventSink
   behave correctly (ContextVar cleanup; no-op when no sink installed;
   total_wall + phase accumulation; nested phases unwind cleanly).
2. driftc `--json` invariants:
   - WITHOUT `--timing`: exactly one JSON object on stdout, no
     `timings` field.
   - WITH `--timing`: exactly one JSON object on stdout, `timings`
     field carries `total_wall` (float), `phases` (dict of
     label→seconds), and `counts` (dict of label→invocations).
   - No `[drift:timing]` text on stdout under `--json` (text summary
     suppressed in JSON mode).
3. driftc text mode + `--timing`: stderr `[drift:timing]` summary
   present and well-formed.
4. Sink contamination: two sequential in-process compiles in one
   Python process get independent timings; neither sees the other's
   ContextVar state.
5. `compile_to_llvm_ir_for_tests` honours a caller-installed sink and
   does not double-install when one is present.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lang.driftc import _events as events

from lang.codegen.llvm.test_utils import sanitizer_timeout


ROOT = Path(__file__).resolve().parents[3]


# ── Module-level surface ──────────────────────────────────────────


class TestEventSinkSurface:
	"""Direct exercise of the `lang/driftc/_events.py` API."""

	def test_no_sink_installed_timed_is_cheap_noop(self) -> None:
		"""With no sink installed, `events.timed(label)` must be a
		bare yield -- no side effects, no exceptions."""
		assert events.current_sink() is None
		with events.timed("dummy"):
			pass
		# Still no sink installed (didn't accidentally create one).
		assert events.current_sink() is None

	def test_install_sink_resets_on_exit(self) -> None:
		"""`install_sink` must reset the ContextVar on every exit
		path (normal return + exception)."""
		# Normal return.
		sink = events.EventSink()
		with events.install_sink(sink):
			assert events.current_sink() is sink
		assert events.current_sink() is None

		# Exception path.
		sink2 = events.EventSink()
		with pytest.raises(RuntimeError):
			with events.install_sink(sink2):
				assert events.current_sink() is sink2
				raise RuntimeError("boom")
		assert events.current_sink() is None

	def test_phase_accumulation(self) -> None:
		"""Sequential same-label `timed` blocks accumulate both time
		AND the per-label invocation count."""
		sink = events.EventSink()
		with events.install_sink(sink):
			sink.begin_compile()
			with events.timed("a"):
				pass
			with events.timed("a"):
				pass
			sink.end_compile()
		summary = sink.timings_summary()
		assert "a" in summary["phases"]
		assert summary["phases"]["a"] > 0
		# Two iterations accumulated -- the recorded total must reflect
		# both (lower bound: same magnitude as one iteration; tighter
		# floor would race with timer resolution on fast machines).
		assert summary["total_wall"] >= 0
		# Counts contract: every phase that fired carries its
		# invocation count in the sibling `counts` map.  Pinning this
		# directly so the JSON contract can't silently drift apart
		# from the docs.
		assert "counts" in summary, "timings_summary() must include counts"
		assert summary["counts"].get("a") == 2, (
			f"counts['a'] should be 2 after two same-label timed blocks; "
			f"got {summary['counts']!r}"
		)

	def test_counts_in_summary_match_phase_keys(self) -> None:
		"""Every key in `phases` must have a matching `counts` entry,
		and `counts` for an un-fired label is absent.  Pins the
		"sibling map" contract documented in `lang/driftc/_events.py`
		and `doc/timing.md`."""
		sink = events.EventSink()
		with events.install_sink(sink):
			sink.begin_compile()
			with events.timed("alpha"):
				pass
			with events.timed("beta"):
				with events.timed("beta_inner"):
					pass
			sink.end_compile()
		summary = sink.timings_summary()
		# Every phases key has a counts entry.
		for label in summary["phases"]:
			assert label in summary["counts"], (
				f"label {label!r} present in phases but missing from counts"
			)
		# Single-fire labels are count=1.
		assert summary["counts"]["alpha"] == 1
		assert summary["counts"]["beta"] == 1
		assert summary["counts"]["beta_inner"] == 1
		# Un-fired label not present.
		assert "never_fired" not in summary["counts"]

	def test_merge_subprocess_timings_merges_counts_additively(self) -> None:
		"""Wrapper-side merge: child driftc's counts merge into the
		wrapper sink additively under the prefix.  Two sequential
		merges with the same prefix stack -- this is the "wrapper
		retried the child" case (e.g. drift_build invoking the same
		compile twice under `compile`).  drift_deploy's build vs
		smoke compiles use SEPARATE prefixes (`build.compile.*` vs
		`smoke.compile.*`) so they stay distinguishable; the
		accumulation behavior pinned here applies only WITHIN one
		prefix."""
		sink = events.EventSink()
		# Two independent child summaries under the same prefix
		# (retried compile under one wrapper session).
		sink.merge_subprocess_timings("compile", {
			"total_wall": 1.5,
			"phases": {"parse": 0.7, "codegen": 0.6},
			"counts": {"parse": 1, "codegen": 1},
		})
		sink.merge_subprocess_timings("compile", {
			"total_wall": 2.0,
			"phases": {"parse": 0.9, "codegen": 0.8},
			"counts": {"parse": 1, "codegen": 1},
		})
		summary = sink.timings_summary()
		# Phases stack (1.5 + 2.0 = 3.5 for total_wall, etc.).
		assert abs(summary["phases"]["compile.total_wall"] - 3.5) < 1e-6
		assert abs(summary["phases"]["compile.parse"] - 1.6) < 1e-6
		# Counts stack additively.
		assert summary["counts"]["compile.parse"] == 2
		assert summary["counts"]["compile.codegen"] == 2
		assert summary["counts"]["compile.total_wall"] == 2

	def test_merge_subprocess_timings_defaults_missing_counts(self) -> None:
		"""Backcompat: older driftc summaries omit the `counts`
		sibling.  The merge must default each observed phase to
		count=1 so the parent always sees at least one invocation
		per phase."""
		sink = events.EventSink()
		sink.merge_subprocess_timings("legacy", {
			"total_wall": 0.5,
			"phases": {"parse": 0.3, "codegen": 0.2},
			# `counts` deliberately omitted (older driftc).
		})
		summary = sink.timings_summary()
		assert summary["counts"]["legacy.parse"] == 1
		assert summary["counts"]["legacy.codegen"] == 1
		assert summary["counts"]["legacy.total_wall"] == 1

	def test_nested_phases_unwind_cleanly(self) -> None:
		"""Nested `timed` blocks each contribute to their own label."""
		sink = events.EventSink()
		with events.install_sink(sink):
			sink.begin_compile()
			with events.timed("outer"):
				with events.timed("inner"):
					pass
			sink.end_compile()
		summary = sink.timings_summary()
		assert "outer" in summary["phases"]
		assert "inner" in summary["phases"]

	def test_total_wall_present_when_begin_end_called(self) -> None:
		"""`total_wall` is 0.0 without begin/end; non-zero after."""
		sink = events.EventSink()
		assert sink.timings_summary()["total_wall"] == 0.0
		sink.begin_compile()
		sink.end_compile()
		assert sink.timings_summary()["total_wall"] >= 0.0

	def test_streamer_receives_progressive_events(self) -> None:
		"""Optional `stream_writer=` receives phase_start / phase_end
		events synchronously."""
		captured: list[dict] = []
		sink = events.EventSink(stream_writer=captured.append)
		with events.install_sink(sink):
			sink.begin_compile()
			with events.timed("p"):
				pass
			sink.end_compile()
		# Should have phase_start + phase_end for "p".
		assert {e.get("event") for e in captured} == {"phase_start", "phase_end"}
		assert all(e.get("phase") == "p" for e in captured)


# ── driftc CLI invariants ─────────────────────────────────────────


@pytest.fixture
def trivial_source(tmp_path: Path) -> Path:
	src = tmp_path / "smoke.drift"
	src.write_text(
		"module main;\n\npub fn main() nothrow -> Int {\n\treturn 0;\n}\n",
		encoding="utf-8",
	)
	return src


def _run_driftc(args: list[str]) -> subprocess.CompletedProcess:
	"""Invoke driftc as a subprocess (mirrors how external tools call it)."""
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", *args],
		capture_output=True, text=True, cwd=ROOT,
		timeout=sanitizer_timeout(120),
	)


def _common_driftc_args(src: Path) -> list[str]:
	return [
		str(src),
		"--stdlib-root", "stdlib",
		"--target-word-bits", "64",
		"--entry", "main::main",
		"--test-build-only",
	]


class TestDriftcJsonInvariants:
	"""Pin the `--json` / `--timing` payload contracts."""

	def test_json_no_timing_omits_timings_field(self, trivial_source: Path) -> None:
		res = _run_driftc(_common_driftc_args(trivial_source) + ["--json"])
		assert res.returncode == 0, f"compile failed: {res.stderr!r}"
		# Exactly-one-JSON-object invariant: stdout parses as ONE object.
		payload = json.loads(res.stdout)
		assert payload["exit_code"] == 0
		assert "timings" not in payload, (
			"--json without --timing must NOT carry the `timings` field; "
			f"got: {payload!r}"
		)

	def test_json_timing_includes_well_formed_timings(self, trivial_source: Path) -> None:
		res = _run_driftc(_common_driftc_args(trivial_source) + ["--json", "--timing"])
		assert res.returncode == 0, f"compile failed: {res.stderr!r}"
		payload = json.loads(res.stdout)
		assert "timings" in payload, (
			"--json --timing must surface a `timings` field; "
			f"got: {payload!r}"
		)
		t = payload["timings"]
		assert isinstance(t.get("total_wall"), float), (
			f"timings.total_wall must be a float; got: {t!r}"
		)
		assert isinstance(t.get("phases"), dict), (
			f"timings.phases must be a dict; got: {t!r}"
		)
		# At least one phase fires for a real compile (parse is the
		# most reliably-present one regardless of CLI shape).
		assert t["phases"], (
			f"timings.phases empty after a successful compile -- "
			f"instrumentation is not wired correctly.  payload: {payload!r}"
		)

	def test_json_timing_stdout_is_single_object(self, trivial_source: Path) -> None:
		"""Under `--json --timing`, stdout MUST be exactly one JSON
		object (no NDJSON, no concatenated objects, no human-readable
		`[drift:timing]` chatter leaking onto stdout)."""
		res = _run_driftc(_common_driftc_args(trivial_source) + ["--json", "--timing"])
		assert res.returncode == 0
		stripped = res.stdout.strip()
		# Strict parse + assert no trailing characters.
		_, idx = json.JSONDecoder().raw_decode(stripped)
		assert idx == len(stripped), (
			f"stdout under --json --timing is not exactly one JSON object "
			f"(extra content after offset {idx}); stdout: {stripped!r}"
		)
		assert "[drift:timing]" not in res.stdout, (
			f"text-mode timing chatter leaked onto stdout under --json: "
			f"{res.stdout!r}"
		)

	def test_text_mode_timing_summary_on_stderr(self, trivial_source: Path) -> None:
		"""`--timing` without `--json` prints the
		`[drift:timing]` summary on stderr.  Pins the documented
		text format including the percent + count columns so the
		shape can't silently regress."""
		import re as _re
		res = _run_driftc(_common_driftc_args(trivial_source) + ["--timing"])
		assert res.returncode == 0, f"compile failed: {res.stderr!r}"
		# Header line with total_wall.
		_header_re = _re.compile(r"^\[drift:timing\] total_wall=\d+\.\d{3}s$")
		_per_phase_re = _re.compile(
			# `[drift:timing]   <label>    =   N.NNNs  NN.N%  count=N`
			# Label may contain alphanumerics, underscores, dots (for
			# `compile.lower` style).  Counts are non-negative ints.
			r"^\[drift:timing\]\s{3}\S+\s+=\s+\d+\.\d{3}s\s+\d+\.\d%\s+count=\d+$"
		)
		header_seen = False
		phase_lines = 0
		for ln in res.stderr.splitlines():
			if ln.startswith("[drift:timing] total_wall="):
				assert _header_re.match(ln), (
					f"header line {ln!r} doesn't match expected shape "
					f"`[drift:timing] total_wall=N.NNNs`"
				)
				header_seen = True
				continue
			if ln.startswith("[drift:timing]   "):
				assert _per_phase_re.match(ln), (
					f"phase line {ln!r} doesn't match expected shape "
					f"`[drift:timing]   <label> = N.NNNs  NN.N%  count=N`"
				)
				phase_lines += 1
		assert header_seen, (
			f"missing `[drift:timing] total_wall=` header on stderr; "
			f"got: {res.stderr!r}"
		)
		assert phase_lines >= 1, (
			f"expected >= 1 phase line on stderr; got 0.  stderr: {res.stderr!r}"
		)


# ── In-process sink lifecycle ─────────────────────────────────────


class TestInProcessSinkLifecycle:
	"""Pin that in-process callers (test runners, e2e harness) can
	install their own sink and read it back, and that sequential
	compiles in one Python process get independent timings."""

	def test_caller_installed_sink_is_honoured_by_main(
		self, trivial_source: Path,
	) -> None:
		"""When a caller installs a sink before calling
		`driftc.main(...)`, the wrapper must honour the existing sink
		and not double-install."""
		from lang.driftc import driftc

		caller_sink = events.EventSink()
		with events.install_sink(caller_sink):
			caller_sink.begin_compile()
			try:
				rc = driftc.main(_common_driftc_args(trivial_source))
			finally:
				caller_sink.end_compile()
		assert rc == 0
		summary = caller_sink.timings_summary()
		assert summary["total_wall"] > 0.0
		# `parse` reliably fires in any compile shape.
		assert "parse" in summary["phases"], (
			f"caller sink saw no parse phase -- the main() wrapper may "
			f"have installed its own sink and recorded into that "
			f"instead.  caller summary: {summary!r}"
		)
		# Sink cleanup on exit.
		assert events.current_sink() is None

	def test_two_sequential_compiles_get_independent_timings(
		self, trivial_source: Path,
	) -> None:
		"""Two compiles in one Python worker must not contaminate
		each other's timings -- each gets its own sink."""
		from lang.driftc import driftc

		sink_a = events.EventSink()
		with events.install_sink(sink_a):
			sink_a.begin_compile()
			try:
				driftc.main(_common_driftc_args(trivial_source))
			finally:
				sink_a.end_compile()

		sink_b = events.EventSink()
		with events.install_sink(sink_b):
			sink_b.begin_compile()
			try:
				driftc.main(_common_driftc_args(trivial_source))
			finally:
				sink_b.end_compile()

		# Each sink saw its own compile -- both have a non-zero
		# total_wall, and neither is a leak from the other (we'd see
		# roughly 2x total in one of them if the ContextVar reset
		# weren't working).
		a = sink_a.timings_summary()
		b = sink_b.timings_summary()
		assert a["total_wall"] > 0.0
		assert b["total_wall"] > 0.0
		assert "parse" in a["phases"]
		assert "parse" in b["phases"]
		# Cleanup invariant.
		assert events.current_sink() is None

	def test_sink_is_garbage_collectable_after_exit(self) -> None:
		"""After `install_sink` exits, the ContextVar is reset.  Two
		successive `install_sink` blocks for *the same* sink instance
		work cleanly (no per-sink state that would prevent reuse).
		Pins that the ContextVar reset token semantics are correct
		even under nested or repeated installs."""
		sink = events.EventSink()
		with events.install_sink(sink):
			sink.begin_compile()
			with events.timed("p"):
				pass
			sink.end_compile()
		assert events.current_sink() is None

		# Re-install the SAME sink instance: accumulates additively.
		with events.install_sink(sink):
			sink.begin_compile()
			with events.timed("p"):
				pass
			sink.end_compile()
		assert events.current_sink() is None
		# After two install cycles, the phase 'p' has accumulated twice
		# (additive within one EventSink instance).
		assert sink.timings_summary()["phases"]["p"] >= 0


# ── Workload vector ───────────────────────────────────────────────


class TestWorkloadSinkSurface:
	"""Direct API contract for the workload counters.  Pins the
	set-vs-add split, the cheap no-op path, and the subprocess merge
	shape so the JSON contract can't silently drift."""

	def test_no_sink_workload_calls_are_cheap_noop(self) -> None:
		"""With no sink installed, `events.set_workload` /
		`events.add_workload` must be silent no-ops -- not raise, not
		create a sink."""
		assert events.current_sink() is None
		events.set_workload("anything", 42)
		events.add_workload("anything", 1)
		assert events.current_sink() is None

	def test_no_sink_skips_parser_token_count_attribute(self) -> None:
		"""Cheap-disabled-path contract: when no sink is installed,
		`parse_program` must NOT compute the token count or stamp
		`_parse_tree_token_count` onto the returned Program.  Pins
		that the metric-compute gate at the parser site actually
		skips the work (a test like
		`test_json_no_timing_omits_workload` proves only that no
		workload field is emitted -- it doesn't prove the per-file
		compute was skipped)."""
		from lang.driftc.parser import parser as _parser

		# Realistic source from an in-tree fixture (parser refuses an
		# empty string).  We don't care which fixture; we just need a
		# valid Drift program.
		fixture = ROOT / "lang/tests/codegen/e2e/int_leading_zero/main.drift"
		src = fixture.read_text(encoding="utf-8")

		# No sink installed: token count must NOT be stamped.
		assert events.current_sink() is None
		prog = _parser.parse_program(src, filename=str(fixture))
		assert not hasattr(prog, "_parse_tree_token_count"), (
			"parse_program stamped `_parse_tree_token_count` on the "
			"returned Program even though no sink was installed -- "
			"the cheap-disabled-path gate is broken."
		)

		# Sink installed: token count IS stamped.
		sink = events.EventSink()
		with events.install_sink(sink):
			prog2 = _parser.parse_program(src, filename=str(fixture))
		assert hasattr(prog2, "_parse_tree_token_count"), (
			"parse_program failed to stamp `_parse_tree_token_count` "
			"on the returned Program under an active sink -- the "
			"sink-active path is broken."
		)
		assert prog2._parse_tree_token_count > 0  # type: ignore[attr-defined]

	def test_set_workload_overwrites_within_sink(self) -> None:
		"""`set_workload` is last-writer-wins within one sink
		(snapshot semantics for compilation-shape values)."""
		sink = events.EventSink()
		with events.install_sink(sink):
			events.set_workload("source.input.files", 3)
			events.set_workload("source.input.files", 7)
		w = sink.timings_summary()["workload"]
		assert w["source.input.files"] == 7

	def test_add_workload_accumulates_within_sink(self) -> None:
		"""`add_workload` accumulates: two calls under one sink sum.
		Mirrors the contract that processed-work counters scale with
		repeated phase invocations alongside the elapsed time."""
		sink = events.EventSink()
		with events.install_sink(sink):
			events.add_workload("mir.processed.instructions", 1000)
			events.add_workload("mir.processed.instructions", 500)
		w = sink.timings_summary()["workload"]
		assert w["mir.processed.instructions"] == 1500

	def test_set_workload_drops_invalid_values(self) -> None:
		"""Producer-side `set_workload` enforces the same strict-int
		non-negative rule as the subprocess merge path.  Without
		this, a caller could publish `"58806"`, True, 1.5, or -1
		directly into a driftc --timing summary as valid schema-1
		workload, breaking the invariant that every published
		counter is a non-negative integer.

		Invalid inputs are SILENTLY DROPPED (the producer is
		in-process compiler code; a violating call is a caller bug
		that should be caught by tests, not surfaced as a runtime
		marker).  No exception raised so a workload bug can't take
		down a compile."""
		sink = events.EventSink()
		with events.install_sink(sink):
			# Bad inputs -- each should be a no-op.
			events.set_workload("key", "58806")        # type: ignore[arg-type]
			events.set_workload("key", True)           # type: ignore[arg-type]
			events.set_workload("key", False)          # type: ignore[arg-type]
			events.set_workload("key", 1.5)            # type: ignore[arg-type]
			events.set_workload("key", -1)
			events.set_workload("key", -1000000)
			events.set_workload("key", {"nested": 1})  # type: ignore[arg-type]
			events.set_workload("key", [1, 2])         # type: ignore[arg-type]
			events.set_workload("key", None)           # type: ignore[arg-type]
		w = sink.timings_summary()["workload"]
		# No value made it through any of the bad calls.
		assert "key" not in w, (
			f"set_workload silently accepted an invalid value; got: {w!r}"
		)

	def test_set_workload_accepts_zero_and_positive(self) -> None:
		"""Boundary: 0 is a legitimate non-negative integer (e.g.
		"this compile emitted no generic instances") and must be
		accepted.  Only negatives are rejected."""
		sink = events.EventSink()
		with events.install_sink(sink):
			events.set_workload("zero", 0)
			events.set_workload("positive", 123456)
		w = sink.timings_summary()["workload"]
		assert w["zero"] == 0
		assert w["positive"] == 123456

	def test_add_workload_drops_invalid_deltas(self) -> None:
		"""Producer-side `add_workload` enforces the same strict-int
		non-negative rule.  A negative delta would violate the
		"every v1 counter is a count or byte size" invariant; we
		deliberately do not provide a subtract operation.  Invalid
		inputs silently drop, leaving any previously-accumulated
		value untouched."""
		sink = events.EventSink()
		with events.install_sink(sink):
			# Seed with a known value.
			events.add_workload("key", 100)
			# Bad deltas -- each must be a no-op (leave value at 100).
			events.add_workload("key", "5")           # type: ignore[arg-type]
			events.add_workload("key", True)          # type: ignore[arg-type]
			events.add_workload("key", 0.5)           # type: ignore[arg-type]
			events.add_workload("key", -1)
			events.add_workload("key", -50)
			events.add_workload("key", {"d": 1})      # type: ignore[arg-type]
			events.add_workload("key", None)          # type: ignore[arg-type]
		w = sink.timings_summary()["workload"]
		assert w["key"] == 100, (
			f"add_workload mutated the accumulator on invalid delta; "
			f"got: {w!r}"
		)

	def test_add_workload_accepts_zero_delta(self) -> None:
		"""A zero delta is a legitimate no-op accumulate (e.g. a
		phase that ran but processed nothing) and must be accepted
		-- it must NOT collide with the silent-drop behavior for
		invalid deltas."""
		sink = events.EventSink()
		with events.install_sink(sink):
			events.add_workload("key", 10)
			events.add_workload("key", 0)
			events.add_workload("key", 5)
		w = sink.timings_summary()["workload"]
		assert w["key"] == 15

	def test_workload_schema_present_in_summary(self) -> None:
		"""`timings_summary()` always carries `workload_schema` so
		readers can detect a schema bump.  Empty workload still
		exposes the schema."""
		sink = events.EventSink()
		summary = sink.timings_summary()
		assert summary["workload_schema"] == events.EventSink.WORKLOAD_SCHEMA
		assert summary["workload"] == {}

	def test_merge_subprocess_workload_prefixes_and_accumulates(self) -> None:
		"""Cross-process merge: child workload keys become
		`<prefix>.<key>` on the parent; repeated merges with the same
		prefix accumulate (matches the timing merge model and is the
		correct behavior for retried child compiles -- both phase
		time and denominators accumulate together).

		Both calls forward the child's `workload_schema` explicitly
		so the merge is schema-checked (matches the wrapper
		callers in `drift_build` / `drift_deploy`)."""
		sink = events.EventSink()
		_schema = events.EventSink.WORKLOAD_SCHEMA
		sink.merge_subprocess_workload("compile", {
			"workload_schema": _schema,  # meta key, must be ignored
			"mir.processed.instructions": 1000,
			"source.input.files": 2,
		}, sub_schema=_schema)
		sink.merge_subprocess_workload("compile", {
			"mir.processed.instructions": 500,
			"source.input.files": 3,
		}, sub_schema=_schema)
		w = sink.timings_summary()["workload"]
		# Prefixed keys.
		assert w["compile.mir.processed.instructions"] == 1500
		assert w["compile.source.input.files"] == 5
		# `workload_schema` is meta -- must NOT be merged as a key
		# under the prefix.
		assert "compile.workload_schema" not in w
		# Schema-matched merge leaves no mismatch marker.
		assert "compile.workload_schema_mismatch" not in w

	def test_merge_subprocess_workload_refuses_schema_mismatch(self) -> None:
		"""When the child reports a different `workload_schema`, the
		merge is REFUSED and a `<prefix>.workload_schema_mismatch`
		marker is published instead.  Prevents the wrapper from
		silently publishing child counters under the parent's schema
		label when the child ran a different toolchain version with
		potentially different counter semantics."""
		sink = events.EventSink()
		child_schema = events.EventSink.WORKLOAD_SCHEMA + 99
		sink.merge_subprocess_workload(
			"compile",
			{"mir.processed.instructions": 1000},
			sub_schema=child_schema,
		)
		w = sink.timings_summary()["workload"]
		# Counter NOT merged (would have been mislabeled).
		assert "compile.mir.processed.instructions" not in w
		# Mismatch marker present, value = child's schema for
		# operator visibility.
		assert w["compile.workload_schema_mismatch"] == child_schema

	def test_merge_subprocess_workload_refuses_missing_schema(self) -> None:
		"""A non-empty child workload with `sub_schema=None` is
		unversioned data and MUST NOT be labeled as v1.  The merge
		is refused and a `<prefix>.workload_schema_unknown` marker
		is published.

		Every producer in this toolchain stamps `workload_schema`;
		this branch defends against the failure mode where the child
		emitted counters but the wrapper somehow passed None
		(corrupt JSON missing the field).  There is no legacy
		back-compat path."""
		sink = events.EventSink()
		sink.merge_subprocess_workload(
			"compile",
			{"mir.processed.instructions": 7},
			sub_schema=None,
		)
		w = sink.timings_summary()["workload"]
		# Counter NOT merged.
		assert "compile.mir.processed.instructions" not in w
		# Unknown-schema marker present.
		assert w["compile.workload_schema_unknown"] == 1
		# Not confused with the mismatch marker.
		assert "compile.workload_schema_mismatch" not in w

	def test_merge_subprocess_workload_non_positive_schema_is_unknown(self) -> None:
		"""Schemas start at 1 and we don't support a pre-v1 or
		negative schema range -- any int `< 1` is corrupt input,
		not a meaningful alternate schema.  Routes to
		`workload_schema_unknown` instead of
		`workload_schema_mismatch=<negative_value>`, which would
		leak a negative value into the workload output despite the
		non-negative-counter invariant."""
		sink = events.EventSink()
		# Negative and zero schemas: both invalid, both produce the
		# unknown marker (not a mismatch with a negative payload).
		sink.merge_subprocess_workload(
			"neg",
			{"mir.processed.instructions": 7},
			sub_schema=-1,
		)
		sink.merge_subprocess_workload(
			"zero",
			{"mir.processed.instructions": 7},
			sub_schema=0,
		)
		w = sink.timings_summary()["workload"]
		# Unknown markers present (always value=1 -- a presence
		# flag, not the rejected schema value).
		assert w["neg.workload_schema_unknown"] == 1
		assert w["zero.workload_schema_unknown"] == 1
		# Critically: no mismatch marker with a negative or zero
		# value leaked through.
		assert "neg.workload_schema_mismatch" not in w
		assert "zero.workload_schema_mismatch" not in w
		# And counters did NOT merge.
		assert "neg.mir.processed.instructions" not in w
		assert "zero.mir.processed.instructions" not in w

	def test_merge_subprocess_workload_refuses_malformed_schema(self) -> None:
		"""A corrupt `--timing-out` JSON could surface a non-numeric
		`workload_schema` (string, dict, list).  The merge must
		refuse such input without raising -- a crash here would
		surface AFTER the user's compile already succeeded, breaking
		the wrapper summary for an avoidable reason.

		Strict-int contract: the merge rejects ANY value that is
		not an actual `int` (and explicitly rejects `bool`, which
		subclasses `int`).  Coercible-but-wrong-type values
		(`"1"`, `True`, `1.5`) MUST land in the unknown bucket --
		`int(...)` coercion would silently relabel them as schema
		1 and contaminate dashboards.

		Any of these inputs lands in the same
		`<prefix>.workload_schema_unknown` bucket as a missing
		schema (both are "we don't know what version of the
		semantics produced these counters")."""
		sink = events.EventSink()
		# Plain non-numeric inputs (string/dict/list) -- these would
		# have raised under `int(...)` before the strict check.
		for _label, _bad in (
			("str_nonint", "not-an-int"),
			("dict", {"nested": 1}),
			("list", [1, 2, 3]),
			# Coercible-but-invalid: `int()` would accept these and
			# silently mislabel.  Strict check rejects them.
			("str_int", "1"),
			("bool_true", True),
			("bool_false", False),
			("float", 1.5),
		):
			sink.merge_subprocess_workload(
				f"compile.{_label}",
				{"mir.processed.instructions": 7},
				sub_schema=_bad,  # type: ignore[arg-type]
			)
		w = sink.timings_summary()["workload"]
		# Every bad input produced an unknown marker, none produced
		# a merged counter or a raise.
		for _label in (
			"str_nonint", "dict", "list",
			"str_int", "bool_true", "bool_false", "float",
		):
			assert w[f"compile.{_label}.workload_schema_unknown"] == 1, (
				f"input labeled {_label!r} should have produced "
				f"workload_schema_unknown=1; full workload: {w!r}"
			)
			# And critically: the counter MUST NOT have been merged
			# under the prefix (silent acceptance of malformed
			# schema is the bug we're guarding against).
			assert f"compile.{_label}.mir.processed.instructions" not in w, (
				f"input labeled {_label!r} silently merged its counter "
				f"despite invalid schema; full workload: {w!r}"
			)

	def test_merge_subprocess_workload_empty_payload_is_noop(self) -> None:
		"""An empty child workload (or one carrying only the
		`workload_schema` meta key) is a no-op -- no merge, no
		markers.  This is the legitimate "child ran but did no
		workload instrumentation" case (e.g. a build phase that
		exited early).  The schema argument is still required even
		on the empty path -- API consistency, not back-compat."""
		_schema = events.EventSink.WORKLOAD_SCHEMA
		sink = events.EventSink()
		# Truly empty.
		sink.merge_subprocess_workload("a", {}, sub_schema=_schema)
		# Only the meta key, no actual counters.
		sink.merge_subprocess_workload(
			"b", {"workload_schema": _schema}, sub_schema=_schema,
		)
		# Even None / malformed schema on an empty payload should
		# stay a clean no-op (the unknown branch only fires for
		# non-empty payloads).
		sink.merge_subprocess_workload("c", {}, sub_schema=None)
		w = sink.timings_summary()["workload"]
		assert w == {}, (
			f"empty child workload must produce no entries; got: {w!r}"
		)

	def test_merge_subprocess_workload_drops_malformed_counter_values(self) -> None:
		"""Counter VALUES must be strict ints (no `bool`, no
		coercible strings/floats) AND non-negative (every v1
		counter is a count or byte size).  Invalid entries drop,
		valid entries merge, and a
		`<prefix>.workload_payload_invalid` marker is published so
		the partial vector isn't indistinguishable from a child
		that legitimately measured fewer dimensions.

		Without the marker, a corrupt child summary like
		`{"source.input.files": "1", "mir.processed.instructions":
		58806}` would publish as a valid-looking vector with one
		dimension silently missing -- the same silent-mislabeling
		failure mode the strict schema check guards against.
		"""
		_schema = events.EventSink.WORKLOAD_SCHEMA
		sink = events.EventSink()
		sink.merge_subprocess_workload(
			"compile",
			{
				# Good counters -- merged.
				"good_int": 12345,
				"zero": 0,  # non-negative boundary: 0 is valid
				# Coercible-but-wrong-type values -- dropped.
				"str_int": "58806",  # JSON-string-of-int
				"bool_true": True,   # bool subclasses int
				"bool_false": False,
				"float": 1.5,
				# Non-numeric -- dropped.
				"str_word": "many",
				"obj": {"nested": 1},
				"arr": [1, 2, 3],
				"none": None,
				# Negative integer -- dropped (every v1 counter is a
				# count or byte size; negative is corruption).
				"negative": -1,
				# Meta key -- filtered before the value check anyway.
				"workload_schema": _schema,
			},
			sub_schema=_schema,
		)
		w = sink.timings_summary()["workload"]
		# Valid ints merged.
		assert w["compile.good_int"] == 12345
		assert w["compile.zero"] == 0
		# Invalid entries dropped -- no `compile.<bad_key>` published.
		for _bad_key in (
			"str_int", "bool_true", "bool_false", "float",
			"str_word", "obj", "arr", "none", "negative",
		):
			assert f"compile.{_bad_key}" not in w, (
				f"malformed counter value for key {_bad_key!r} was "
				f"silently merged under matching schema; full workload: {w!r}"
			)
		# Payload-invalid marker present so consumers can see that
		# the published vector is partial.
		assert w["compile.workload_payload_invalid"] == 1
		# Meta key was filtered, not published as a counter.
		assert "compile.workload_schema" not in w
		# Schema itself was valid -- no schema-level markers.
		assert "compile.workload_schema_mismatch" not in w
		assert "compile.workload_schema_unknown" not in w

	def test_merge_subprocess_workload_payload_invalid_marker_accumulates(self) -> None:
		"""Two merges under the same prefix that each contain an
		invalid counter accumulate the marker (=2) -- additive,
		mirroring the merge model for valid counters.  Lets
		operators detect how many merge rounds were degraded."""
		_schema = events.EventSink.WORKLOAD_SCHEMA
		sink = events.EventSink()
		# Two corrupt children under the same prefix.
		for _ in range(2):
			sink.merge_subprocess_workload(
				"compile",
				{"good": 1, "bad": "not-an-int"},
				sub_schema=_schema,
			)
		w = sink.timings_summary()["workload"]
		# Valid counter accumulated as usual.
		assert w["compile.good"] == 2
		# Marker accumulated.
		assert w["compile.workload_payload_invalid"] == 2

	def test_merge_subprocess_workload_clean_payload_no_marker(self) -> None:
		"""A payload with ONLY valid counters under a matching schema
		merges with no marker -- the absence of the marker is the
		consumer's signal that every published key is trustworthy."""
		_schema = events.EventSink.WORKLOAD_SCHEMA
		sink = events.EventSink()
		sink.merge_subprocess_workload(
			"compile",
			{"a": 10, "b": 0, "c": 999},
			sub_schema=_schema,
		)
		w = sink.timings_summary()["workload"]
		assert w == {"compile.a": 10, "compile.b": 0, "compile.c": 999}
		assert "compile.workload_payload_invalid" not in w

	def test_merge_subprocess_workload_requires_sub_schema_argument(self) -> None:
		"""`sub_schema` is keyword-only with no default value.  A
		caller that forgets to pass it must hit a TypeError at the
		call site, not silently produce unversioned data.

		Pins the API-design contract that there is no back-compat
		path for unversioned workload data."""
		sink = events.EventSink()
		with pytest.raises(TypeError):
			sink.merge_subprocess_workload(  # type: ignore[call-arg]
				"compile",
				{"mir.processed.instructions": 7},
			)


class TestDriftcWorkloadCli:
	"""End-to-end pins for the driftc workload payload under
	`--json --timing` and `--timing` text mode."""

	def test_json_workload_present_and_well_formed(
		self, trivial_source: Path,
	) -> None:
		"""`--json --timing` must include `workload_schema` (int) and
		`workload` (dict of str -> int).  Source counters fire on
		every compile (trivial source still parses), so we can pin
		their presence + integer type."""
		res = _run_driftc(_common_driftc_args(trivial_source) + ["--json", "--timing"])
		assert res.returncode == 0, f"compile failed: {res.stderr!r}"
		t = json.loads(res.stdout)["timings"]
		assert isinstance(t.get("workload_schema"), int)
		assert isinstance(t.get("workload"), dict)
		w = t["workload"]
		# Source counters always fire (the parse path runs even when
		# codegen does not).
		for k in (
			"source.input.files",
			"source.input.utf8_bytes",
			"source.input.parse_tree_tokens",
		):
			assert k in w, f"missing workload key {k}; got {sorted(w)!r}"
			assert isinstance(w[k], int), (
				f"workload[{k}] must be int, got {type(w[k]).__name__}"
			)
			assert w[k] >= 0
		# Trivial source = 1 user file.
		assert w["source.input.files"] == 1

	def test_json_no_timing_omits_workload(
		self, trivial_source: Path,
	) -> None:
		"""Without `--timing`, no sink is installed, so workload
		instrumentation is a no-op and the JSON carries neither
		`workload` nor `workload_schema`."""
		res = _run_driftc(_common_driftc_args(trivial_source) + ["--json"])
		assert res.returncode == 0
		payload = json.loads(res.stdout)
		assert "timings" not in payload
		# No `workload`/`workload_schema` at the top level either.
		assert "workload" not in payload
		assert "workload_schema" not in payload

	def test_text_mode_workload_lines_well_formed(
		self, trivial_source: Path,
	) -> None:
		"""`--timing` (no `--json`) prints `[drift:workload]` lines on
		stderr after the `[drift:timing]` block.  Pin the
		alphabetical-sort + integer-value format so CI greps don't
		break on accidental reorder."""
		import re as _re
		res = _run_driftc(_common_driftc_args(trivial_source) + ["--timing"])
		assert res.returncode == 0, f"compile failed: {res.stderr!r}"
		_header_re = _re.compile(r"^\[drift:workload\] workload_schema=\d+$")
		_kv_re = _re.compile(r"^\[drift:workload\]   [\w.]+=\d+$")
		header_seen = False
		kv_lines: list[str] = []
		for ln in res.stderr.splitlines():
			if ln.startswith("[drift:workload] workload_schema="):
				assert _header_re.match(ln), (
					f"workload header line {ln!r} doesn't match expected shape"
				)
				header_seen = True
				continue
			if ln.startswith("[drift:workload]   "):
				assert _kv_re.match(ln), (
					f"workload kv line {ln!r} doesn't match `key=int` shape"
				)
				kv_lines.append(ln)
		assert header_seen, (
			f"missing `[drift:workload] workload_schema=` header on stderr; "
			f"got: {res.stderr!r}"
		)
		# Source counters always fire -- at least 3 kv lines expected.
		assert len(kv_lines) >= 3, (
			f"expected >= 3 workload kv lines; got {len(kv_lines)}. "
			f"stderr: {res.stderr!r}"
		)
		# Alphabetical order pin (so dashboards/CI greps stay stable).
		keys = [ln.split()[1].split("=", 1)[0] for ln in kv_lines]
		assert keys == sorted(keys), (
			f"workload kv lines not sorted alphabetically; got: {keys!r}"
		)

	def test_codegen_path_emits_mir_and_llvm_workload(
		self, tmp_path: Path,
	) -> None:
		"""A compile that runs codegen (`--emit-ir`, no
		`--test-build-only`) must emit nonzero
		`mir.processed.instructions`, `codegen.input_mir.instructions`,
		and `llvm.ir.utf8_bytes` -- pins that the MIR-pass /
		codegen-input / IR-render capture points are all wired into
		the same sink the driver reads back."""
		src = tmp_path / "main.drift"
		# Entry function named `main` so the default `--entry main`
		# resolution finds it.  `--emit-ir` routes through
		# `_emit_codegen` / `module.render()` -- the sites that record
		# codegen + IR workload.
		src.write_text(
			"module main;\n\npub fn main() nothrow -> Int {\n\treturn 0;\n}\n",
			encoding="utf-8",
		)
		ir_out = tmp_path / "out.ll"
		res = _run_driftc([
			str(src),
			"--stdlib-root", "stdlib",
			"--target-word-bits", "64",
			"--emit-ir", str(ir_out),
			"--json", "--timing",
		])
		assert res.returncode == 0, f"compile failed: {res.stderr!r}"
		w = json.loads(res.stdout)["timings"]["workload"]
		for k in (
			"mir.processed.functions",
			"mir.processed.blocks",
			"mir.processed.instructions",
			"codegen.input_mir.functions",
			"codegen.input_mir.blocks",
			"codegen.input_mir.instructions",
			"llvm.ir.utf8_bytes",
			"generics.instances_emitted",
		):
			assert k in w, (
				f"missing workload key {k} after a codegen-emitting "
				f"compile; got: {sorted(w)!r}"
			)
			assert w[k] > 0, (
				f"workload[{k}] = {w[k]}, expected > 0 after a "
				f"codegen-emitting compile"
			)

	def test_user_source_token_count_scales_independently_of_stdlib(
		self, tmp_path: Path,
	) -> None:
		"""Doubling user source tokens must roughly double
		`source.input.parse_tree_tokens` WITHOUT folding in stdlib
		counts (the split is the load-bearing reason for the user/
		stdlib classification -- a single conflated number would
		mask the user-side delta against a large stdlib backdrop)."""
		small = tmp_path / "small.drift"
		small.write_text(
			"module main;\n\npub fn main() nothrow -> Int {\n\treturn 0;\n}\n",
			encoding="utf-8",
		)
		# Add several extra trivial functions to grow the token count.
		big = tmp_path / "big.drift"
		extra = "\n".join(
			f"pub fn f{i}(x: Int) nothrow -> Int {{\n\treturn x;\n}}"
			for i in range(20)
		)
		big.write_text(
			f"module main;\n\npub fn main() nothrow -> Int {{\n\treturn 0;\n}}\n\n{extra}\n",
			encoding="utf-8",
		)

		def _workload(src: Path) -> dict:
			res = _run_driftc(_common_driftc_args(src) + ["--json", "--timing"])
			assert res.returncode == 0, f"compile failed: {res.stderr!r}"
			return json.loads(res.stdout)["timings"]["workload"]

		w_small = _workload(small)
		w_big = _workload(big)
		# User-side tokens grow strictly.
		assert w_big["source.input.parse_tree_tokens"] > w_small["source.input.parse_tree_tokens"], (
			f"expected big user source to have more parse_tree_tokens; "
			f"small={w_small['source.input.parse_tree_tokens']}, "
			f"big={w_big['source.input.parse_tree_tokens']}"
		)
		# Stdlib token count is stable across the two compiles
		# (same stdlib_root, same set of stdlib files); pin that the
		# user delta does NOT leak into the stdlib counter.
		assert (
			w_small["source.implicit_stdlib.parse_tree_tokens"]
			== w_big["source.implicit_stdlib.parse_tree_tokens"]
		), (
			"stdlib token count drifted between two user-source-only "
			"changes -- the classification is folding user tokens into "
			"the stdlib bucket.  "
			f"small_stdlib={w_small['source.implicit_stdlib.parse_tree_tokens']}, "
			f"big_stdlib={w_big['source.implicit_stdlib.parse_tree_tokens']}"
		)
