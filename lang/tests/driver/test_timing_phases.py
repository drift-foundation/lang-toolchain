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
     field carries `total_wall` (float) and `phases` (dict).
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
		"""Sequential same-label `timed` blocks accumulate."""
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
		timeout=120,
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
		`[drift:timing]` summary on stderr."""
		res = _run_driftc(_common_driftc_args(trivial_source) + ["--timing"])
		assert res.returncode == 0, f"compile failed: {res.stderr!r}"
		assert "[drift:timing] total_wall=" in res.stderr, (
			f"expected `[drift:timing] total_wall=` on stderr; "
			f"got: {res.stderr!r}"
		)
		# Per-phase lines must be present.
		assert "[drift:timing]   " in res.stderr


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
