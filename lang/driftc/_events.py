# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Scoped in-process compiler telemetry sink.

Single instrumentation path everywhere in the compiler:

    from lang.driftc import _events as events
    ...
    with events.timed("typecheck"):
        ...

Driver-side, the sink is installed for the duration of one compile and
queried for its summary at the end:

    sink = events.EventSink()
    with events.install_sink(sink):
        sink.begin_compile()
        try:
            ...do the compile...
        finally:
            sink.end_compile()
    summary = sink.timings_summary()
    # summary == {"total_wall": float,
    #             "phases": {label: seconds, ...},
    #             "counts": {label: invocations, ...}}

Design constraints (deliberate):

  * No JSON knowledge inside compiler phases -- they only call
    `events.timed(label)`.
  * No stderr-parsing for tooling -- callers read structured data via
    `EventSink.timings_summary()`.
  * Cheap when disabled -- with no sink installed, `events.timed` is
    one `ContextVar.get()` call that returns `None`, after which the
    function returns the module-level `_NOOP_TIMED` singleton context
    manager.  No allocations per call.
  * No broad global mutable state -- the sink lives in a `ContextVar`
    scoped to the active `install_sink` block.  Two in-process compiles
    (sequential in one worker, or in parallel xdist subprocess workers)
    each own their own sink and cannot contaminate each other.
  * Monotonic clock only (`time.monotonic`).  No wall-clock.
  * Deliberately small.  Not a pub/sub framework: one writer (the
    driver), one optional streamer (`stream_writer=` for future
    `--json-lines`), no subscribers.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator


_CURRENT_SINK: ContextVar["EventSink | None"] = ContextVar(
	"_drift_event_sink", default=None,
)


