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


def test_std_sync_atomic_api_compiles(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main

import std.sync as sync;

fn main() nothrow -> Int {
	var a = sync.atomic_int(1);
	val old1 = a.exchange(2, sync.MemoryOrder::SeqCst());
	if old1 != 1 {
		return 1;
	}
	val ok = a.compare_exchange(2, 3, sync.MemoryOrder::SeqCst(), sync.MemoryOrder::Acquire());
	if not ok {
		return 2;
	}
	val old2 = a.fetch_sub(1, sync.MemoryOrder::Relaxed());
	if old2 != 3 {
		return 3;
	}
	return 0;
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []


def test_std_sync_atomic_bool_fetch_sub_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main

import std.sync as sync;

fn main() nothrow -> Int {
	var a = sync.atomic_bool(false);
	val _ = a.fetch_sub(true, sync.MemoryOrder::Relaxed());
	return 0;
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc != 0
	msgs = [d.get("message", "") for d in payload.get("diagnostics", [])]
	assert any("no matching method 'fetch_sub'" in m for m in msgs)


def test_std_sync_compare_exchange_order_matrix_compiles(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main

import std.sync as sync;

fn main() nothrow -> Int {
	var a = sync.atomic_int(1);
	// Valid order pairs (compile + runtime path available).
	val _v1 = a.compare_exchange(1, 2, sync.MemoryOrder::Relaxed(), sync.MemoryOrder::Relaxed());
	val _v2 = a.compare_exchange(2, 3, sync.MemoryOrder::Acquire(), sync.MemoryOrder::Acquire());
	val _v3 = a.compare_exchange(3, 4, sync.MemoryOrder::Release(), sync.MemoryOrder::Relaxed());
	val _v4 = a.compare_exchange(4, 5, sync.MemoryOrder::AcqRel(), sync.MemoryOrder::Acquire());
	val _v5 = a.compare_exchange(5, 6, sync.MemoryOrder::SeqCst(), sync.MemoryOrder::SeqCst());

	// Invalid failure orders are accepted by API shape and handled by runtime guard (return false).
	val _i1 = a.compare_exchange(6, 7, sync.MemoryOrder::Relaxed(), sync.MemoryOrder::Release());
	val _i2 = a.compare_exchange(6, 7, sync.MemoryOrder::SeqCst(), sync.MemoryOrder::AcqRel());
	return 0;
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []


def test_std_sync_fetch_wrap_semantics_contract_compiles(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main

import std.sync as sync;

fn main() nothrow -> Int {
	// API contract: fetch_add/fetch_sub overflow semantics are wrapping, not trapping.
	// This is a runtime semantics pin; checker should accept boundary-value usage.
	val max_i: Int = 9223372036854775807;
	val min_i: Int = -9223372036854775808;
	val max_u: Uint = cast<Uint>(18446744073709551615);

	var ai = sync.atomic_int(max_i);
	val _old_i1 = ai.fetch_add(1, sync.MemoryOrder::Relaxed());
	val _old_i2 = ai.fetch_sub(1, sync.MemoryOrder::Relaxed());
	if min_i == 0 {
		return 1;
	}

	var au = sync.atomic_uint(max_u);
	val _old_u1 = au.fetch_add(cast<Uint>(1), sync.MemoryOrder::Relaxed());
	val _old_u2 = au.fetch_sub(cast<Uint>(1), sync.MemoryOrder::Relaxed());
	return 0;
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []
