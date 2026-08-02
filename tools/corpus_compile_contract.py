#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""The SINGLE corpus compile contract.

Both the corpus COMPILATION (drift_corpus_audit._compile_one, which spawns
driftc) and the FINGERPRINT consume this — exact driftc argv, normalized child
environment, runtime variant, and tool/linker/native-lib selection — so there
are never two approximations that can drift apart.  Every resolver mirrors what
driftc ACTUALLY does (env_flags.env_true, language_runtime.runtime_archive_
variant, shutil.which("clang"), the `--linker` override, always `-lz`); parity
with driftc is pinned in the fingerprint tests.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lang.driftc import link_selection  # noqa: E402
from lang.driftc.env_flags import env_true  # noqa: E402
from lang.language_runtime import runtime_archive_variant  # noqa: E402

# A schema stamp for the CONTRACT itself: bump when the compile contract
# (argv shape, env normalization, variant/tool/lib selection) changes, so the
# fingerprint invalidates cached records that were produced under an older one.
# v2: --sanitize override in runtime_variant, debug-lib parity via
# link_selection, build-relevant non-Drift env normalized into the identity.
CONTRACT_SCHEMA_VERSION = 2

# DRIFT*/DRIFTC* variables the corpus compile HONORS (compile-relevant).  These
# are inherited into the child; every OTHER ambient variable is simply not
# copied (see normalized_child_env — the child env is CONSTRUCTED, not filtered).
HONORED_ENV = (
	"DRIFT_STRING_ARC_AUDIT",           # the corpus sets this
	"DRIFT_STRING_ARC_AUDIT_VERBOSE",   # changes audit RECORDS
	"DRIFT_DEBUG",
	"DRIFT_ASAN",
	"DRIFT_UBSAN",
	"DRIFT_RUNTIME_LIB_CACHE_DIR",
	"DRIFT_RUNTIME_BUILD_ROOT",
	"DRIFT_TOOLCHAIN_ROOT",
)
# NOTE: DRIFT_ALLOC_TRACK is deliberately NOT honored — the corpus lane pins
# alloc_track=False, matching driftc's own build_runtime_archive call.

# Vars the corpus sets itself (fixed for the whole run).
_CORPUS_SET_ENV = {"DRIFT_STRING_ARC_AUDIT": "1"}
# Per-fixture OUTPUT path — set by the caller, NOT part of the compile identity.
AUDIT_FILE_ENV = "DRIFT_STRING_ARC_AUDIT_FILE"

# The ONLY ambient (non-Drift) variables inherited into the child.  The child
# environment is CONSTRUCTED from this allowlist — never copied wholesale — so
# NO unlisted compile-affecting variable (LD_PRELOAD, PYTHONOPTIMIZE,
# COMPILER_PATH, GCC_EXEC_PREFIX, …) can change a result without moving the
# fingerprint: anything not listed here is simply absent from the child.
_INHERITED_NON_DRIFT_ENV = (
	"PATH", "HOME",                              # tool resolution / tool HOME
	"LD_LIBRARY_PATH", "LIBRARY_PATH",           # link/library search
	"C_INCLUDE_PATH", "CPATH", "CPLUS_INCLUDE_PATH",  # C include search
	"PYTHONPATH",                                # child interpreter module path
)
INHERITED_ENV = _INHERITED_NON_DRIFT_ENV + HONORED_ENV
# Deterministic pins on EVERY child (part of the identity): stable hash seed and
# locale so assertion behaviour and diagnostics cannot vary with the ambient
# shell.  PYTHONOPTIMIZE etc. are simply not inherited, so they are absent.
PINNED_ENV = {"PYTHONHASHSEED": "0", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}

# Vars set per-run to a CONTROLLED value (so a child compiler/interpreter never
# falls back to a shared /tmp) but fingerprinted as a STABLE PLACEHOLDER — their
# absolute path is volatile and must not perturb the compile identity.
TMPDIR_ENV = "TMPDIR"
_PLACEHOLDER_ENV = {TMPDIR_ENV: "<controlled-per-run-scratch>"}


def normalized_child_env(ambient: "dict | None" = None,
                         scratch: "str | None" = None) -> dict:
	"""The EXACT environment handed to every driftc child, CONSTRUCTED from the
	inherit-allowlist (never a filtered copy of the ambient environment): the
	allowlisted ambient values, then the corpus-set vars, then the deterministic
	pins.  When `scratch` is given, TMPDIR is pointed at that controlled per-run
	directory so no child writes under a shared /tmp.  The per-fixture audit-file
	path is added by the caller."""
	src = os.environ if ambient is None else ambient
	env = {k: src[k] for k in INHERITED_ENV if k in src}
	env.update(_CORPUS_SET_ENV)
	env.update(PINNED_ENV)
	if scratch is not None:
		env[TMPDIR_ENV] = str(scratch)
	return env


