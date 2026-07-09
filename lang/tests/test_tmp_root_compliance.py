# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Static audit: enforce `DRIFT_TMP_ROOT` namespace compliance.

Background — incident, 2026-05-15
---------------------------------
A long-running test session left ~20 GB of scratch under `/tmp`,
filling tmpfs.  Most of the artifacts were Drift-owned but lacked any
recognizable prefix, so cleanup had to fall back to "anything owned
by $USER under /tmp" — too broad, with a real risk of deleting the
user's unrelated work.

The fix: the `/tmp/drift-$USER/session-*` namespace plus a janitor
that can sweep stale sessions safely (see `doc/conventions/tmp-root.md`
and `tools/drift_janitor.sh`).  But a convention is only a convention
until enforced — without a guard rail, regressions are inevitable.

What this audit checks
----------------------
1. **No hard-coded writable `/tmp/...` paths** in active source.
   Specifically, no string literal of the form `"/tmp/<x>"` outside
   an allow-listed set of mock-only / negative-test / docs uses.
2. **No bare `tempfile.mkdtemp()` / `mkstemp()`** — these must pass
   `dir=...` (`session_root()`, `tmp_path`, `BUILD_ROOT`, etc.).
3. **No bare `tempfile.TemporaryDirectory()`** outside allow-listed
   pytest-only files — same reason, plus the explicit `dir=` form
   is greppable for review.

The audit walks the repo with simple regex + AST checks; it is
intentionally simple so the failure messages point at the exact
line the contributor needs to fix.

To opt out of a single line, add a trailing comment
``# drift-tmp-root-audit: allow <reason>`` on the offending line.
Use sparingly and only when the path is genuinely safe (e.g., a
string fixture validating that a path is *rejected*, a mock return
value never written to, documentation).

To opt out of an entire file, add it to the file-level allow-list
below with a one-line reason.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# NOTE: there is NO file-level allow-list — the per-line marker
# `# drift-tmp-root-audit: allow <reason>` is the only opt-out
# mechanism.  Earlier drafts of this audit had a file-level
# allow-list, which was too coarse: a future real /tmp write or
# bare TemporaryDirectory call inside an "exempt" file would slip
# through.  Per-line markers force the contributor to acknowledge
# each specific safe literal, and a code reviewer can see exactly
# which lines are exempted in the diff.

# Self-exemption: this audit file contains the literal strings
# `"/tmp/"`, `tempfile.mktemp`, `tempfile.NamedTemporaryFile`, etc.
# in docstrings, error messages, and regex patterns.  Skip it
# entirely — it does not make any real calls.
_AUDIT_SELF = "lang/tests/test_tmp_root_compliance.py"

# Directories to walk.  Tests/tools/docs are in scope.  Vendored or
# generated dirs are not.
# The top-level `justfile` is a file root, like `conftest.py` — it was
# missing from earlier drafts, which is exactly how the
# `lang-llvm-test` recipe's hard-coded `/tmp/lang_test_codegen.o`
# escaped the audit until the 2026-07-08 tmpfs-exhaustion incident.
_WALK_ROOTS = ("lang", "tools", "stdlib", "doc", "conftest.py", "justfile", "examples", "tests", "work")

# Paths under these prefixes are excluded entirely (vendored, build
# artifacts, caches).
_EXCLUDE_PREFIXES = (
	".venv/", "build/", "staged/", ".git/", "__pycache__/",
	"node_modules/", "dist/",
	# History entry contains illustrative literals in prose; safe.
	"doc/history.md",
)

# Per-line opt-out marker.  Comment-syntax agnostic — works in Python
# (`#`), Drift (`//`), shell (`#`), Markdown (`<!--`), etc.  The
# marker text is the contract; the comment leader is whatever the
# host language accepts.
_ALLOW_RE = re.compile(r"drift-tmp-root-audit:\s*allow\b")

