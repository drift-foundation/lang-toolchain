# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Janitor-safe session-scoped scratch root for Drift tooling and tests.

Invariant
---------
Any artifact a Drift process writes under /tmp must live inside
``$DRIFT_TMP_ROOT`` so a janitor can later sweep stale sessions with a
single ``rm -rf`` — regardless of whether the creating process exited
cleanly, was OOM-killed, or was SIGKILLed.

``/tmp`` is memory-backed on most Linux setups (tmpfs), so a bad kill
during a heavy test or agent session can wedge the machine.  Cleanup
hooks (``try/finally``, ``TemporaryDirectory``, shell ``trap``) do not
run on SIGKILL, so the safety net is *namespace*, not graceful cleanup.

Usage
-----
``session_root()`` is the primary entry point.  It returns
``Path("$DRIFT_TMP_ROOT")`` — creating it lazily and writing the chosen
value back into ``os.environ`` so subprocesses inherit it.

``drift_tempdir()`` / ``drift_mkdtemp()`` / ``drift_mkstemp()`` are thin
wrappers that pin ``dir=session_root()`` so the result is guaranteed to
live in the Drift namespace, even if a caller forgets.

Pytest tests should prefer the built-in ``tmp_path`` fixture; the
top-level ``conftest.py`` exports ``PYTEST_DEBUG_TEMPROOT`` so pytest's
own tmp tree lands under the session root automatically.
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

_ENV = "DRIFT_TMP_ROOT"


def _resolve_user() -> str:
	return os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"


def session_root() -> Path:
	"""Return $DRIFT_TMP_ROOT, initializing it if unset.

	If $DRIFT_TMP_ROOT is already set (parent shell, agent harness, or an
	earlier call in this process), reuse it verbatim.  Otherwise pick
	``/tmp/drift-$USER/session-$PID-$timestamp`` and export it so child  ## drift-tmp-root-audit: allow namespace docstring
	processes inherit the same root.

	The returned directory is created (parents=True, exist_ok=True).
	"""
	root = os.environ.get(_ENV)
	if not root:
		# The one legitimate hard-coded /tmp/ literal in the repo: this  # drift-tmp-root-audit: allow namespace origin comment
		# helper IS the source of truth for the DRIFT_TMP_ROOT namespace.
		root = f"/tmp/drift-{_resolve_user()}/session-{os.getpid()}-{int(time.time())}"  # drift-tmp-root-audit: allow namespace origin
		os.environ[_ENV] = root
	p = Path(root)
	p.mkdir(parents=True, exist_ok=True)
	return p


def drift_tempdir(prefix: str = "tmp", suffix: str = "") -> "tempfile.TemporaryDirectory[str]":
	"""``tempfile.TemporaryDirectory`` pinned under :func:`session_root`."""
	return tempfile.TemporaryDirectory(dir=str(session_root()), prefix=prefix, suffix=suffix)


def drift_mkdtemp(prefix: str = "tmp", suffix: str = "") -> str:
	"""``tempfile.mkdtemp`` pinned under :func:`session_root`.

	Caller owns cleanup.  Prefer :func:`drift_tempdir` (context manager).
	"""
	return tempfile.mkdtemp(dir=str(session_root()), prefix=prefix, suffix=suffix)


def drift_mkstemp(prefix: str = "tmp", suffix: str = ""):
	"""``tempfile.mkstemp`` pinned under :func:`session_root`."""
	return tempfile.mkstemp(dir=str(session_root()), prefix=prefix, suffix=suffix)


__all__ = ("session_root", "drift_tempdir", "drift_mkdtemp", "drift_mkstemp")
