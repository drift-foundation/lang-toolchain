# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Static audit of synthetic-owning-local discipline in
`lang/driftc/stage2/hir_to_mir.py`.

Scans the file for sites matching the canonical synthetic-local
shape:

    self.b.ensure_local(NAME)
    self._local_types[NAME] = ...
    self.b.emit(M.StoreLocal(local=NAME, ...))

(all three within a 30-line forward window from the `ensure_local`
call).  Every match must satisfy ONE of:

  (a) Explicit register: the 30-line forward window also contains
      `self._register_drop_local(NAME, ...)`.  Used by the helper
      body itself AND by hand-rolled sites whose enclosing comment
      documents WHY they hand-roll (via a marker).
  (b) Allow marker: a comment line within 10 lines of the
      `ensure_local` call matches the marker grammar:
          `# materialize-audit: allow <reason> <free-form>`

There is NO helper-name bypass.  The helper body itself
(`_materialize_owned_temp`) MUST contain `_register_drop_local`
to pass the audit -- same rule as every other site.  This is the
load-bearing call the whole audit exists to enforce; exempting the
helper by name would let a future edit silently delete the call
and still ship.

Allowed reason keywords (typos fail; lint validates against this
set):

  - `non-owning`   -- synthesized local holds a non-droppable value
                      (Bool short-circuit, Int ephemeral, etc.)
  - `consumed`     -- owning value consumed inline by a following
                      MoveOut / DropValue / Call; never reaches
                      scope-exit cleanup
  - `registered`   -- owning value registered via a different
                      mechanism (match_cleanup_authoring, drop_flags,
                      binder bind path, explicit M.CleanupHook).
                      Marker must name the registration site.
  - `synthesized`  -- owning struct/variant assembled inline and
                      consumed by the same expression (e.g.
                      diagnostic owning throw)
  - `intentional`  -- reviewed and deliberately hand-rolled for a
                      specific reason (rare; marker must explain)

Hard gate.  No `audit-pending`, no `audit-deferred`, no warning-only
escape.  If a site cannot be classified within slice scope, stop
the slice and report -- do not paper over with a deferral marker.

Companion audit shape:
`test_drift_owned_string_audit.py` (runtime ABI ownership).
`test_ledger_cache_safety_mutation_audit.py` (ledger freshness).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCOPED_FILE = ROOT / "lang" / "driftc" / "stage2" / "hir_to_mir.py"

ALLOWED_REASONS = frozenset({
	"non-owning",
	"consumed",
	"registered",
	"synthesized",
	"intentional",
})

# Pattern: `self.b.ensure_local(NAME)` where NAME is an identifier.
_ENSURE_LOCAL_RE = re.compile(
	r"^(\s*)self\.b\.ensure_local\(\s*([a-zA-Z_][\w]*)\s*\)",
	re.MULTILINE,
)

# Marker pattern: `# materialize-audit: allow <reason> <rest-of-line>`
_MARKER_RE = re.compile(
	r"#\s*materialize-audit:\s*allow\s+([a-z\-]+)\b"
)


def _enclosing_function_name(lines: list[str], line_idx: int) -> str | None:
	"""Walk backwards from line_idx (0-based) to find the nearest
	`def <name>(...)` and return <name>.  Returns None if no enclosing
	def is found within the file (shouldn't happen for any real site)."""
	for i in range(line_idx, -1, -1):
		m = re.match(r"\s*def\s+([A-Za-z_]\w*)\s*\(", lines[i])
		if m is not None:
			return m.group(1)
	return None


def _window_contains_local_types_set(window: str, var: str) -> bool:
	pat = rf"self\._local_types\[\s*{re.escape(var)}\s*\]\s*="
	return bool(re.search(pat, window))


def _window_contains_storelocal(window: str, var: str) -> bool:
	pat = rf"M\.StoreLocal\(\s*local\s*=\s*{re.escape(var)}\b"
	return bool(re.search(pat, window))


def _window_contains_register_drop(window: str, var: str) -> bool:
	pat = rf"self\._register_drop_local\(\s*{re.escape(var)}\b"
	return bool(re.search(pat, window))


def _find_marker(window: str) -> str | None:
	"""Return the marker's reason keyword if a marker is in the
	window, else None.  The keyword may or may not be in
	ALLOWED_REASONS; caller validates."""
	m = _MARKER_RE.search(window)
	return m.group(1) if m else None