# `/tmp/<x>` ANYWHERE in a line — quoted, in heredocs, in
# docstrings, in shell command examples, etc.  The earlier
# quote-delimited regex missed heredocs and embedded shell, which
# was the original failure mode (a deploy bundle README example
# with `-o /tmp/hello` inside a Python docstring slipped through).
# Use per-line allow markers for harmless prose/examples.
# Negative lookbehind: `/tmp/` must not be preceded by a word char,
# dot, or dash — otherwise repo-local `build/tmp/...` (the disk-backed
# gate scratch root) and names like `foo.tmp/` would false-positive.
_HARDCODED_TMP_RE = re.compile(r"(?<![\w.-])/tmp/")


def _walk_source_files():
	for root_name in _WALK_ROOTS:
		root = _REPO_ROOT / root_name
		if not root.exists():
			continue
		if root.is_file():
			yield root
			continue
		for p in root.rglob("*"):
			if not p.is_file():
				continue
			rel = p.relative_to(_REPO_ROOT).as_posix()
			if any(rel.startswith(pref) for pref in _EXCLUDE_PREFIXES):
				continue
			# Audit Python, Drift, shell, justfile, markdown.
			if p.suffix not in (".py", ".drift", ".sh", ".md") and p.name not in ("justfile", "Makefile"):
				continue
			yield p


def test_no_hardcoded_writable_tmp_paths() -> None:
	"""No `/tmp/<x>` text in active source unless explicitly allowed
	per-line.  Scans for `/tmp/` ANYWHERE — quoted, in heredocs,
	docstrings, embedded shell — because heredocs were exactly the
	originally-missed failure mode.  See `doc/conventions/tmp-root.md`."""
	violations: list[str] = []
	for path in _walk_source_files():
		rel = path.relative_to(_REPO_ROOT).as_posix()
		if rel == _AUDIT_SELF:
			continue
		try:
			text = path.read_text(encoding="utf-8")
		except (UnicodeDecodeError, OSError):
			continue
		for lineno, line in enumerate(text.splitlines(), 1):
			if not _HARDCODED_TMP_RE.search(line):
				continue
			if _ALLOW_RE.search(line):
				continue
			# Skip pure markdown prose code-fence examples — they don't
			# get executed.
			if path.suffix == ".md":
				continue
			violations.append(
				f"{rel}:{lineno}: hard-coded /tmp path: {line.strip()}"
			)
	if violations:
		listing = "\n  ".join(violations)
		pytest.fail(
			f"DRIFT_TMP_ROOT compliance violations ({len(violations)} hard-coded "
			f"/tmp/ reference(s)):\n  {listing}\n\n"
			f"Fix: use the appropriate scratch helper.  Python: "
			f"`from lang.test_support.drift_tmp import drift_tempdir, drift_mkdtemp, session_root` "
			f"and pin `dir=session_root()`.  Drift: `env.drift_tmp_path(\"name\")`.  "
			f"Per-line allow (for harmless prose/examples/mock returns/path-rejection tests): "
			f"add ` # drift-tmp-root-audit: allow <reason>` to the line."
		)


# tempfile APIs that must pin `dir=...`.  `mktemp` is fully deprecated
# (insecure; race condition between path return and file create) and
# must not be used at all — it has no `dir=` form that closes the
# race window, so we flag every call.
_TEMPFILE_DIR_REQUIRED = {"mkdtemp", "mkstemp", "TemporaryDirectory", "NamedTemporaryFile"}
_TEMPFILE_FORBIDDEN = {"mktemp"}


class _TempfileBareCallVisitor(ast.NodeVisitor):
	"""Detect tempfile calls that need `dir=`, plus `tempfile.mktemp`
	(forbidden outright)."""

	def __init__(self, source_lines: list[str]) -> None:
		self.source_lines = source_lines
		# (lineno, fn_name, category)  category in {"dir_required", "forbidden"}
		self.bare_calls: list[tuple[int, str, str]] = []

	def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
		fn_name, category = _resolve_tempfile_call(node.func)
		if fn_name is not None:
			# Per-line allow.
			lineno = node.lineno
			allowed = (
				1 <= lineno <= len(self.source_lines)
				and bool(_ALLOW_RE.search(self.source_lines[lineno - 1]))
			)
			if not allowed:
				if category == "dir_required":
					has_dir_kw = any(kw.arg == "dir" for kw in node.keywords)
					if not has_dir_kw:
						self.bare_calls.append((lineno, fn_name, category))
				elif category == "forbidden":
					self.bare_calls.append((lineno, fn_name, category))
		self.generic_visit(node)


