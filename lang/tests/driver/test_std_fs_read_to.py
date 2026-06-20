# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: std.fs.read_to_bytes / read_to_string — whole-file helpers.

Pins the contract:
1. Empty file, small one-chunk file, multi-chunk file (> 64 KiB chunk).
2. `max_bytes` mandatory & in bytes: exact cap accepted; over-cap → `too-large`;
   `max_bytes == 0` accepts only an empty file (non-empty → `too-large`);
   `max_bytes < 0` → `invalid-argument` (no open).
3. `read_to_string` UTF-8 validation: invalid UTF-8 → `invalid-utf8`; the same
   bytes are accepted by `read_to_bytes` (arbitrary bytes).
4. Ownership: the returned `String` / `Array<Byte>` is owned — it can be moved
   through a struct and an array and torn down leak/double-free clean (valgrind).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout, asan_active, valgrind_cmd

_VALGRIND_SKIP = pytest.mark.skipif(
	shutil.which("valgrind") is None or asan_active(),
	reason="valgrind requires a non-ASan binary (ASan shadow memory collides)",
)

ROOT = Path(__file__).resolve().parents[3]


def _compile(tmp_path: Path, source: str, name: str = "test_bin") -> Path:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / name
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:800]}"
	assert out.exists()
	return out


def _fixtures(tmp_path: Path) -> dict[str, Path]:
	good = tmp_path / "good.txt"
	good.write_bytes(b"hello world\nsecond line\n")  # 24 bytes
	empty = tmp_path / "empty.txt"
	empty.write_bytes(b"")
	bad = tmp_path / "bad.txt"
	bad.write_bytes(b"abc\xff\xfedef")  # 8 bytes, invalid UTF-8
	multi = tmp_path / "multi.txt"
	multi.write_bytes(b"A" * (64 * 1024 + 1000))  # > one 65536 chunk
	return {"good": good, "empty": empty, "bad": bad, "multi": multi}


# Returns 0 iff every contract assertion holds; otherwise a bitmask of the
# failing checks (so a failure pinpoints which clause regressed).
_CONTRACT_SOURCE = """\
module main;
import std.core as core;
import std.fs as fs;
import std.concurrent as conc;

const GOOD_LEN: Int = 24;
const MULTI_LEN: Int = {multi_len};

pub fn main() nothrow -> Int {{
\tval t = conc.Duration(millis = 5000);
\tvar fails = 0;

\t// 1. small one-chunk: read_to_string + read_to_bytes
\tmatch fs.read_to_string("{good}", 1000000, t) {{
\t\tcore.Result::Ok(s) => {{ if s.byte_length() != GOOD_LEN {{ fails = fails + 1; }} }},
\t\tcore.Result::Err(e) => {{ fails = fails + 2; }}
\t}}
\tmatch fs.read_to_bytes("{good}", 1000000, t) {{
\t\tcore.Result::Ok(b) => {{ if b.len() != GOOD_LEN {{ fails = fails + 4; }} }},
\t\tcore.Result::Err(e) => {{ fails = fails + 8; }}
\t}}

\t// 2. multi-chunk (> 64 KiB)
\tmatch fs.read_to_bytes("{multi}", 1000000, t) {{
\t\tcore.Result::Ok(b) => {{ if b.len() != MULTI_LEN {{ fails = fails + 16; }} }},
\t\tcore.Result::Err(e) => {{ fails = fails + 32; }}
\t}}

\t// 3. empty file: read_to_string Ok empty; max_bytes==0 empty Ok
\tmatch fs.read_to_string("{empty}", 1000000, t) {{
\t\tcore.Result::Ok(s) => {{ if s.byte_length() != 0 {{ fails = fails + 64; }} }},
\t\tcore.Result::Err(e) => {{ fails = fails + 128; }}
\t}}
\tmatch fs.read_to_bytes("{empty}", 0, t) {{
\t\tcore.Result::Ok(b) => {{ if b.len() != 0 {{ fails = fails + 256; }} }},
\t\tcore.Result::Err(e) => {{ fails = fails + 512; }}
\t}}

\t// 4. exact max_bytes accepted
\tmatch fs.read_to_bytes("{good}", GOOD_LEN, t) {{
\t\tcore.Result::Ok(b) => {{ if b.len() != GOOD_LEN {{ fails = fails + 1024; }} }},
\t\tcore.Result::Err(e) => {{ fails = fails + 2048; }}
\t}}

\t// 5. over-cap -> too-large; max_bytes==0 non-empty -> too-large
\tmatch fs.read_to_string("{good}", 5, t) {{
\t\tcore.Result::Ok(s) => {{ fails = fails + 4096; }},
\t\tcore.Result::Err(e) => {{ if (e.kind + "") != "too-large" {{ fails = fails + 8192; }} }}
\t}}
\tmatch fs.read_to_bytes("{good}", 0, t) {{
\t\tcore.Result::Ok(b) => {{ fails = fails + 16384; }},
\t\tcore.Result::Err(e) => {{ if (e.kind + "") != "too-large" {{ fails = fails + 32768; }} }}
\t}}

\t// 6. negative cap -> invalid-argument (no open)
\tmatch fs.read_to_bytes("{good}", -1, t) {{
\t\tcore.Result::Ok(b) => {{ fails = fails + 65536; }},
\t\tcore.Result::Err(e) => {{ if (e.kind + "") != "invalid-argument" {{ fails = fails + 131072; }} }}
\t}}

\t// 7. invalid UTF-8: read_to_string -> invalid-utf8; read_to_bytes accepts bytes
\tmatch fs.read_to_string("{bad}", 1000000, t) {{
\t\tcore.Result::Ok(s) => {{ fails = fails + 262144; }},
\t\tcore.Result::Err(e) => {{ if (e.kind + "") != "invalid-utf8" {{ fails = fails + 524288; }} }}
\t}}
\tmatch fs.read_to_bytes("{bad}", 1000000, t) {{
\t\tcore.Result::Ok(b) => {{ if b.len() != 8 {{ fails = fails + 1048576; }} }},
\t\tcore.Result::Err(e) => {{ fails = fails + 2097152; }}
\t}}

\treturn fails;
}}
"""