class EventSink:
	"""Records compile-phase timing events for one compile.

	Each `phase_start`/`phase_end` pair contributes to a per-label sum.
	Nested phases sum to their own labels; the per-label sums are
	informational and may exceed `total_wall` (overlap with their
	parent).  Only `total_wall` -- recorded once via
	`begin_compile`/`end_compile` at the outermost compile boundary --
	is the load-bearing "did this get faster" number.

	Optional `stream_writer` argument: a callable invoked once per
	event (`phase_start`/`phase_end`) with a dict payload.  Used by
	the future `--json-lines` output mode to feed progressive events
	to tools without the compiler phases knowing.  When `None`, no
	streaming happens.
	"""

	__slots__ = (
		"_phases",
		"_counts",
		"_stack",
		"_wall_start",
		"_wall_end",
		"_stream_writer",
	)

	def __init__(self, *, stream_writer: Callable[[dict], None] | None = None) -> None:
		self._phases: dict[str, float] = {}
		# Sibling map: how many times each label fired (incremented per
		# phase_start).  Lets `timings_summary()` answer "one slow call
		# vs many small calls" -- e.g. `normalize_hir count=500`
		# signals per-function overhead vs `trust_verify_loop count=1`
		# pointing at one large pass.
		self._counts: dict[str, int] = {}
		# Stack of (label, monotonic_start) so nested phases unwind correctly
		# even when the same label nests (rare, but defended against).
		self._stack: list[tuple[str, float]] = []
		self._wall_start: float | None = None
		self._wall_end: float | None = None
		self._stream_writer = stream_writer

	def begin_compile(self) -> None:
		"""Mark the outer compile boundary.  Idempotent within a sink
		(second call replaces the start); callers should only call once
		per sink lifetime."""
		self._wall_start = time.monotonic()

	def end_compile(self) -> None:
		"""Mark the end of the compile boundary.  Idempotent within a
		sink (second call replaces the end)."""
		self._wall_end = time.monotonic()

	def phase_start(self, label: str) -> None:
		t = time.monotonic()
		self._stack.append((label, t))
		self._counts[label] = self._counts.get(label, 0) + 1
		if self._stream_writer is not None:
			self._stream_writer({"event": "phase_start", "phase": label})

	def phase_end(self, label: str) -> None:
		t = time.monotonic()
		if not self._stack or self._stack[-1][0] != label:
			# Defensive: mismatched end (e.g., exception unwind that
			# skipped a level).  Silently no-op; the elapsed for the
			# enclosing scope is still captured when it unwinds.
			return
		_, t0 = self._stack.pop()
		elapsed = t - t0
		self._phases[label] = self._phases.get(label, 0.0) + elapsed
		if self._stream_writer is not None:
			self._stream_writer(
				{"event": "phase_end", "phase": label, "seconds": elapsed}
			)

	def merge_subprocess_timings(self, prefix: str, sub_timings: dict[str, Any]) -> None:
		"""Merge a child compile's structured timings into this sink
		under a label prefix.

		`sub_timings` is the dict shape `EventSink.timings_summary()`
		produces -- `{"total_wall": float, "phases": {label: secs},
		"counts": {label: invocations}}` -- typically read back from a
		child driftc invocation's `--timing-out <path>` JSON file.

		Each child phase `<label>` becomes `<prefix>.<label>` in this
		sink's `phases` dict; the child's `total_wall` becomes
		`<prefix>.total_wall` so the wrapper summary surfaces the
		child's authoritative wall time as a distinct row.  Repeated
		merges with the same prefix accumulate additively (e.g.
		multiple driftc invocations under "smoke.compile" stack).

		Cross-process aggregation only -- inside one process the
		`ContextVar` already carries the sink and direct
		`events.timed(...)` calls are the right path.
		"""
		phases = sub_timings.get("phases") or {}
		if isinstance(phases, dict):
			for k, v in phases.items():
				try:
					_v = float(v)
				except (TypeError, ValueError):
					continue
				full_key = f"{prefix}.{k}"
				self._phases[full_key] = self._phases.get(full_key, 0.0) + _v
		# Counts merge additively under the same prefix.  If the child
		# omits `counts` (older driftc) we default each merged phase to
		# count=1 so the parent sees at least one invocation per
		# observed phase.
		counts = sub_timings.get("counts")
		if isinstance(counts, dict):
			for k, c in counts.items():
				try:
					_c = int(c)
				except (TypeError, ValueError):
					continue
				full_key = f"{prefix}.{k}"
				self._counts[full_key] = self._counts.get(full_key, 0) + _c
		elif isinstance(phases, dict):
			for k in phases.keys():
				full_key = f"{prefix}.{k}"
				self._counts[full_key] = self._counts.get(full_key, 0) + 1
		tw = sub_timings.get("total_wall")
		if tw is not None:
			try:
				_tw = float(tw)
			except (TypeError, ValueError):
				_tw = None
			if _tw is not None:
				tw_key = f"{prefix}.total_wall"
				self._phases[tw_key] = self._phases.get(tw_key, 0.0) + _tw
				self._counts[tw_key] = self._counts.get(tw_key, 0) + 1

	def close_all_open_phases(self) -> None:
		"""Pop every still-open phase off the stack, recording its
		elapsed time as of *now*.

		Called by the driver at error-emit boundaries
		(`_emit_compile_json` in driftc.py) and right before the
		text-mode timing summary is printed.  Lets manual
		`phase_start`/`phase_end` sites (`trust_pre_pass`,
		`trust_verify_loop`, `emit_package`) capture their elapsed
		time even when an early `return 1` skips the matching
		`phase_end` -- so the `timings.phases` dict in an error
		payload reflects which phase was running when the compile
		bailed.

		Phases that closed cleanly are unaffected (they've already
		left the stack).  Subsequent `phase_end(label)` calls for
		those (now-closed) phases are no-ops via the defensive
		mismatch guard.
		"""
		now = time.monotonic()
		# Drain bottom-up: the stack is LIFO, so iterating .pop()
		# closes the innermost-still-open phase first.
		while self._stack:
			label, t0 = self._stack.pop()
			elapsed = now - t0
			self._phases[label] = self._phases.get(label, 0.0) + elapsed
			if self._stream_writer is not None:
				self._stream_writer(
					{"event": "phase_end", "phase": label, "seconds": elapsed}
				)

	def timings_summary(self) -> dict[str, Any]:
		"""Return the compile's timing summary.

		Shape:
		    {
		      "total_wall": float,
		      "phases":     {label: seconds, ...},
		      "counts":     {label: invocations, ...},
		    }

		`counts` carries one entry per `phases` key: how many times
		`phase_start(label)` fired during the compile.  Lets readers
		spot per-call overhead vs single-large-call cost without
		re-instrumenting (`smoke.compile count=2` is the canonical
		retry-detection signal).

		`total_wall` is 0.0 if `begin_compile`/`end_compile` weren't
		both called (a caller-side bug -- the driver entry points
		ensure this in production).
		"""
		if self._wall_start is not None and self._wall_end is not None:
			total_wall = self._wall_end - self._wall_start
		else:
			total_wall = 0.0
		return {
			"total_wall": total_wall,
			"phases": dict(self._phases),
			"counts": dict(self._counts),
		}