def _find_violations_in_text(src: str, *, scope_label: str = "") -> list[str]:
	"""Run the audit on `src` (a hir_to_mir.py file body or a
	synthetic snippet) and return a list of violation messages.
	Empty list means clean."""
	lines = src.split("\n")
	violations: list[str] = []
	for m in _ENSURE_LOCAL_RE.finditer(src):
		var = m.group(2)
		line_idx = src.count("\n", 0, m.start())
		# Build forward window: 30 lines from the ensure_local line.
		window = "\n".join(lines[line_idx : line_idx + 31])
		# Build proximity window for marker scan: 10 lines each side
		# of the ensure_local line.
		marker_start = max(0, line_idx - 10)
		marker_window = "\n".join(lines[marker_start : line_idx + 11])
		# Site is a candidate iff `_local_types[var] = ...` AND
		# `StoreLocal(local=var, ...)` both appear in forward window.
		if not _window_contains_local_types_set(window, var):
			continue
		if not _window_contains_storelocal(window, var):
			continue
		# Candidate site -- regardless of enclosing function name.
		# The helper body itself MUST satisfy rule (a) explicitly;
		# there is no name-based bypass.  Otherwise a future edit
		# that deletes the helper's `_register_drop_local` call
		# would silently pass this audit.
		fn_name = _enclosing_function_name(lines, line_idx)
		# Rule (a): explicit register in window?
		has_register = _window_contains_register_drop(window, var)
		# Rule (b): marker present?
		marker_reason = _find_marker(marker_window)
		if marker_reason is None and not has_register:
			violations.append(
				f"{scope_label}{line_idx + 1}: synthetic-local site "
				f"`{var}` in `{fn_name}` has the canonical "
				f"ensure_local + StoreLocal triple but no "
				f"_register_drop_local AND no allow marker.  Fix: "
				f"either convert to `_materialize_owned_temp(...)` / "
				f"`_materialize_owned_temp_for_borrow(...)`, OR add "
				f"`self._register_drop_local({var}, ...)` within 30 "
				f"lines, OR add a `# materialize-audit: allow <reason> "
				f"<why>` comment within 10 lines.  Allowed reasons: "
				f"{sorted(ALLOWED_REASONS)}."
			)
			continue
		if marker_reason is not None:
			if marker_reason not in ALLOWED_REASONS:
				violations.append(
					f"{scope_label}{line_idx + 1}: synthetic-local site "
					f"`{var}` in `{fn_name}` has allow marker with "
					f"unrecognized reason `{marker_reason}`.  Allowed "
					f"reasons: {sorted(ALLOWED_REASONS)}.  No "
					f"`audit-pending` / `audit-deferred` escape -- "
					f"every site must be classified at slice landing."
				)
				continue
		# Passes one of rule (b) or rule (c).
	return violations


def test_owned_temp_materialization_audit() -> None:
	"""Hard-gate audit over hir_to_mir.py."""
	src = SCOPED_FILE.read_text(encoding="utf-8")
	rel = str(SCOPED_FILE.relative_to(ROOT))
	violations = _find_violations_in_text(src, scope_label=f"{rel}:")
	assert not violations, (
		f"{len(violations)} materialize-audit violation(s):\n\n"
		+ "\n\n".join(violations)
	)


# ---------------------------------------------------------------------------
# Parser self-tests (synthetic snippets exercise the matchers
# independently of the real `hir_to_mir.py` content).  Pinned positive
# cases mirror what the scanner ACTUALLY sees in production -- helper-
# using sites do NOT contain the inline triple at the caller (the
# triple lives inside the helper body), so they are not in the
# candidate set and need no positive test.  The real positive cases
# are: explicit register (covers both the helper body AND hand-rolled
# sites) and recognized allow marker.
# ---------------------------------------------------------------------------


def test_audit_self_helper_body_passes_via_explicit_register() -> None:
	"""The helper body (`_materialize_owned_temp`) passes the audit
	because it contains the canonical `_register_drop_local` call --
	NOT because of any name-based bypass.  This is the load-bearing
	call the whole audit exists to enforce; the helper passes the
	same rule every other site passes."""
	src = (
		"def _materialize_owned_temp(self, *, name_prefix, ty, value):\n"
		"\tlocal = f'{name_prefix}{self.b.new_temp()}'\n"
		"\tself.b.ensure_local(local)\n"
		"\tself._local_types[local] = ty\n"
		"\tself._register_drop_local(local, ty)\n"
		"\tself.b.emit(M.StoreLocal(local=local, value=value))\n"
		"\treturn local\n"
	)
	violations = _find_violations_in_text(src)
	assert not violations, f"helper body with register must pass; got: {violations}"


def test_audit_self_helper_body_without_register_fails() -> None:
	"""The helper body with the `_register_drop_local` call REMOVED
	must fail the audit.  Pins the load-bearing invariant: the audit
	has no helper-name bypass; a future edit that drops the register
	call from the helper body would be caught by this audit, not
	silently shipped."""
	src = (
		"def _materialize_owned_temp(self, *, name_prefix, ty, value):\n"
		"\tlocal = f'{name_prefix}{self.b.new_temp()}'\n"
		"\tself.b.ensure_local(local)\n"
		"\tself._local_types[local] = ty\n"
		"\t# _register_drop_local intentionally omitted to test audit\n"
		"\tself.b.emit(M.StoreLocal(local=local, value=value))\n"
		"\treturn local\n"
	)
	violations = _find_violations_in_text(src)
	assert len(violations) == 1 and "no _register_drop_local" in violations[0], (
		f"helper body without register must fail; got: {violations}"
	)


def test_audit_self_explicit_register_passes() -> None:
	"""A non-helper function with `ensure_local + _local_types +
	_register_drop_local + StoreLocal` (the full hand-rolled pattern)
	passes -- same rule the helper body passes."""
	src = (
		"def _some_other_path(self, ty, value):\n"
		"\tlocal = f'__x{self.b.new_temp()}'\n"
		"\tself.b.ensure_local(local)\n"
		"\tself._local_types[local] = ty\n"
		"\tself._register_drop_local(local, ty)\n"
		"\tself.b.emit(M.StoreLocal(local=local, value=value))\n"
	)
	violations = _find_violations_in_text(src)
	assert not violations, f"explicit register must pass; got: {violations}"


def test_audit_self_marker_with_recognized_reason_passes() -> None:
	"""A non-helper site with one of the recognized allow-marker
	reasons passes (here: `non-owning`)."""
	src = (
		"def _short_circuit_bool(self):\n"
		"\t# materialize-audit: allow non-owning Bool short-circuit temp\n"
		"\ttemp = f'__b{self.b.new_temp()}'\n"
		"\tself.b.ensure_local(temp)\n"
		"\tself._local_types[temp] = self._bool_type\n"
		"\tself.b.emit(M.StoreLocal(local=temp, value=v))\n"
	)
	violations = _find_violations_in_text(src)
	assert not violations, f"recognized marker must pass; got: {violations}"


def test_audit_self_unmarked_unregistered_fails() -> None:
	"""A non-helper site with the inline triple but NO
	_register_drop_local AND NO marker is a hard violation."""
	src = (
		"def _bad_unmarked(self, ty, value):\n"
		"\tlocal = f'__y{self.b.new_temp()}'\n"
		"\tself.b.ensure_local(local)\n"
		"\tself._local_types[local] = ty\n"
		"\tself.b.emit(M.StoreLocal(local=local, value=value))\n"
	)
	violations = _find_violations_in_text(src)
	assert len(violations) == 1 and "no _register_drop_local" in violations[0], (
		f"unmarked + unregistered must fail; got: {violations}"
	)


def test_audit_self_bad_reason_fails() -> None:
	"""Marker with an unknown reason keyword (audit-pending,
	audit-deferred, typos) fails."""
	src = (
		"def _bad_reason(self):\n"
		"\t# materialize-audit: allow audit-pending will-fix-later\n"
		"\tlocal = f'__z{self.b.new_temp()}'\n"
		"\tself.b.ensure_local(local)\n"
		"\tself._local_types[local] = ty\n"
		"\tself.b.emit(M.StoreLocal(local=local, value=v))\n"
	)
	violations = _find_violations_in_text(src)
	assert len(violations) == 1 and "audit-pending" in violations[0], (
		f"audit-pending must fail; got: {violations}"
	)