# Ownership: move the returned String / Array<Byte> through a struct and an
# array, then tear down. Must be leak/double-free clean under valgrind.
_OWNERSHIP_SOURCE = """\
module main;
import std.core as core;
import std.fs as fs;
import std.concurrent as conc;

struct Holder(name: String, body: String, raw: Array<Byte>);

fn load(path: String) throws -> Holder {{
\tval t = conc.Duration(millis = 5000);
\tval s = fs.read_to_string(path, 1000000, t).or_throw();
\tval b = fs.read_to_bytes(path, 1000000, t).or_throw();
\treturn Holder(name = path + "", body = move s, raw = move b);
}}

fn run(path: String, n: Int) throws -> Int {{
\tvar hs: Array<Holder> = [];
\tvar i = 0;
\twhile i < n {{ hs.push(load(path)); i = i + 1; }}
\tvar tot = 0;
\tvar j = 0;
\twhile j < hs.len() {{ tot = tot + hs[j].body.byte_length() + hs[j].raw.len(); j = j + 1; }}
\treturn tot;
}}

pub fn main() nothrow -> Int {{
\tvar r = 0;
\ttry {{ r = run("{good}", 5); }} catch {{ r = 0; }}
\tif r > 0 {{ return 0; }}
\treturn 1;
}}
"""


def test_read_to_contract(tmp_path: Path) -> None:
	fx = _fixtures(tmp_path)
	multi_len = fx["multi"].stat().st_size
	source = _CONTRACT_SOURCE.format(
		good=fx["good"], empty=fx["empty"], bad=fx["bad"], multi=fx["multi"],
		multi_len=multi_len,
	)
	binary = _compile(tmp_path, source)
	res = subprocess.run([str(binary)], capture_output=True, text=True,
		timeout=sanitizer_timeout(60))
	assert res.returncode == 0, (
		f"contract checks failed (bitmask={res.returncode}); "
		f"stderr: {res.stderr[:400]}"
	)


def test_read_to_ownership_moves(tmp_path: Path) -> None:
	fx = _fixtures(tmp_path)
	source = _OWNERSHIP_SOURCE.format(good=fx["good"])
	binary = _compile(tmp_path, source)
	res = subprocess.run([str(binary)], capture_output=True, text=True,
		timeout=sanitizer_timeout(60))
	assert res.returncode == 0, f"ownership move run failed: {res.stderr[:400]}"


@_VALGRIND_SKIP
def test_read_to_ownership_memcheck(tmp_path: Path) -> None:
	fx = _fixtures(tmp_path)
	source = _OWNERSHIP_SOURCE.format(good=fx["good"])
	binary = _compile(tmp_path, source, name="mc_bin")
	res = subprocess.run(
		valgrind_cmd("--error-exitcode=99", "--leak-check=full",
			"--errors-for-leak-kinds=definite,indirect", str(binary)),
		capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert res.returncode != 99, (
		f"valgrind found leaks/errors in read_to ownership path:\n{res.stderr[:1000]}"
	)
	assert res.returncode == 0, f"program failed under valgrind: {res.stderr[:400]}"
