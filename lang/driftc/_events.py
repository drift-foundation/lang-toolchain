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
    # summary == {"total_wall":      float,
    #             "phases":          {label: seconds, ...},
    #             "counts":          {label: invocations, ...},
    #             "workload_schema": int,
    #             "workload":        {key: int, ...}}

Workload counters (see `set_workload` / `add_workload`) pair with
phase timings so two compiles can be compared on both elapsed time
and "amount of compiler work attempted."  See `doc/timing.md` for
the v1 key inventory + units.

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
		"_workload",
		"_stack",
		"_wall_start",
		"_wall_end",
		"_stream_writer",
	)

	# Bumped when the `workload` key set or semantics change in a way
	# that breaks downstream readers (renamed keys, redefined units,
	# removed keys).  Additive new keys do NOT bump the schema.
	WORKLOAD_SCHEMA = 1

	def __init__(self, *, stream_writer: Callable[[dict], None] | None = None) -> None:
		self._phases: dict[str, float] = {}
		# Sibling map: how many times each label fired (incremented per
		# phase_start).  Lets `timings_summary()` answer "one slow call
		# vs many small calls" -- e.g. `normalize_hir count=500`
		# signals per-function overhead vs `trust_verify_loop count=1`
		# pointing at one large pass.
		self._counts: dict[str, int] = {}
		# Workload counters: machine-neutral compilation-shape and
		# processed-work metrics that pair with `_phases` to let two
		# compiles be compared by both elapsed time and "amount of
		# compiler work attempted."  Two write modes coexist:
		#   * set_workload(key, v)  -- snapshot of compilation-unit
		#     or final-artifact shape (source bytes, parsed module
		#     count, final rendered IR bytes).  Last writer wins.
		#   * add_workload(key, d)  -- processed work whose phase
		#     time also accumulates (generics emitted, MIR
		#     instructions produced by each compile_stubbed_funcs
		#     invocation, codegen MIR input per lowering call).  If
		#     the work runs twice its phase time accumulates twice;
		#     the workload denominator must too, so elapsed-per-unit
		#     stays comparable across retries.
		# See `doc/timing.md` for the full key inventory + units.
		self._workload: dict[str, int] = {}
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

	def set_workload(self, key: str, value: int) -> None:
		"""Snapshot a compilation-shape or final-artifact value.
		Last writer wins -- subsequent set_workload(key, ...) overwrites
		any prior set OR add for the same key.

		Use for: input source bytes/files, parsed module/function
		counts, final rendered IR bytes -- quantities that describe the
		compilation unit or artifact once, regardless of whether a phase
		ran more than once.

		Value contract: strict `int` (not `bool`, not `"1"`, not
		`1.5`), `>= 0`.  Every v1 workload counter is a count or a
		byte size; neither type-coerced values nor negatives are
		legitimate.  Invalid inputs are SILENTLY DROPPED -- the
		producer is in-process compiler code, so a violating call is
		a caller bug that should be caught by tests, not surfaced
		as a runtime marker.  Symmetric with the strict validation
		applied to cross-process merge inputs in
		`merge_subprocess_workload`.

		See `add_workload` for processed-work counters whose phase time
		also accumulates across repeated invocations.
		"""
		if not isinstance(value, int) or isinstance(value, bool) or value < 0:
			return
		self._workload[key] = value

	def add_workload(self, key: str, delta: int) -> None:
		"""Accumulate a processed-work value.

		Use for counters that pair with a phase whose elapsed time
		ALSO accumulates across repeated invocations: generics emitted
		per `generic_instantiation` exit, MIR instructions per
		`compile_stubbed_funcs` completion, codegen MIR input per
		`codegen.lower` call.  Two runs of the same phase double both
		the time and the denominator, keeping per-unit cost
		comparable.

		Delta contract: strict `int` (not `bool`, not `"1"`, not
		`1.5`), `>= 0`.  Same rule as `set_workload`; same
		silent-drop behavior on violation.  A negative delta would
		violate the "every v1 counter is a count or byte size"
		invariant; we deliberately do not provide a subtract
		operation.

		See `set_workload` for compilation-shape snapshots whose value
		describes the unit once and should not double.
		"""
		if not isinstance(delta, int) or isinstance(delta, bool) or delta < 0:
			return
		self._workload[key] = self._workload.get(key, 0) + delta

	def merge_subprocess_workload(
		self,
		prefix: str,
		sub_workload: dict[str, Any],
		*,
		sub_schema: int | None,
	) -> None:
		"""Merge a child compile's workload dict into this sink under a
		prefix, additively.

		Same model as `merge_subprocess_timings`: each child key
		`<k>` becomes `<prefix>.<k>` in this sink's workload dict;
		repeated merges with the same prefix accumulate.  Adding under
		the same prefix is correct when timing also accumulates -- a
		retried child compile under the same prefix contributes both
		its phase time and its processed-work counters twice.

		`sub_schema` is REQUIRED (keyword-only, no default).  Every
		producer in this toolchain stamps `workload_schema` on its
		summary; we do not carry unversioned workload data forward.

		Schema disposition:

		* `sub_schema` is a valid int matching `self.WORKLOAD_SCHEMA`
		  → keys merge under `<prefix>.<key>`.
		* `sub_schema` is a positive int (`>= 1`) that does NOT
		  match → merge is REFUSED; a
		  `<prefix>.workload_schema_mismatch` marker is recorded
		  with the child's schema value so operators can identify
		  which toolchain produced the dropped data.
		* `sub_schema` is None, is not a strict `int` (string,
		  bool, float, dict, list -- including coercible values like
		  `"1"`, `True`, `1.5`), or is an int `< 1` (zero, negative
		  -- not a meaningful schema since v1 is the first) AND the
		  child workload is non-empty → merge is REFUSED; a
		  `<prefix>.workload_schema_unknown` marker is recorded
		  (value=1).  This is a defensive guard for the corrupt-
		  `--timing-out`-JSON failure mode, not a back-compat path
		  -- in normal operation every producer stamps a valid
		  positive integer.
		* Empty child workload (including a dict carrying only the
		  meta `workload_schema` key with no counters) → no-op, no
		  markers.
		* Schema matches but the payload contains one or more
		  invalid counter values (wrong type, negative) → the
		  invalid entries are dropped, the valid entries merge as
		  usual, and a `<prefix>.workload_payload_invalid` marker
		  is added (accumulates additively, so retries of a corrupt
		  child show `=N`).  See the counter-value contract below.

		Counter VALUES are validated to the same strictness as the
		schema:

		* Must be a strict `int` (not `bool`, not `"1"`, not `1.5`).
		* Must be `>= 0` -- every v1 counter is a count or byte
		  size; a negative value is corruption, not a legitimate
		  delta.

		Invalid counters are dropped, AND a
		`<prefix>.workload_payload_invalid` marker is published
		(value accumulates additively across multiple merges under
		the same prefix, so a wrapper that retried a corrupt child
		twice shows `=2`).  This is symmetric with the schema-level
		markers: a consumer reading a workload vector with no marker
		knows every key reflects a counter the producer emitted; a
		marker says "this prefix dropped data, treat the partial
		vector as suspect."  Without this, a child summary like
		`{"source.input.files": "1", "mir.processed.instructions":
		58806}` would publish under a valid schema with one
		dimension silently missing, indistinguishable from a child
		that legitimately measured only the other dimension.

		Valid counters merge normally even when other counters in
		the same payload are invalid -- the partial data is still
		useful, and the marker lets the consumer detect that it IS
		partial.

		The meta `workload_schema` key is filtered out before merge
		(the schema lives on the parent sink, not on prefixed
		entries).
		"""
		if not isinstance(sub_workload, dict):
			return
		# Drop the meta key before deciding emptiness so an entry that
		# carries only `workload_schema` doesn't trigger the
		# unknown-schema marker.
		_payload = {
			k: v for k, v in sub_workload.items() if k != "workload_schema"
		}
		if not _payload:
			return
		# Strict type check -- only an actual positive JSON integer
		# counts as a valid schema.  `int(...)` would silently
		# coerce True → 1, "1" → 1, 1.5 → 1, all of which would
		# mislabel non-conforming producers as schema 1.  `bool`
		# subclasses `int` in Python, so the explicit
		# `not isinstance(..., bool)` guard is needed to reject
		# True/False.
		#
		# Schemas start at 1 and we don't support a pre-v1 or
		# negative schema range; any int < 1 is corruption, not a
		# meaningful alternate schema.  Routing those to
		# `workload_schema_unknown` (not `workload_schema_mismatch`)
		# also prevents a negative `mismatch=<value>` from leaking
		# into the workload output despite the non-negative-value
		# rule applied to every other counter.
		_schema_int: int | None
		if (
			isinstance(sub_schema, int)
			and not isinstance(sub_schema, bool)
			and sub_schema >= 1
		):
			_schema_int = sub_schema
		else:
			_schema_int = None
		if _schema_int is None:
			# Non-empty payload + missing/invalid/non-positive schema
			# = unversioned or corrupt data.  Don't label it as v1.
			self._workload[f"{prefix}.workload_schema_unknown"] = 1
			return
		if _schema_int != self.WORKLOAD_SCHEMA:
			# Refuse merge: child uses a different workload semantic.
			# Value is the child's schema so operators can read which
			# version produced the dropped data.
			self._workload[f"{prefix}.workload_schema_mismatch"] = _schema_int
			return
		_invalid_counters = 0
		for k, v in _payload.items():
			# Strict-int + non-negative check for the counter value.
			# `int(v)` coercion would accept `"58806"`, True/False,
			# and `1.5`, silently relabeling malformed payload
			# values as valid v1 ints.  Negatives are corruption:
			# every v1 counter is a count or byte size, neither
			# can be negative.  Bad entries drop, and a single
			# `<prefix>.workload_payload_invalid` marker (counted
			# below) tells the consumer the partial vector
			# published here had some dimensions lost -- without
			# the marker, the result would be indistinguishable
			# from a child that legitimately measured fewer
			# dimensions, masking corruption.
			if (
				not isinstance(v, int)
				or isinstance(v, bool)
				or v < 0
			):
				_invalid_counters += 1
				continue
			full_key = f"{prefix}.{k}"
			self._workload[full_key] = self._workload.get(full_key, 0) + v
		if _invalid_counters > 0:
			# Additive: a wrapper that retried a corrupt child twice
			# shows `=2`.  Mirrors the additive-merge model for
			# valid counters; lets operators see how many merge
			# rounds were degraded.  Counts MERGE EVENTS that had
			# at least one bad counter, not individual bad
			# counters within an event -- the latter would expose
			# implementation detail (how many fields the producer
			# emits) without giving the consumer anything more
			# actionable.
			_inv_key = f"{prefix}.workload_payload_invalid"
			self._workload[_inv_key] = self._workload.get(_inv_key, 0) + 1

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
		      "total_wall":      float,
		      "phases":          {label: seconds, ...},
		      "counts":          {label: invocations, ...},
		      "workload_schema": int,
		      "workload":        {key: int, ...},
		    }

		`counts` carries one entry per `phases` key: how many times
		`phase_start(label)` fired during the compile.  Lets readers
		spot per-call overhead vs single-large-call cost without
		re-instrumenting (`smoke.compile count=2` is the canonical
		retry-detection signal).

		`workload` is the machine-neutral compilation-shape and
		processed-work vector (see `set_workload` / `add_workload`).
		Keys are additive under `workload_schema = 1`; removal or
		semantic redefinition of a key bumps the schema.  Empty dict
		on a compile that did no workload instrumentation -- consumers
		must handle missing keys, not assume presence.

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
			"workload_schema": self.WORKLOAD_SCHEMA,
			"workload": dict(self._workload),
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


def set_workload(key: str, value: int) -> None:
	"""Module-level shortcut for `EventSink.set_workload`.

	Cheap no-op when no sink is installed: one `ContextVar.get()`
	returning `None`, then return -- same shape as `phase_start` /
	`phase_end`.  Compiler instrumentation sites should call this
	unconditionally; the `--timing` gate happens once at the driver
	entry by installing (or not installing) a sink.
	"""
	sink = _CURRENT_SINK.get()
	if sink is not None:
		sink.set_workload(key, value)


def add_workload(key: str, delta: int) -> None:
	"""Module-level shortcut for `EventSink.add_workload`.

	Cheap no-op when no sink is installed: one `ContextVar.get()`
	returning `None`, then return -- same shape as `phase_start` /
	`phase_end`.
	"""
	sink = _CURRENT_SINK.get()
	if sink is not None:
		sink.add_workload(key, delta)
