# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""`drift inspect build-info` + the shared production reader (W3).

Pins the full fail-closed contract of PLAN §2.4 AT THE CLI: every
hostile row proves exit 1, EMPTY stdout, and a stderr diagnostic. The
hostile inputs are hand-synthesized minimal ELF64 files (no binutils
dependency — the synthesizer also covers what objcopy cannot express,
e.g. duplicate or SHT_NOBITS sections). `--json` success output is the
section's exact canonical UTF-8 bytes plus one newline — pinned under a
hostile PYTHONIOENCODING too. The inspected binary is never executed:
the reader is pure file parsing.
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.build_info import (
	BUILD_INFO_MAX_PAYLOAD,
	BuildInfoError,
	assemble_build_info,
	canonical_json,
	extract_build_info,
	read_build_info_section,
)

ROOT = Path(__file__).resolve().parents[2]

_PROG = "pub fn main() nothrow -> Int {\n\treturn 0;\n}\n"

_SHT_PROGBITS, _SHT_STRTAB, _SHT_NOBITS = 1, 3, 8
_SHF_COMPRESSED = 0x800


def _valid_payload(description: str = "d") -> bytes:
	return assemble_build_info(
		git_sha="abc", word_bits=64, build_profile="optimized",
		build_utc="2026-07-31T00:00:00Z",
		artifact={"name": "synth", "version": "1.0.0",
		          "description": description, "license": "MIT"},
		dependencies={}, extra={},
	).encode("utf-8")


def _synth_elf(
	payload: bytes,
	*,
	bi_count: int = 1,
	bi_type: int = _SHT_PROGBITS,
	bi_flags: int = 0,
	bi_offset_override: int | None = None,
	str_type: int = _SHT_STRTAB,
	str_offset_override: int | None = None,
	class_byte: int = 2,
	endian_byte: int = 1,
	ident_version: int = 1,
	shoff_override: int | None = None,
	ehsize_override: int | None = None,
	shentsize_override: int | None = None,
	shstr_size_override: int | None = None,
) -> bytes:
	"""Minimal valid-by-default ELF64 with `bi_count` .drift_build_info
	sections; every knob produces one hostile mutation."""
	shstr = b"\x00.shstrtab\x00.drift_build_info\x00"
	name_shstrtab, name_bi = 1, 11
	ehsize, shentsize = 0x40, 0x40
	shnum = 2 + bi_count
	off_payload = ehsize
	off_shstr = off_payload + len(payload)
	off_shoff = off_shstr + len(shstr)
	hdr = bytearray(ehsize)
	hdr[:4] = b"\x7fELF"
	hdr[4], hdr[5], hdr[6] = class_byte, endian_byte, ident_version
	struct.pack_into(
		"<HHIQQQIHHHHHH", hdr, 0x10,
		2, 0x3E, 1, 0, 0,
		shoff_override if shoff_override is not None else off_shoff,
		0, ehsize_override if ehsize_override is not None else ehsize,
		0, 0,
		shentsize_override if shentsize_override is not None else shentsize,
		shnum, 1)

	def sh(name, typ, flags, off, size):
		return struct.pack("<IIQQQQIIQQ", name, typ, flags, 0,
		                   off, size, 0, 0, 1, 0)

	str_off = (str_offset_override if str_offset_override is not None
	           else off_shstr)
	bi_off = (bi_offset_override if bi_offset_override is not None
	          else off_payload)
	shstr_size = (shstr_size_override if shstr_size_override is not None
	              else len(shstr))
	table = sh(0, 0, 0, 0, 0) + sh(name_shstrtab, str_type, 0, str_off, shstr_size)
	for _ in range(bi_count):
		table += sh(name_bi, bi_type, bi_flags, bi_off, len(payload))
	return bytes(hdr) + payload + shstr + table