def fingerprint_env(ambient: "dict | None" = None) -> dict:
	"""The compile IDENTITY: the EXACT normalized child environment (which is
	already minimal), minus the per-fixture output path, with TMPDIR represented
	as a stable placeholder.  Because the child env is constructed, not filtered,
	the identity IS the child env — no "copy everything, fingerprint a subset"
	gap — and the volatile scratch path never perturbs it."""
	env = normalized_child_env(ambient)
	env.pop(AUDIT_FILE_ENV, None)
	env.update(_PLACEHOLDER_ENV)
	return dict(sorted(env.items()))


def debug_style(env: "dict | None" = None) -> bool:
	return env_true("DRIFT_DEBUG", os.environ if env is None else env)


def _sanitize_override(extra_args) -> "frozenset[str] | None":
	"""The `--sanitize=<tokens>` (or `--sanitize <tokens>`) selection from the
	driftc args, or None if not given — parsed by the shared authority so it
	matches the compiler flag exactly."""
	val = _last_option_value(extra_args, "--sanitize")
	if val is None:
		return None
	return link_selection.sanitizer_tokens(val)


def runtime_variant(extra_args=(), env: "dict | None" = None) -> str:
	"""The runtime archive variant driftc selects for the corpus lane.  Mirrors
	driftc exactly: an explicit `--sanitize` is authoritative over DRIFT_ASAN/
	DRIFT_UBSAN (driftc.py:9557), env_true semantics ("0" is FALSE), alloc_track
	pinned False.  extra_args is honored so `--sanitize` cannot make the
	prebuilt/hashed archive differ from the one driftc links."""
	e = os.environ if env is None else env
	asan, ubsan = link_selection.effective_sanitizers(_sanitize_override(extra_args), e)
	return runtime_archive_variant(
		debug_style=env_true("DRIFT_DEBUG", e),
		asan_enabled=asan,
		ubsan_enabled=ubsan,
		alloc_track_enabled=False,
	)


def resolve_clang() -> "str | None":
	return shutil.which("clang")  # matches driftc — NOT CLANG_BIN


def resolve_ar() -> "str | None":
	return shutil.which("llvm-ar") or shutil.which("ar")


def _last_option_value(extra_args, flag: str) -> "str | None":
	"""The LAST value of `--flag VALUE` / `--flag=VALUE` across the whole vector
	— matching argparse's store action (last occurrence wins), so a later
	override (e.g. `--sanitize=address --sanitize=none`) is honored exactly as
	driftc honors it."""
	args = list(extra_args)
	found = None
	eq = f"{flag}="
	i = 0
	while i < len(args):
		a = args[i]
		if a == flag and i + 1 < len(args):
			found = args[i + 1]
			i += 2
			continue
		if a.startswith(eq):
			found = a.split("=", 1)[1]
		i += 1
	return found


def _explicit_linker(extra_args) -> "str | None":
	return _last_option_value(extra_args, "--linker")


def resolve_linker(extra_args=()) -> dict:
	"""driftc's linker selection, via the shared authority: explicit
	`--linker ld|gold` wins, else prefer ld.gold, else default ld."""
	selection = link_selection.select_linker(_explicit_linker(extra_args))
	path = shutil.which("ld.gold") if selection == "gold" else shutil.which("ld")
	return {"selection": selection, "path": path}


def native_link_libs(debug_style_runtime: bool) -> list[str]:
	"""The RESOLVED native library base-names driftc links (debug backtrace libs
	only under debug-style, libz always), via the shared production authority —
	keyed on DRIFT_DEBUG, NOT on the sanitizer variant, so debug+ASan is modeled
	correctly."""
	return link_selection.native_link_lib_names(debug_style_runtime)


def driftc_argv(fixture_main: Path, entry: str, out: Path, extra_args, stdlib_root: Path) -> list[str]:
	"""The EXACT argv the corpus passes to `python -m lang.driftc.driftc`."""
	return ["--dev", "--stdlib-root", str(stdlib_root), *list(extra_args),
	        str(fixture_main), "--entry", entry, "-o", str(out)]


def driftc_argv_template(extra_args) -> list[str]:
	"""The argv with per-fixture / per-machine slots as placeholders — the
	stable shape the fingerprint records (content of stdlib/fixtures is hashed
	elsewhere)."""
	return ["--dev", "--stdlib-root", "<STDLIB>", *list(extra_args),
	        "<FIXTURE>", "--entry", "<ENTRY>", "-o", "<OUT>"]
