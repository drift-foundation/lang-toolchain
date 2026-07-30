# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.codegen.llvm.test_utils import sanitizer_timeout


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def test_default_rejects_unqualified_println(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	source = """
module m_main;

pub fn main() nothrow -> Int{
	println("ok");
	return 0;
}
"""
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	_write_file(main_path, source)
	rc, payload = _run_driftc_json(["-M", str(root), str(main_path)], capsys)
	assert rc != 0
	msgs = [d.get("message", "") for d in payload.get("diagnostics", [])]
	assert any("println" in m for m in msgs)


def test_no_prelude_also_rejects_unqualified_println(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	source = """
module m_main;

pub fn main() nothrow -> Int{
	println("ok");
	return 0;
}
"""
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	_write_file(main_path, source)
	rc, payload = _run_driftc_json(["--no-prelude", "-M", str(root), str(main_path)], capsys)
	assert rc != 0
	msgs = [d.get("message", "") for d in payload.get("diagnostics", [])]
	assert any("println" in m for m in msgs)


def test_no_prelude_explicit_import_allows_println(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	main_src = """
module m_main;

import std.console as console;

pub fn main() nothrow -> Int{
	console.println("ok");
	return 0;
}
	"""
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	_write_file(main_path, main_src)
	rc, payload = _run_driftc_json(["--no-prelude", "-M", str(root), str(main_path)], capsys)
	assert rc == 0, payload


def test_explicit_import_allows_eprint_variants(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	main_src = """
module m_main;

import std.console as console;

pub fn main() nothrow -> Int{
	console.eprint("x");
	console.eprintln("y");
	return 0;
}
	"""
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	_write_file(main_path, main_src)
	rc, payload = _run_driftc_json(["-M", str(root), str(main_path)], capsys)
	assert rc == 0, payload


def test_std_io_stream_handles_are_copyable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	main_src = """
module m_main;

import std.io as io;

pub fn main() nothrow -> Int{
	val out = io.stdout();
	val out2 = out;
	val err = io.stderr();
	val in0 = io.stdin();
	val in1 = in0;
	return 0;
}
	"""
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	_write_file(main_path, main_src)
	rc, payload = _run_driftc_json(["-M", str(root), str(main_path)], capsys)
	assert rc == 0, payload


def test_std_io_configured_builder_path_compiles(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	main_src = """
module m_main;

import std.io as io;
import std.concurrent as conc;

pub fn main() nothrow -> Int{
	val out = io.stdout_builder().timeout(conc.Duration(millis = 25)).build();
	var b = io.buffer(1);
	io.buffer_write(b, 0, cast<Byte>(65));
	val _ = out.write(b);
	return 0;
}
	"""
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	_write_file(main_path, main_src)
	rc, payload = _run_driftc_json(["-M", str(root), str(main_path)], capsys)
	assert rc == 0, payload


def test_std_core_string_from_utf8_bytes_compiles(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	main_src = """
module m_main;

import std.core as core;
import std.io as io;

pub fn main() nothrow -> Int{
	var b = io.buffer(3);
	io.buffer_write(b, 0, cast<Byte>(97));
	io.buffer_write(b, 1, cast<Byte>(98));
	io.buffer_write(b, 2, cast<Byte>(99));
	val s = core.string_from_utf8_bytes(io.buffer_ptr(b), io.buffer_len(b));
	if s.byte_length() != 3 {
		return 2;
	}
	return 0;
}
	"""
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	_write_file(main_path, main_src)
	rc, payload = _run_driftc_json(["-M", str(root), str(main_path)], capsys)
	assert rc == 0, payload


def test_std_io_configured_read_line_compiles(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	main_src = """
module m_main;

import std.io as io;

pub fn main() nothrow -> Int{
	val i = io.stdin_builder().build();
	val _ = i.read_line();
	return 0;
}
	"""
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	_write_file(main_path, main_src)
	rc, payload = _run_driftc_json(["-M", str(root), str(main_path)], capsys)
	assert rc == 0, payload


def test_std_io_file_builder_path_compiles(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	main_src = """
module m_main;

import std.io as io;
import std.concurrent as conc;

pub fn main() nothrow -> Int{
	val opened = io.file_builder("tmp.bin").write(true).create(true).truncate(true).mode(io.FILE_MODE_DEFAULT).timeout(conc.Duration(millis = 10)).build();
	match opened {
		Ok(f) => {
			val _ = f.close();
			return 0;
		},
		Err(_) => { return 2; },
		default => { return 3; }
	}
}
	"""
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	_write_file(main_path, main_src)
	rc, payload = _run_driftc_json(["-M", str(root), str(main_path)], capsys)
	assert rc == 0, payload


def test_std_io_builder_fluent_chain_compiles(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	main_src = """
module m_main;

import std.io as io;
import std.concurrent as conc;

pub fn main() nothrow -> Int{
	val out = io.stdout_builder().timeout(conc.Duration(millis = 25)).build();
	var b = io.buffer(1);
	io.buffer_write(b, 0, cast<Byte>(65));
	val _ = out.write(b);
	val opened = io.file_builder("tmp.bin").write(true).create(true).truncate(true).mode(io.FILE_MODE_DEFAULT).timeout(conc.Duration(millis = 25)).build();
	match opened {
		Ok(f) => {
			val _ = f.close();
			return 0;
		},
		Err(_) => { return 2; },
		default => { return 3; }
	}
}
	"""
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	_write_file(main_path, main_src)
	rc, payload = _run_driftc_json(["-M", str(root), str(main_path)], capsys)
	assert rc == 0, payload


def test_std_io_error_code_helpers_compile(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	main_src = """
module m_main;

import std.io as io;

pub fn main() nothrow -> Int{
	val e = io.IoError(kind = io.IO_ERROR_KIND_ERRNO, code = io.IO_ERR_EOF);
	if io.is_eof_error(e) { return 0; }
	return 1;
}
	"""
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	_write_file(main_path, main_src)
	rc, payload = _run_driftc_json(["-M", str(root), str(main_path)], capsys)
	assert rc == 0, payload


def test_std_io_file_builder_append_mode_chain_no_hang(tmp_path: Path) -> None:
	main_src = """
module m_main;

import std.io as io;
import std.concurrent as conc;
import std.core as core;

pub fn main() nothrow -> Int {
	return try run_main() catch { 1 };
}

// Slice 5 (pub-error track): `or_throw()` requires Err to be `pub error`.
// `std.io.IoError` is a `pub variant` so it can't be the Err of or_throw
// under Phase 5a strict enforcement.  This test pins parser non-hang on
// a long fluent builder chain — the or_throw() shape is incidental to
// that purpose, so we match on the Result instead.
fn run_main() throws -> Int {
	val t = conc.Duration(millis = 1000);
	match io.file_builder("tmp_chain.bin").read(true).write(true).create(true).truncate(true).append(false).mode(io.FILE_MODE_DEFAULT).timeout(t).build() {
		Ok(opened) => {
			match opened.close() {
				Ok(_) => { return 0; },
				Err(_) => { return 1; },
			}
		},
		Err(_) => { return 1; },
	}
}
	"""
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	_write_file(main_path, main_src)
	cmd = [
		sys.executable,
		"-m",
		"lang.driftc",
		"-M",
		str(root),
		str(main_path),
		"--json",
	]
	try:
		res = subprocess.run(cmd, cwd=Path(__file__).parents[3], capture_output=True, text=True, timeout=sanitizer_timeout(20))
	except subprocess.TimeoutExpired:
		pytest.fail("driftc compile timed out for file_builder append/mode fluent chain")
	payload = json.loads(res.stdout) if res.stdout.strip() else {}
	assert res.returncode == 0, payload