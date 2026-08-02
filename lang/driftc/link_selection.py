# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""The SINGLE production authority for driftc's native-library, linker, and
sanitizer selection.

`driftc.py` and the ownership-corpus compile contract
(`tools/corpus_compile_contract.py`) BOTH import these helpers, so a corpus
compile can never be modeled by an approximation that drifts from what the
compiler actually links.  Every function here mirrors — and is the sole home
of — the logic that used to live inline in `driftc._run_compile_cli`.
"""
from __future__ import annotations

import shutil
from ctypes.util import find_library
from pathlib import Path

from lang.driftc.env_flags import env_true

# ── native libraries ─────────────────────────────────────────────────

# Production library search directories (x86_64 Linux, the only supported
# target).  A library is linked only when ctypes' find_library resolves it AND
# its `lib<name>.so` exists in one of these dirs.
NATIVE_SEARCH_DIRS = (
	Path("/lib"),
	Path("/lib64"),
	Path("/usr/lib"),
	Path("/usr/lib64"),
	Path("/lib/x86_64-linux-gnu"),
	Path("/usr/lib/x86_64-linux-gnu"),
)

# Backtrace-symbolization libraries, gated on the DEBUG-STYLE runtime
# (DRIFT_DEBUG), NOT on sanitizer selection: assert_runtime.c only references
# libdwfl/libunwind/libelf symbols under -DDRIFT_RT_MODE_DEBUG=1.  A
# debug+ASan build still links these because it is debug-style.
DEBUG_BACKTRACE_LIBS = ("dw", "unwind", "unwind-x86_64", "elf")


def link_flags_for_lib(name: str) -> list[str]:
	"""`[-l<name>]` iff the library resolves in production search dirs, else []."""
	if not find_library(name):
		return []
	for d in NATIVE_SEARCH_DIRS:
		if (d / f"lib{name}.so").exists():
			return [f"-l{name}"]
	return []


def resolve_native_lib_path(name: str) -> "str | None":
	"""The `lib<name>.so` path driftc would actually link — find_library-gated,
	first matching production search dir — or None.  This is the SAME authority
	`link_flags_for_lib` uses, so a fingerprint that hashes this path hashes the
	very file the compiler links (not whatever `clang -print-file-name` reports,
	which searches different directories)."""
	if not find_library(name):
		return None
	for d in NATIVE_SEARCH_DIRS:
		so = d / f"lib{name}.so"
		if so.exists():
			return str(so)
	return None


def native_link_lib_names(debug_style: bool) -> list[str]:
	"""The RESOLVED native library base-names driftc links, in link order:
	the debug backtrace libs (only under debug-style) then libz (always) —
	each included only when it resolves on this host."""
	names: list[str] = []
	if debug_style:
		for n in DEBUG_BACKTRACE_LIBS:
			if link_flags_for_lib(n):
				names.append(n)
	if link_flags_for_lib("z"):
		names.append("z")
	return names


def native_link_flags(debug_style: bool) -> list[str]:
	"""The `-l…` flags driftc appends to the link line."""
	return [f"-l{n}" for n in native_link_lib_names(debug_style)]


# ── linker ───────────────────────────────────────────────────────────

def select_linker(linker_arg: "str | None") -> str:
	"""driftc's `--linker` resolution: explicit `ld`/`gold` wins; otherwise
	prefer ld.gold when present, else the default ld."""
	if linker_arg == "ld":
		return "ld"
	if linker_arg == "gold":
		return "gold"
	if shutil.which("ld.gold") is not None:
		return "gold"
	return "ld"


# ── sanitizer selection ──────────────────────────────────────────────

VALID_SANITIZERS = ("address", "undefined", "none")


def sanitizer_tokens(value: str) -> "frozenset[str]":
	"""Parse a `--sanitize=<comma-list>` value into its non-`none` token set.
	Raises ValueError on unknown tokens or `none` combined with others (driftc
	wraps this as an argparse error)."""
	tokens = [t.strip() for t in value.split(",") if t.strip()]
	unknown = sorted({t for t in tokens if t not in VALID_SANITIZERS})
	if unknown:
		raise ValueError(f"unknown sanitizer(s): {', '.join(unknown)}; "
		                 f"valid: {', '.join(VALID_SANITIZERS)}")
	non_none = {t for t in tokens if t != "none"}
	if "none" in tokens and non_none:
		raise ValueError("'none' cannot be combined with other sanitizers")
	return frozenset(non_none)


def effective_sanitizers(sanitize_tokens: "frozenset[str] | None",
                         env: "dict | None" = None) -> "tuple[bool, bool]":
	"""(asan, ubsan) exactly as driftc resolves them: an explicit `--sanitize`
	token set is authoritative; otherwise the DRIFT_ASAN/DRIFT_UBSAN env
	aliases apply."""
	if sanitize_tokens is not None:
		return "address" in sanitize_tokens, "undefined" in sanitize_tokens
	return env_true("DRIFT_ASAN", env), env_true("DRIFT_UBSAN", env)