def _inspect(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
	code = ("import sys\nfrom lang.drift.cli import main\n"
	        "sys.exit(main(sys.argv[1:]))")
	return subprocess.run(
		[sys.executable, "-c", code, "inspect", "build-info"] + args,
		capture_output=True, cwd=ROOT, timeout=60,
		env={**os.environ, **(env or {})})


@pytest.fixture(scope="module")
def stamped_binary(tmp_path_factory) -> Path:
	d = tmp_path_factory.mktemp("inspect")
	src = d / "main.drift"
	src.write_text(_PROG)
	out = d / "app"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", str(src),
		 "-o", str(out),
		 "--artifact-name", "inspectee", "--artifact-version", "1.0.0",
		 "--artifact-description", "d", "--artifact-license", "MIT"],
		capture_output=True, text=True, cwd=ROOT,
		timeout=sanitizer_timeout(180))
	assert res.returncode == 0, res.stderr[-800:]
	return out


class TestSuccess:
	def test_json_is_exact_section_bytes_real_binary(self, stamped_binary) -> None:
		section = read_build_info_section(stamped_binary)
		res = _inspect([str(stamped_binary), "--json"])
		assert res.returncode == 0, res.stderr
		assert res.stdout == section + b"\n"
		assert json.loads(res.stdout)["artifact"]["name"] == "inspectee"

	def test_default_output_pretty_prints(self, stamped_binary) -> None:
		res = _inspect([str(stamped_binary)])
		assert res.returncode == 0
		assert json.loads(res.stdout)["artifact"]["version"] == "1.0.0"
		assert b"\n  " in res.stdout, "default output should be indented"

	def test_synthetic_valid_elf_accepted(self, tmp_path) -> None:
		payload = _valid_payload()
		f = tmp_path / "synth"
		f.write_bytes(_synth_elf(payload))
		res = _inspect([str(f), "--json"])
		assert res.returncode == 0, res.stderr
		assert res.stdout == payload + b"\n"

	def test_unicode_exact_bytes_under_hostile_stdout_encoding(self, tmp_path) -> None:
		"""print() would traceback/mangle under PYTHONIOENCODING=ascii;
		BOTH modes write through the binary stream."""
		payload = _valid_payload(description="snow ☃ unicode")
		f = tmp_path / "unicode"
		f.write_bytes(_synth_elf(payload))
		res = _inspect([str(f), "--json"], env={"PYTHONIOENCODING": "ascii"})
		assert res.returncode == 0, res.stderr
		assert res.stdout == payload + b"\n"
		res = _inspect([str(f)], env={"PYTHONIOENCODING": "ascii"})
		assert res.returncode == 0, res.stderr
		assert "snow ☃ unicode".encode("utf-8") in res.stdout


def _mutate_bi_size(elf: bytes, new_size: int) -> bytes:
	"""Patch the .drift_build_info section header's sh_size field —
	the declared size lies while the file stays small, proving the
	reader rejects from the HEADER before copying anything."""
	e_shoff = int.from_bytes(elf[0x28:0x30], "little")
	e_shentsize = int.from_bytes(elf[0x3A:0x3C], "little")
	e_shnum = int.from_bytes(elf[0x3C:0x3E], "little")
	out = bytearray(elf)
	# The bi section is the last header (see _synth_elf layout).
	off = e_shoff + (e_shnum - 1) * e_shentsize
	out[off + 0x20:off + 0x28] = new_size.to_bytes(8, "little")
	return bytes(out)


def _oversized_payload() -> bytes:
	pad = "x" * (BUILD_INFO_MAX_PAYLOAD)
	return b"{" + pad.encode() + b"}"


