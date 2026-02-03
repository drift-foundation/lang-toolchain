# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

from lang2.driftc.driftc import main as driftc_main


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def test_match_stmt_missing_return_repro(tmp_path: Path, capsys) -> None:
	source = """
module m_main

import std.io as io;
import std.core as core;
import std.concurrent as conc;

fn main() nothrow -> Int {
	val w_opts = io.OpenOptions(read = false, write = true, create = true, truncate = true, append = false, mode = io.FILE_MODE_DEFAULT);
	val t = conc.Duration(millis = 1000);
	var w: core.Result<io.File, io.IoError> = io.open("io_test_file.bin", &w_opts, t);
	match w {
		Ok(v) => {
			var buf = io.buffer(3);
			io.buffer_write(&mut buf, 0, cast<Byte>(65));
			io.buffer_write(&mut buf, 1, cast<Byte>(66));
			io.buffer_write(&mut buf, 2, cast<Byte>(67));
			val wres = v.write(&buf, t);
			val wok = match wres {
				Ok(n) => { (n != 3) ? 1 : 0 },
				default => { 2 }
			};
			if wok != 0 {
				return wok;
			}
			val c = v.close(t);
			val ccode = match c {
				Ok(_) => { 0 },
				default => { 3 }
			};
			if ccode != 0 {
				return ccode;
			}
		},
		default => { return 4; }
	}
	val r_opts = io.OpenOptions(read = true, write = false, create = false, truncate = false, append = false, mode = 0);
	var r: core.Result<io.File, io.IoError> = io.open("io_test_file.bin", &r_opts, t);
	match r {
		Ok(v2) => {
			var buf2 = io.buffer(3);
			val rres = v2.read(&mut buf2, t);
			val n = match rres {
				Ok(v) => { v },
				default => { 5 }
			};
			if n == 5 {
				return 5;
			}
			if n != 3 {
				return 6;
			}
			val c2 = v2.close(t);
			val c2code = match c2 {
				Ok(_) => { 0 },
				default => { 10 }
			};
			if c2code != 0 {
				return c2code;
			}
			return 0;
		},
		default => { return 11; }
	}
}
"""
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	_write_file(main_path, source)
	rc = driftc_main(["-M", str(root), str(main_path), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	assert rc == 0, payload
