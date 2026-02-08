# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	root = stdlib_root()
	args = list(argv)
	if root:
		args += ["--stdlib-root", str(root)]
	args += ["--dev"]
	args += ["--json"]
	rc = driftc_main(args)
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def test_std_log_builder_and_calls_compile(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main

import std.log as log;
import std.concurrent as conc;

fn main() nothrow -> Int {
	var cfg_builder = log.config_builder();
	cfg_builder.min_level(log.Level::Debug());
	cfg_builder.queue_capacity(2048);
	cfg_builder.backpressure_policy(log.BackpressurePolicy::BlockWithTimeout());
	cfg_builder.write_timeout(conc.Duration(millis = 50));
	cfg_builder.enqueue_timeout(conc.Duration(millis = 25));
	cfg_builder.sink(log.stderr_sink());
	cfg_builder.formatter(log.FormatterKind::JsonIso8601());
	val cfg = cfg_builder.build();
	if not log.init(cfg) {
		return 1;
	}

	if not log.info("auth-failed", {"attempts": 3, "status": 401}) {
		return 2;
	}

	val root = log.logger_main();
	var lib_builder = root.derive("auth-lib");
	lib_builder.min_level(log.Level::Debug());
	val lib = lib_builder.build();
	if not lib.debug("token-parse", {"attempts": 3, "status": 401}) {
		return 3;
	}

	if not log.flush(conc.Duration(millis = 100)) {
		return 4;
	}
	return 0;
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []


def test_std_log_inline_map_literal_attrs_infers_value_type(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main

import std.log as log;

fn main() nothrow -> Int {
	if not log.info("auth-failed", {"attempts": 3, "status": 401}) {
		return 1;
	}
	return 0;
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []


def test_std_log_user_type_can_be_attr_via_debuggable_impl(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main

import std.log as log;

struct User {
	id: Int
}

implement log.Debuggable for User {
	pub fn to_debug(self: &User) nothrow -> DiagnosticValue {
		return DiagnosticValue::Int(self.id);
	}
}

fn main() nothrow -> Int {
	if not log.info("user-created", {"user": User(id = 7)}) {
		return 1;
	}
	return 0;
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []
