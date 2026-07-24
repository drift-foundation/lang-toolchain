# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""B-repr(B5) §3.1 layout audit: String storage/header layout knowledge
is restricted to EXACTLY two parties —

  C side:      lang/language_runtime/string_runtime.{h,c} (by EXACT
               path — a same-named file elsewhere is NOT exempt)
  compiler:    the codegen layout authority in llvm_codegen.py — the
               literal emitters (incl. the empty-singleton handle), the
               StringByteAt bytes-base, the StringBytesBase intrinsic,
               and the `__drift_string_observe_guard` they share (its
               flags-word load at storage offset 8)

Production C surface: lang/language_runtime and lang/compiler_infra,
scanned RECURSIVELY (a future nested production directory is covered
automatically); compiler_infra was the directory whose
duplicate-definition escape produced the error_dummy defect.
Exclusions are EXACT resolved paths: lang/compiler_infra/tests (C
whiteboxes — deliberate layout probes justified in-file).
lang-obsolete is outside the scanned roots entirely.

Checks:
  * no `DriftRcBytes` TYPE usage at all outside string_runtime (catches
    aliased header access: `DriftRcBytes *h = ...; h->flags`);
  * no `->strong` / bare `storage->flags` / singleton symbol / storage
    tail arithmetic;
  * no duplicate `struct DriftString` / `struct DriftRcBytes`
    definitions anywhere (authoritative-path pin, not line numbers);
  * no value- OR pointer-form member reads of `len` / `data` /
    `storage` on identifiers declared as DriftString (values or
    pointers);
  * negative self-teeth prove the scanners catch basename shadowing,
    aliased ->flags, and ->storage reads.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RUNTIME = ROOT / "lang" / "language_runtime"
CODEGEN = ROOT / "lang" / "codegen" / "llvm" / "llvm_codegen.py"

# EXACT allowed paths (resolved) — never basename comparison.
_ALLOWED_C_PATHS = {
	(RUNTIME / "string_runtime.h").resolve(),
	(RUNTIME / "string_runtime.c").resolve(),
}

# Recursive roots: any FUTURE nested production directory under these
# trees is scanned automatically; exclusions are exact resolved paths.
_PRODUCTION_C_ROOTS = [
	RUNTIME,
	ROOT / "lang" / "compiler_infra",
]
_EXCLUDED_PATHS = {
	(ROOT / "lang" / "compiler_infra" / "tests").resolve(),
}

_FORBIDDEN_PATTERNS = [
	(re.compile(r"\bDriftRcBytes\b"), "DriftRcBytes type usage (header layout access)"),
	(re.compile(r"->\s*strong\b"), "->strong access"),
	(re.compile(r"storage\s*->\s*flags\b"), "storage->flags access"),
	(re.compile(r"__drift_rt_string_empty"), "empty-singleton symbol"),
	(re.compile(r"storage\s*\+\s*1\b"), "storage tail arithmetic"),
]

_DEFN_PATTERNS = [
	(re.compile(r"struct\s+DriftString\s*\{"), "DriftString"),
	(re.compile(r"struct\s+DriftRcBytes\s*\{"), "DriftRcBytes"),
]

_DECL_RX = re.compile(r"\bDriftString\s*(\**)\s*([A-Za-z_][A-Za-z0-9_]*)")
_MEMBERS = ("len", "data", "storage")


def _c_files():
	for root in _PRODUCTION_C_ROOTS:
		for pat in ("*.c", "*.h"):
			for path in root.rglob(pat):
				resolved = path.resolve()
				if any(resolved.is_relative_to(ex) for ex in _EXCLUDED_PATHS):
					continue
				yield path


def _is_allowed(path: Path) -> bool:
	return path.resolve() in _ALLOWED_C_PATHS


def _scan_forbidden(name: str, text: str) -> list[str]:
	out = []
	for rx, label in _FORBIDDEN_PATTERNS:
		for m in rx.finditer(text):
			line = text.count("\n", 0, m.start()) + 1
			out.append(f"{name}:{line}: {label}")
	return out


def _scan_member_reads(name: str, text: str) -> list[str]:
	out = []
	names = {m.group(2) for m in _DECL_RX.finditer(text)} - {"DriftString"}
	for ident in sorted(names):
		for member in _MEMBERS:
			# value form (s.len) AND pointer form (s->len)
			rx = re.compile(rf"\b{re.escape(ident)}\s*(?:\.|->)\s*{member}\b")
			for m in rx.finditer(text):
				line = text.count("\n", 0, m.start()) + 1
				out.append(f"{name}:{line}: {ident} member read '{member}'")
	return out


def _scan_definitions(name: str, text: str) -> list[str]:
	out = []
	for rx, label in _DEFN_PATTERNS:
		for _ in rx.finditer(text):
			out.append(f"{name}: struct {label}")
	return out


def test_runtime_c_layout_knowledge_is_confined() -> None:
	violations: list[str] = []
	for path in _c_files():
		if _is_allowed(path):
			continue
		violations += _scan_forbidden(path.name, path.read_text(errors="replace"))
	assert not violations, (
		"String layout knowledge outside string_runtime.{h,c}:\n" + "\n".join(violations)
	)


def test_runtime_c_member_reads_migrated_to_accessors() -> None:
	violations: list[str] = []
	for path in _c_files():
		if _is_allowed(path):
			continue
		violations += _scan_member_reads(path.name, path.read_text(errors="replace"))
	assert not violations, (
		"DriftString member reads outside string_runtime (use drift_string_len/data):\n"
		+ "\n".join(violations)
	)


def test_no_duplicate_driftstring_definitions() -> None:
	"""EXACTLY ONE definition each of `struct DriftString` and
	`struct DriftRcBytes` across all live production C, at the
	AUTHORITATIVE PATH (path-pinned, not line-pinned)."""
	found: list[tuple[Path, str]] = []
	for path in _c_files():
		for entry in _scan_definitions(path.name, path.read_text(errors="replace")):
			found.append((path.resolve(), entry.split(": ", 1)[1]))
	authoritative = (RUNTIME / "string_runtime.h").resolve()
	assert sorted(found) == [
		(authoritative, "struct DriftRcBytes"),
		(authoritative, "struct DriftString"),
	], (
		"struct DriftString/DriftRcBytes must each be defined exactly once, "
		f"in {authoritative}; found: {found}"
	)


def test_codegen_layout_authority_is_exactly_three_lowerings() -> None:
	src = CODEGEN.read_text()
	# The +16 bytes-base offset: exactly twice (StringByteAt and
	# StringBytesBase); the literal emitters point at the HEADER and never
	# spell the byte offset.
	offs = re.findall(r"getelementptr i8, ptr \{storage_tmp\}, \{idx_llty\} 16", src)
	assert len(offs) == 2, (
		f"expected exactly 2 bytes-base (+16) lowerings (StringByteAt, StringBytesBase); found {len(offs)}"
	)
	# The observe guard's flags-word access (offset 8): exactly once,
	# inside the guard emission.
	flag_geps = re.findall(r"getelementptr i8, ptr %storage, i64 8", src)
	assert len(flag_geps) == 1, (
		f"expected exactly 1 flags-word (+8) access (the observe guard); found {len(flag_geps)}"
	)
	# The singleton symbol: docstring + declare + handle emit, all inside
	# _emit_empty_singleton_handle.
	sing = re.findall(r"__drift_rt_string_empty", src)
	assert len(sing) == 3, (
		"expected exactly 3 empty-singleton references (docstring + declare + "
		f"handle, all inside _emit_empty_singleton_handle) in codegen; found {len(sing)}"
	)
	assert "_emit_empty_singleton_handle" in src
	# Literal flag computation is centralized.
	assert src.count("def _string_literal_flags") == 1
	# The retired ABI-21 bytes-GEP (constant field 2) must not return.
	assert "i32 0, i32 2, i32 0" not in src, "retired data-GEP (field 2) reappeared in codegen"


def test_compiler_stage2_is_representation_blind() -> None:
	"""The MIR vocabulary and ownership pipeline carry NO layout
	knowledge: nothing in lang/driftc spells the header offset, the
	singleton symbol, or DriftRcBytes."""
	bad: list[str] = []
	for path in (ROOT / "lang" / "driftc").rglob("*.py"):
		text = path.read_text(errors="replace")
		for needle in ("DriftRcBytes", "__drift_rt_string_empty"):
			if needle in text:
				bad.append(f"{path.relative_to(ROOT)}: {needle}")
	assert not bad, "layout knowledge leaked into lang/driftc:\n" + "\n".join(bad)


# ── Negative self-teeth: the scanners must CATCH these shapes ────────


def test_negative_basename_shadowing_not_exempt(tmp_path: Path) -> None:
	"""A file NAMED string_runtime.h outside the authoritative path is
	NOT exempt (exact-path allowlist, never basename)."""
	shadow = tmp_path / "string_runtime.h"
	shadow.write_text("struct DriftString { drift_isize len; char *data; };\n")
	assert not _is_allowed(shadow), "basename shadowing must not bypass the allowlist"
	assert _scan_definitions(shadow.name, shadow.read_text()), (
		"the duplicate-definition scanner must flag the shadow file"
	)


def test_negative_aliased_flags_access_caught() -> None:
	"""Aliasing the header pointer defeats name-based ->flags matching —
	the TYPE-level DriftRcBytes ban must catch it."""
	text = "DriftRcBytes *h = get_header();\nuint64_t f = h->flags;\n"
	hits = _scan_forbidden("synthetic.c", text)
	assert any("DriftRcBytes type usage" in h for h in hits), hits


def test_negative_storage_member_read_caught() -> None:
	"""Both value- and pointer-form .storage reads on DriftString
	identifiers are member-read violations."""
	text = (
		"void f(DriftString s, DriftString *ps) {\n"
		"    void *a = s.storage;\n"
		"    void *b = ps->storage;\n"
		"}\n"
	)
	hits = _scan_member_reads("synthetic.c", text)
	assert any("s member read 'storage'" in h for h in hits), hits
	assert any("ps member read 'storage'" in h for h in hits), hits


def test_negative_pointer_form_len_data_caught() -> None:
	text = (
		"void g(DriftString *p) {\n"
		"    long n = p->len;\n"
		"    const char *d = p->data;\n"
		"}\n"
	)
	hits = _scan_member_reads("synthetic.c", text)
	assert any("'len'" in h for h in hits) and any("'data'" in h for h in hits), hits