_HOSTILE_ROWS = [
	("missing_section", lambda p: _synth_elf(p, bi_count=0), b"no .drift_build_info"),
	("duplicate_sections", lambda p: _synth_elf(p, bi_count=2), b"exactly one"),
	("wrong_class", lambda p: _synth_elf(p, class_byte=1), b"ELF class"),
	("wrong_endianness", lambda p: _synth_elf(p, endian_byte=2), b"byte order"),
	("bad_ident_version", lambda p: _synth_elf(p, ident_version=9), b"identification version"),
	("bad_ehsize", lambda p: _synth_elf(p, ehsize_override=0x20), b"header size"),
	("table_out_of_bounds", lambda p: _synth_elf(p, shoff_override=1 << 32), b"section table out of bounds"),
	("strtab_out_of_bounds", lambda p: _synth_elf(p, str_offset_override=1 << 32), b"string table out of bounds"),
	("strtab_wrong_type", lambda p: _synth_elf(p, str_type=_SHT_PROGBITS), b"SHT_STRTAB"),
	("oversized_shentsize", lambda p: _synth_elf(p, shentsize_override=4096), b"malformed ELF section table header"),
	("oversized_shstrtab", lambda p: _synth_elf(p, shstr_size_override=(1 << 20) + 1), b"exceeds the"),
	("nobits_section", lambda p: _synth_elf(p, bi_type=_SHT_NOBITS), b"SHT_PROGBITS"),
	("compressed_section", lambda p: _synth_elf(p, bi_flags=_SHF_COMPRESSED), b"compressed"),
	("content_out_of_bounds", lambda p: _synth_elf(p, bi_offset_override=1 << 32), b"content out of bounds"),
	("empty_payload", lambda p: _synth_elf(b""), b"empty"),
	("oversized_payload", lambda p: _synth_elf(_oversized_payload()), b"exceeds the cap"),
	("oversized_declared_size", lambda p: _mutate_bi_size(_synth_elf(p), 2 << 20), b"exceeds the cap"),
	("invalid_utf8", lambda p: _synth_elf(b"\xff\xfe\x01"), b"UTF-8"),
	("invalid_json", lambda p: _synth_elf(b"{nope"), b"not valid JSON"),
	("wrong_discriminator", lambda p: _synth_elf(
		canonical_json({"format": "other/v9"}).encode()), b"invalid"),
	("schema_violation", lambda p: _synth_elf(
		canonical_json({"format": "drift-build-info/v1"}).encode()),
	 b"top-level keys"),
	("noncanonical_json", lambda p: _synth_elf(
		json.dumps(json.loads(p), indent=1, sort_keys=True,
		           ensure_ascii=False).encode()), b"canonically"),
]


class TestCliHostileMatrix:
	"""PLAN §2.4: each row proves exit 1 + EMPTY stdout + stderr diag
	AT THE CLI, on synthesized inputs (no binutils, deterministic)."""

	@pytest.mark.parametrize(
		"label,craft,expect",
		_HOSTILE_ROWS, ids=[r[0] for r in _HOSTILE_ROWS])
	def test_row(self, tmp_path, label, craft, expect) -> None:
		f = tmp_path / label
		f.write_bytes(craft(_valid_payload()))
		res = _inspect([str(f)])
		assert res.returncode == 1, (label, res.stdout[:200])
		assert res.stdout == b"", label
		assert expect in res.stderr, (label, res.stderr[:300])

	def test_non_elf_truncated_nonexistent_directory(self, tmp_path) -> None:
		txt = tmp_path / "not_elf.txt"
		txt.write_text("just text\n")
		trunc = tmp_path / "truncated"
		trunc.write_bytes(_synth_elf(_valid_payload())[:200])
		for target in (txt, trunc, tmp_path / "no-such-file", tmp_path):
			res = _inspect([str(target)])
			assert res.returncode == 1 and res.stdout == b"", target
			assert res.stderr, target


class TestReaderUnit:
	def test_reader_raises_buildinfoerror_only(self, stamped_binary, tmp_path) -> None:
		"""Never OSError or bare exceptions from the shared reader."""
		with pytest.raises(BuildInfoError, match="not a regular file"):
			read_build_info_section(tmp_path / "missing")
		small = tmp_path / "small"
		small.write_bytes(b"\x7fELF")
		with pytest.raises(BuildInfoError, match="too small"):
			read_build_info_section(small)
		# Deterministic read failure (chmod(0) is a no-op under root /
		# CAP_DAC_OVERRIDE): mock the open itself.
		unreadable = tmp_path / "unreadable"
		unreadable.write_bytes(_synth_elf(_valid_payload()))
		from unittest.mock import patch as _patch
		with _patch("builtins.open",
		            side_effect=PermissionError("mocked denial")):
			with pytest.raises(BuildInfoError, match="cannot read"):
				read_build_info_section(unreadable)
		assert extract_build_info(stamped_binary)
