# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Pins for lang.driftc.link_selection — the SINGLE production authority that
driftc and the ownership-corpus compile contract share for native-library,
linker, and sanitizer selection.  Parity here IS parity between what driftc
links and what the corpus fingerprint models.
"""
from __future__ import annotations

import pytest

from lang.driftc import link_selection as L


# ── native libraries ─────────────────────────────────────────────────

def test_native_normal_lane_is_libz_only():
	# driftc always links -lz; the normal lane links nothing else.
	assert L.native_link_flags(False) == ["-lz"]
	assert L.native_link_lib_names(False) == ["z"]


def test_native_debug_lane_adds_all_four_backtrace_libs():
	"""debug+ASan must still fingerprint all four debug libraries plus z; the
	debug libs are gated on debug-style, not on the sanitizer variant."""
	names = L.native_link_lib_names(True)
	assert names == ["dw", "unwind", "unwind-x86_64", "elf", "z"]
	assert L.native_link_flags(True) == ["-ldw", "-lunwind", "-lunwind-x86_64", "-lelf", "-lz"]
	# normal lane is a strict subset (z only).
	assert set(L.native_link_lib_names(False)) <= set(names)


def test_link_flags_for_missing_lib_is_empty():
	assert L.link_flags_for_lib("definitely_not_a_real_lib_xyz") == []
	assert L.resolve_native_lib_path("definitely_not_a_real_lib_xyz") is None


def test_resolve_native_lib_path_is_a_real_search_dir_file():
	p = L.resolve_native_lib_path("z")
	assert p is not None and p.endswith("libz.so")
	from pathlib import Path
	assert any(str(Path(p).parent) == str(d) for d in L.NATIVE_SEARCH_DIRS)


# ── linker ───────────────────────────────────────────────────────────

def test_select_linker_explicit_wins():
	assert L.select_linker("ld") == "ld"
	assert L.select_linker("gold") == "gold"


def test_select_linker_auto_prefers_gold_when_present():
	import shutil
	expected = "gold" if shutil.which("ld.gold") else "ld"
	assert L.select_linker(None) == expected


# ── sanitizer selection ──────────────────────────────────────────────

def test_sanitizer_tokens_parse():
	assert L.sanitizer_tokens("address") == frozenset({"address"})
	assert L.sanitizer_tokens("address,undefined") == frozenset({"address", "undefined"})
	assert L.sanitizer_tokens("none") == frozenset()


def test_sanitizer_tokens_reject_unknown_and_none_combo():
	with pytest.raises(ValueError, match="unknown sanitizer"):
		L.sanitizer_tokens("bogus")
	with pytest.raises(ValueError, match="'none' cannot be combined"):
		L.sanitizer_tokens("none,address")


def test_effective_sanitizers_tokens_authoritative_over_env():
	assert L.effective_sanitizers(frozenset({"address"}), {"DRIFT_ASAN": "0"}) == (True, False)
	assert L.effective_sanitizers(frozenset(), {"DRIFT_ASAN": "1"}) == (False, False)   # none wins


def test_effective_sanitizers_env_fallback_when_no_tokens():
	assert L.effective_sanitizers(None, {"DRIFT_ASAN": "1", "DRIFT_UBSAN": "0"}) == (True, False)
	assert L.effective_sanitizers(None, {}) == (False, False)
