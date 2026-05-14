# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Native-deps wiring tests for the std.codec.gzip_* surface.

The gzip codec calls into libz via a runtime/toolchain-owned C shim
(`lang/language_runtime/codec_gzip_runtime.c`). For the link contract
to hold end-to-end, three things must be in place:

  1. The shim's .c source is in the runtime archive's source list,
     so codec_gzip_runtime.o ends up inside libdrift_rt_abi<N>.a.

  2. The stdlib package build (tools/deploy/steps/stdlib.py) declares
     -lz via `--native-link-lib z`, so consumer auto-link picks it up.

  3. The e2e test runner passes -lz too (tests build against stdlib
     source, not the production .dmp, so they bypass path #2).

The general native-deps auto-link mechanism is exercised by
`lang/tests/driver/test_driftc_package_v0.py::TestConsumerAutoLink`
with synthetic libs. These tests pin the *stdlib-specific* wiring.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def test_codec_gzip_runtime_source_in_runtime_archive() -> None:
	"""codec_gzip_runtime.c is in the runtime archive source list, so
	codec_gzip_runtime.o ends up inside libdrift_rt_abi<N>.a alongside
	the other runtime helpers."""
	from lang.language_runtime import get_runtime_sources

	sources = get_runtime_sources(ROOT)
	src_names = {p.name for p in sources}
	assert "codec_gzip_runtime.c" in src_names, (
		"codec_gzip_runtime.c must be in get_runtime_sources() so the "
		"runtime archive contains the gzip shim; otherwise consumers of "
		"std.codec.gzip_* will fail to link with undefined drift_codec_gzip_* "
		f"symbols. Found sources: {sorted(src_names)}"
	)


def test_codec_runtime_header_exists() -> None:
	"""The shared codec runtime header exists alongside the gzip shim."""
	header = ROOT / "lang" / "language_runtime" / "codec_runtime.h"
	assert header.exists(), (
		f"codec_runtime.h must exist at {header}; it defines the shared "
		"DRIFT_CODEC_* return codes and the codec-agnostic free helper "
		"(drift_codec_free) used by every codec shim in the family."
	)


def test_stdlib_deploy_build_declares_libz() -> None:
	"""tools/deploy/steps/stdlib.py builds the stdlib .dmp with
	--native-link-lib z so every consumer of stdlib auto-links libz
	via package native_deps. Because stdlib is compiled monolithically,
	std.codec's gzip wrappers are emitted into every binary's IR and
	pull codec_gzip_runtime.o (which references libz) into the link —
	`-Wl,--as-needed` cannot strip the resulting libz.so.1 entry from
	DT_NEEDED. So libz becomes both a build-time and runtime dependency
	for every Drift program on x86_64 Linux.
	"""
	step_src = (ROOT / "tools" / "deploy" / "steps" / "stdlib.py").read_text(encoding="utf-8")
	# Look for the --native-link-lib argument followed by "z" in the
	# build_stdlib_package command construction. The exact formatting
	# can vary (single line, multi-line, with comment between) so use
	# a regex with DOTALL.
	pat = re.compile(r'"--native-link-lib"\s*,\s*"z"', re.DOTALL)
	assert pat.search(step_src) is not None, (
		"tools/deploy/steps/stdlib.py must include --native-link-lib z "
		"in the build_stdlib_package command, so the stdlib .dmp's "
		"native_deps.link_libs includes 'z' and consumers auto-link -lz."
	)


def test_e2e_runner_links_libz() -> None:
	"""lang/tests/codegen/e2e/runner.py passes -lz to clang so e2e tests
	(which build against stdlib source, not the .dmp) link cleanly.
	Without this, every e2e test that imports std.codec (which most do
	transitively) would fail to link with undefined deflate / inflate
	references from codec_gzip_runtime.o.
	"""
	runner_src = (ROOT / "lang" / "tests" / "codegen" / "e2e" / "runner.py").read_text(encoding="utf-8")
	# Look for the -lz lib added to link_libs.
	pat = re.compile(r'_link_flags_for_lib\(\s*"z"\s*\)', re.DOTALL)
	assert pat.search(runner_src) is not None, (
		"lang/tests/codegen/e2e/runner.py must include "
		"_link_flags_for_lib(\"z\") in its link_libs, otherwise gzip-using "
		"e2e tests will fail at link time with undefined deflate/inflate refs."
	)


def test_codec_shim_symbols_present() -> None:
	"""Sanity-check that the gzip shim exports the expected C symbols.
	Parses codec_gzip_runtime.c and codec_runtime.h directly rather
	than building the runtime; cheap and avoids requiring libz at test
	collection time.
	"""
	src = (ROOT / "lang" / "language_runtime" / "codec_gzip_runtime.c").read_text(encoding="utf-8")
	for sym in ("drift_codec_gzip_encode", "drift_codec_gzip_decode", "drift_codec_free", "drift_codec_copy_bytes"):
		assert sym in src, f"codec_gzip_runtime.c must define {sym}"

	header = (ROOT / "lang" / "language_runtime" / "codec_runtime.h").read_text(encoding="utf-8")
	# Header declares the shared error codes; spot-check a couple
	for code in ("DRIFT_CODEC_OK", "DRIFT_CODEC_TRUNCATED", "DRIFT_CODEC_BAD_DATA", "DRIFT_CODEC_OUTPUT_TOO_LARGE", "DRIFT_CODEC_INVALID_LEVEL"):
		assert code in header, f"codec_runtime.h must declare {code}"


def test_codec_drift_declares_gzip_exports() -> None:
	"""std.codec exposes gzip_encode / gzip_encode_level / gzip_decode
	and the GZIP_DECODE_MAX_OUTPUT cap constant."""
	codec_src = (ROOT / "stdlib" / "std" / "codec" / "codec.drift").read_text(encoding="utf-8")
	for sym in ("gzip_encode", "gzip_encode_level", "gzip_decode", "GZIP_DECODE_MAX_OUTPUT"):
		assert sym in codec_src, f"stdlib/std/codec/codec.drift must export {sym}"
	# Output cap value should be 256 MiB exactly.
	assert "268435456" in codec_src, (
		"GZIP_DECODE_MAX_OUTPUT should be 268435456 (256 MiB). If you change "
		"the cap value, update this test and the docstring on the constant."
	)