def current_sink() -> EventSink | None:
	"""Return the sink installed for the active compile, or None."""
	return _CURRENT_SINK.get()


@contextmanager
def install_sink(sink: EventSink) -> Iterator[EventSink]:
	"""Install `sink` as the active sink for the duration of this
	context.  Resets cleanly on every exit path (normal return,
	exception, sys.exit) via the `ContextVar.reset` token.
	"""
	tok = _CURRENT_SINK.set(sink)
	try:
		yield sink
	finally:
		_CURRENT_SINK.reset(tok)


class _NoopTimedCM:
	"""Module-level singleton context manager returned by `timed()` on
	the cheap path (no sink installed).  No per-call allocations:
	`timed(label)` looks up the ContextVar, finds `None`, and returns
	this shared instance.
	"""

	__slots__ = ()

	def __enter__(self) -> None:
		return None

	def __exit__(self, exc_type, exc, tb) -> bool:
		return False


_NOOP_TIMED = _NoopTimedCM()


class _ActiveTimedCM:
	"""Per-call context manager for the active path (sink installed).
	Holds the (sink, label) pair across enter/exit; one small instance
	per `timed(label)` call.  Cheaper than a `@contextmanager`
	generator and free of the `_GeneratorContextManager` wrapping
	cost.
	"""

	__slots__ = ("_sink", "_label")

	def __init__(self, sink: "EventSink", label: str) -> None:
		self._sink = sink
		self._label = label

	def __enter__(self) -> None:
		self._sink.phase_start(self._label)
		return None

	def __exit__(self, exc_type, exc, tb) -> bool:
		self._sink.phase_end(self._label)
		return False


def timed(label: str):
	"""Return a context manager that records a phase event into the
	active sink, if one is installed.

	Cheap path (no sink installed): returns a module-level singleton
	no-op.  Zero allocations, no clock reads -- a single
	`ContextVar.get()` followed by an attribute return.

	Active path: returns a small `_ActiveTimedCM` instance that
	delegates to `sink.phase_start` / `sink.phase_end`.
	"""
	sink = _CURRENT_SINK.get()
	if sink is None:
		return _NOOP_TIMED
	return _ActiveTimedCM(sink, label)


def phase_start(label: str) -> None:
	"""Module-level shortcut for callers that can't use the `timed`
	context manager (e.g., wrapping a long block where reindenting
	would be noisy and risky -- pair with `phase_end(label)` in a
	`finally`).  Cheap no-op when no sink is installed.
	"""
	sink = _CURRENT_SINK.get()
	if sink is not None:
		sink.phase_start(label)


def phase_end(label: str) -> None:
	"""See `phase_start`.  Cheap no-op when no sink is installed."""
	sink = _CURRENT_SINK.get()
	if sink is not None:
		sink.phase_end(label)