def _resolve_tempfile_call(func: ast.expr) -> tuple[str | None, str | None]:
	"""Return (api_name, category) if `func` is a tempfile call we care
	about, else (None, None).  category is "dir_required" or "forbidden"."""
	# `tempfile.<X>` form.
	if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "tempfile":
		if func.attr in _TEMPFILE_DIR_REQUIRED:
			return f"tempfile.{func.attr}", "dir_required"
		if func.attr in _TEMPFILE_FORBIDDEN:
			return f"tempfile.{func.attr}", "forbidden"
	# Bare `<X>` form (after `from tempfile import X`).
	if isinstance(func, ast.Name):
		if func.id in _TEMPFILE_DIR_REQUIRED:
			return func.id, "dir_required"
		if func.id in _TEMPFILE_FORBIDDEN:
			return func.id, "forbidden"
	return None, None


def _collect_tempfile_violations() -> tuple[list[str], list[str]]:
	"""Walk Python source; return (dir_required_violations, forbidden_violations)."""
	dir_required_violations: list[str] = []
	forbidden_violations: list[str] = []
	for path in _walk_source_files():
		if path.suffix != ".py":
			continue
		rel = path.relative_to(_REPO_ROOT).as_posix()
		if rel == _AUDIT_SELF:
			continue
		try:
			text = path.read_text(encoding="utf-8")
		except (UnicodeDecodeError, OSError):
			continue
		try:
			tree = ast.parse(text, filename=str(path))
		except SyntaxError:
			continue
		visitor = _TempfileBareCallVisitor(text.splitlines())
		visitor.visit(tree)
		for lineno, fn_name, category in visitor.bare_calls:
			entry = f"{rel}:{lineno}: {fn_name}"
			if category == "dir_required":
				dir_required_violations.append(f"{entry} (missing dir= argument)")
			else:
				forbidden_violations.append(f"{entry} (forbidden — use drift_mkdtemp / drift_tempdir)")
	return dir_required_violations, forbidden_violations


def test_no_bare_tempfile_dir_required_calls() -> None:
	"""All of `tempfile.mkdtemp`, `mkstemp`, `TemporaryDirectory`, and
	`NamedTemporaryFile` must pin `dir=...`.  Without it, the
	resulting path lives outside the Drift namespace where a
	SIGKILL-leak survives until the next reboot.

	`NamedTemporaryFile` is included here because it is the most
	common source of leak-prone short-lived files in CI tooling
	(IR validators, snapshot tests, etc.) — exactly the kind of
	use this convention targets."""
	dir_required, _ = _collect_tempfile_violations()
	if dir_required:
		listing = "\n  ".join(dir_required)
		pytest.fail(
			f"DRIFT_TMP_ROOT compliance violations ({len(dir_required)} bare "
			f"tempfile call(s) missing `dir=`):\n  {listing}\n\n"
			f"Fix: pass `dir=session_root()` "
			f"(from `lang.test_support.drift_tmp`), `dir=tmp_path`, or another "
			f"caller-controlled directory.  Per-line allow: add "
			f"` # drift-tmp-root-audit: allow <reason>`."
		)


def test_no_tempfile_mktemp_calls() -> None:
	"""`tempfile.mktemp()` is forbidden outright.

	It is deprecated (and has been since Python 2.3): the API returns
	a path without atomically creating the file, leaving a TOCTOU
	window between the return and the caller's `open()`.  It also
	has no API surface for pinning under `$DRIFT_TMP_ROOT` (no
	`dir=` accepted), making it incompatible with the janitor-safe
	namespace.  Use `drift_mkdtemp()` + a known filename, or
	`drift_tempdir()` for a context-managed scratch dir."""
	_, forbidden = _collect_tempfile_violations()
	if forbidden:
		listing = "\n  ".join(forbidden)
		pytest.fail(
			f"DRIFT_TMP_ROOT compliance violations ({len(forbidden)} forbidden "
			f"tempfile call(s)):\n  {listing}\n\n"
			f"Fix: replace with `drift_mkdtemp(prefix='...')` plus a known "
			f"sub-name, or `drift_tempdir(...)` as a context manager."
		)
