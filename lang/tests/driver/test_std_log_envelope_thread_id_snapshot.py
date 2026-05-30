# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression (0.27.203): `std.log` must capture caller-thread IDs
(`thread.vt_id()` / `thread.kernel_thread_id()`) at emit time, before
handing the record off to the async drain path.  Previously these
were computed inside `_emit_envelope_handle`, meaning the final log
line carried the drain worker's IDs — unusable for request-flow
debugging, because downstream log-analytics code assumes `vtid`/`tid`
identify the event *origin*, not the thread that happened to dequeue
it.

Forcing drain to run on a different VT than emit deterministically
(to catch the bug via end-to-end log-line inspection) would need
either a held drain-lock API we don't expose or a stress scenario
with heavy contention — neither is a clean regression.  Instead this
test pins the **snapshot point** structurally by inspecting the
stdlib source:

  1. `LogEnvelope` carries `vtid: Int` and `tid: Int` fields.
  2. `_emit_payload_throwing` calls `thread.vt_id()` and
     `thread.kernel_thread_id()` and passes them to `_alloc_envelope`
     BEFORE enqueue.
  3. `_emit_envelope_handle` does NOT call `thread.vt_id()` or
     `thread.kernel_thread_id()` — it reads the pre-captured values
     from the envelope.

If any of those three invariants is violated (e.g. someone moves
the thread-ID probe back into `_emit_envelope_handle`, or forgets
to pass them into `_alloc_envelope`, or reverts the envelope
schema), this test fails immediately.
"""
from __future__ import annotations

import re
from pathlib import Path


_STDLIB_LOG = Path(__file__).resolve().parents[3] / "stdlib" / "std" / "log" / "log.drift"


def _read_log_drift() -> str:
	text = _STDLIB_LOG.read_text(encoding="utf-8")
	assert text, f"{_STDLIB_LOG} is empty"
	return text


def _extract_fn_body(text: str, name: str) -> str:
	# Match `fn <name>(...)` through the matching closing brace at
	# the same indentation.  stdlib uses tabs at the top level, so a
	# top-level `}` on a fresh line ends the function body.
	pattern = rf"\nfn {re.escape(name)}\(.*?\n\}}\n"
	m = re.search(pattern, text, re.DOTALL)
	assert m, f"could not locate fn {name}(...) in {_STDLIB_LOG}"
	return m.group(0)


def test_log_envelope_carries_vtid_and_tid_fields() -> None:
	"""LogEnvelope struct must carry `vtid: Int` and `tid: Int`
	fields — the per-emit thread-ID snapshot storage slots."""
	text = _read_log_drift()
	m = re.search(r"\nstruct LogEnvelope \{(.*?)\n\}\n", text, re.DOTALL)
	assert m, "could not locate `struct LogEnvelope { ... }`"
	body = m.group(1)
	assert re.search(r"\bvtid\s*:\s*Int\b", body), (
		f"LogEnvelope is missing `vtid: Int` field (needed to snapshot "
		f"caller's virtual-thread ID at emit time):\n{body}"
	)
	assert re.search(r"\btid\s*:\s*Int\b", body), (
		f"LogEnvelope is missing `tid: Int` field (needed to snapshot "
		f"caller's POSIX thread ID at emit time):\n{body}"
	)


def test_emit_payload_throwing_captures_thread_ids_before_enqueue() -> None:
	"""`_emit_payload_throwing` must capture `thread.vt_id()` and
	`thread.kernel_thread_id()` synchronously and pass them to
	`_alloc_envelope` — this is the only place where the caller's
	thread identity is still on-stack, before the record is handed
	to the async drain path."""
	text = _read_log_drift()
	body = _extract_fn_body(text, "_emit_payload_throwing")
	assert "thread.vt_id()" in body, (
		f"_emit_payload_throwing must call thread.vt_id() to capture the "
		f"emit-time virtual-thread ID:\n{body}"
	)
	assert "thread.kernel_thread_id()" in body, (
		f"_emit_payload_throwing must call thread.kernel_thread_id() to "
		f"capture the emit-time OS thread ID:\n{body}"
	)
	# Both must appear before the _alloc_envelope call (i.e. the
	# values are threaded through the allocator, not fetched later).
	vtid_pos = body.index("thread.vt_id()")
	tid_pos = body.index("thread.kernel_thread_id()")
	alloc_pos = body.index("_alloc_envelope(")
	assert vtid_pos < alloc_pos, (
		"thread.vt_id() must be called BEFORE _alloc_envelope "
		"(snapshot-at-emit, not after enqueue)"
	)
	assert tid_pos < alloc_pos, (
		"thread.kernel_thread_id() must be called BEFORE _alloc_envelope "
		"(snapshot-at-emit, not after enqueue)"
	)


def test_emit_envelope_handle_does_not_probe_thread_ids() -> None:
	"""`_emit_envelope_handle` runs on the drain worker — if it
	probes thread IDs itself, those are the drain-worker's IDs, not
	the emitter's.  The function must instead read `env.vtid` /
	`env.tid` from the stored envelope."""
	text = _read_log_drift()
	body = _extract_fn_body(text, "_emit_envelope_handle")
	assert "thread.vt_id()" not in body, (
		f"_emit_envelope_handle must NOT call thread.vt_id() — that would "
		f"record the drain worker's VT, not the emitter's:\n{body}"
	)
	assert "thread.kernel_thread_id()" not in body, (
		f"_emit_envelope_handle must NOT call thread.kernel_thread_id() — "
		f"that would record the drain worker's OS thread, not the "
		f"emitter's:\n{body}"
	)
	# Positive: it must read the stored snapshot.
	assert "env.vtid" in body, (
		f"_emit_envelope_handle must read env.vtid from the envelope:\n{body}"
	)
	assert "env.tid" in body, (
		f"_emit_envelope_handle must read env.tid from the envelope:\n{body}"
	)


def test_alloc_envelope_accepts_vtid_and_tid_parameters() -> None:
	"""`_alloc_envelope` is the seam between emit-time and drain-time
	— it must accept the snapshotted IDs as parameters so emit-time
	callers can hand them in."""
	text = _read_log_drift()
	m = re.search(
		r"\nfn _alloc_envelope\((.*?)\)\s*->",
		text, re.DOTALL,
	)
	assert m, "could not locate fn _alloc_envelope(...) signature"
	sig = m.group(1)
	assert re.search(r"\bvtid\s*:\s*Int\b", sig), (
		f"_alloc_envelope must accept `vtid: Int` so emit-time snapshot "
		f"can be threaded into the envelope:\n{sig}"
	)
	assert re.search(r"\btid\s*:\s*Int\b", sig), (
		f"_alloc_envelope must accept `tid: Int` so emit-time snapshot "
		f"can be threaded into the envelope:\n{sig}"
	)
