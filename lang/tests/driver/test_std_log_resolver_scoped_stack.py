# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression (0.27.203): `std.log.ContextResolver` returns a borrow
(`Optional<&log.LogContext>`) so that apps whose natural request-
context storage is `rt.ScopedStack<log.LogContext>` in a thread-local
registry can implement the resolver without an owned-context round
trip.  The owned variant (0.27.202) forced a `LogContext.clone()`
that stdlib did not provide — the app team hit this wall and filed
`/tmp/stdlib-log-context-resolver-clone-gap.md`.  # drift-tmp-root-audit: allow historical doc reference in module docstring

This test is the app-shape pin for the borrowed API.  It covers:

  1. A resolver that discovers context from the thread registry
     (`rt.thread_registry` -> `rt.get<RequestContextState>` ->
     `state.ctx.peek()`), returning the borrow straight through.
  2. Bare `logger.info("event")` merges the pushed context into the
     record.
  3. An explicit `&log.log_context()` call suppresses the resolver
     for that one emit (per-call opt-out).
  4. After the scoped-stack entry is popped, the resolver returns
     `None` and subsequent bare emits carry no ambient context.

Both the "resolver returns borrow rooted in thread-local state" path
and the "empty-context suppression" path are exercised end-to-end
(compile + run binary).  Exit code 0 means every in-source
assertion succeeded.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root

from lang.codegen.llvm.test_utils import sanitizer_timeout


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _compile_and_run(
	tmp_path: Path,
	source: str,
	capsys: pytest.CaptureFixture[str],
) -> tuple[int, int, str]:
	"""Compile `source` to an executable; run it; return (compile_rc, run_rc, stderr)."""
	mod_root = tmp_path / "mods"
	main_src = mod_root / "main" / "main.drift"
	_write_file(main_src, source)
	exe = tmp_path / "out"
	root = stdlib_root()
	args = [
		"-M", str(mod_root),
		str(main_src),
		"-o", str(exe),
		"--dev",
		"--json",
	]
	if root:
		args += ["--stdlib-root", str(root)]
	rc = driftc_main(args)
	capsys.readouterr()
	if rc != 0:
		return rc, -1, ""
	result = subprocess.run(
		[str(exe)],
		capture_output=True,
		text=True,
		timeout=sanitizer_timeout(10),
	)
	return 0, result.returncode, result.stderr


_APP_SHAPE_SCOPED_STACK_RESOLVER = """
module main;

import std.log as log;
import std.runtime as rt;
import std.concurrent as conc;

// The app's request-scoped state: a scoped stack of LogContexts.
// Installed once per thread into the thread registry; the resolver
// reads from it on every emit.
pub struct RequestContextState {
	pub ctx: rt.ScopedStack<log.LogContext>
}

pub fn request_context_state() nothrow -> RequestContextState {
	return RequestContextState(ctx = rt.scoped_stack<type log.LogContext>());
}

// The resolver pulls request context from the thread registry and
// returns the peeked LogContext as a borrow.  No cloning; no owned
// round-trip.  When the stack is empty (or the state isn't
// installed) the resolver returns None, matching the contract that
// bare emits carry no ambient context.
pub struct AppLogResolver { }

implement log.ContextResolver for AppLogResolver {
	pub fn resolve(self: &AppLogResolver) nothrow -> Optional<&log.LogContext> {
		val _ = self;
		val reg = rt.thread_registry();
		match rt.get<type RequestContextState>(reg) {
			Some(st) => {
				return st.ctx.peek();
			},
			None => {
				return Optional<&log.LogContext>::None();
			}
		}
	}
}

fn _peek_depth() nothrow -> Int {
	val reg = rt.thread_registry();
	match rt.get<type RequestContextState>(reg) {
		Some(st) => { return st.ctx.depth(); },
		None => { return -1; }
	}
}

fn main() nothrow -> Int {
	val reg = rt.thread_registry();
	if not reg.set<type RequestContextState>(request_context_state()) {
		return 10;
	}

	// Before any push, resolver must observe an empty stack.
	if _peek_depth() != 0 { return 11; }

	// Build logger with the app's resolver installed.
	var b = log.config_builder();
	b.min_level(log.Level::Debug());
	b.context_resolver(conc.arc(AppLogResolver()).as_interface<type log.ContextResolver>());
	val logger = log.create_logger("svc", b.build());

	// Bare emit with empty stack: resolver returns None; logger emits
	// without ambient context — no crash, no UAF.
	logger.info("pre-push");

	// Push a request-scoped LogContext onto the stack, then log while
	// the guard is live.  Resolver must return a borrow into the
	// pushed context, and the record must carry `req_id` / `user`.
	match rt.get_mut<type RequestContextState>(reg) {
		Some(mutst) => {
			var ctx = log.log_context();
			ctx.put("req_id", "R-123");
			ctx.put("user", "alice");
			val guard = mutst.ctx.push(move ctx);

			// Sanity: depth should be 1 while guard is live.
			if mutst.ctx.depth() != 1 { return 20; }

			// Bare emit: ambient ctx from the stack is picked up.
			logger.info("inside-request");

			// Per-call opt-out: explicit empty &LogContext suppresses
			// the resolver for this one emit.  No language restriction
			// on holding the guard and an explicit empty ctx in the
			// same scope.
			logger.info("inside-request-opt-out", &log.log_context());

			// `guard` drops at the end of this arm — its Destructible
			// impl pops the LogContext off the stack.
			val _ = move guard;
		},
		None => { return 21; }
	}

	// Guard has been dropped — stack is empty again.  Resolver
	// returns None; bare emit carries no ambient context.
	if _peek_depth() != 0 { return 30; }
	logger.info("post-pop");

	return 0;
}
""".lstrip()


def test_scoped_stack_resolver_via_thread_registry(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""End-to-end: a ContextResolver that peeks a `rt.ScopedStack<LogContext>`
	out of the thread registry and returns the borrow must compile and
	run correctly under the 0.27.203 borrowed-return contract.  This
	is the exact app shape from the gap report — if it doesn't work
	here, the app team hits the same wall on the next integration."""
	compile_rc, run_rc, stderr = _compile_and_run(
		tmp_path, _APP_SHAPE_SCOPED_STACK_RESOLVER, capsys
	)
	assert compile_rc == 0, f"compile failed: rc={compile_rc}"
	assert run_rc == 0, (
		f"scoped-stack resolver pattern returned {run_rc}, expected 0 "
		f"(non-zero is an in-source assertion index — see main.drift)\n"
		f"stderr: {stderr[-600:]}"
	)

	# Verify the ambient-context merge actually happened: `inside-request`
	# must carry `req_id` and `user`, while `pre-push`, `inside-request-opt-out`,
	# and `post-pop` must NOT carry them.
	inside_lines = [
		ln for ln in stderr.splitlines()
		if '"ev":"inside-request"' in ln or '"ev": "inside-request"' in ln
	]
	assert inside_lines, f"no inside-request log line in stderr:\n{stderr}"
	assert any("R-123" in ln and "alice" in ln for ln in inside_lines), (
		f"inside-request record missing req_id/user attrs merged from ambient "
		f"scoped-stack context:\n{inside_lines}"
	)

	for ev in ("pre-push", "inside-request-opt-out", "post-pop"):
		ev_lines = [
			ln for ln in stderr.splitlines()
			if f'"ev":"{ev}"' in ln or f'"ev": "{ev}"' in ln
		]
		assert ev_lines, f"no {ev} log line in stderr:\n{stderr}"
		assert not any("R-123" in ln for ln in ev_lines), (
			f"{ev} record unexpectedly carries ambient ctx (resolver should "
			f"have returned None or been suppressed):\n{ev_lines}"
		)
