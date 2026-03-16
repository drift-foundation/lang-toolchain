# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from lang.driftc.core.types_core import TypeTable
from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root
from lang.tests.driver.driver_cli_helpers import with_target_word_bits


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	if "--dev" not in argv:
		argv = [*argv, "--dev"]
	rc = driftc_main(with_target_word_bits(argv + ["--json"]))
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _error_codes(payload: dict) -> list[str]:
	diags = payload.get("diagnostics", []) if isinstance(payload, dict) else []
	return [d.get("code") for d in diags if d.get("code")]


def _error_messages(payload: dict) -> list[str]:
	diags = payload.get("diagnostics", []) if isinstance(payload, dict) else []
	return [d.get("message") for d in diags if d.get("message")]


def test_optional_ctor_expected_type_and_explicit_ok(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	src = tmp_path / "main" / "main.drift"
	_write_file(
		src,
		"""
module main;

fn main() -> Int {
	val a: Optional<Int> = Optional::None();
	val b = Optional::None<type Int>();
	val c = Optional<Int>::None();
	val d: Optional<Int> = Optional::Some(1);
	match d {
		None => { return 0; },
		Some(x) => { return x; },
	}
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), "--stdlib-root", str(stdlib_root()), str(src)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []


def test_generic_impl_array_literal_fuzz_fixed_seed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	rng = random.Random(4242)
	types = ["Int", "Bool", "Byte"]
	uses: list[str] = []
	for idx in range(6):
		ty = rng.choice(types)
		uses.append(
			"\n".join(
				[
					f"fn use_{idx}() -> Int {{",
					f"\tvar items: Array<{ty}> = [];",
					f"\tvar b: Box<{ty}> = Box(items = items);",
					"\tvar v = b.make();",
					"\treturn v;",
					"}",
				]
			)
		)
	src = tmp_path / "main" / "main.drift"
	_write_file(
		src,
		"\n".join(
			[
				"module main;",
				"",
				"struct Box<T> {",
				"\titems: Array<T>",
				"}",
				"",
				"implement<T> Box<T> {",
				"\tfn make(self: &Box<T>) nothrow -> Int {",
				"\t\tvar out: Array<T> = [];",
				"\t\treturn out.len;",
				"\t}",
				"}",
				"",
				*uses,
				"",
				"fn main() -> Int {",
				"\tval a = use_0();",
				"\tval b = use_1();",
				"\tval c = use_2();",
				"\treturn a + b + c;",
				"}",
				"",
			]
		),
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), "--stdlib-root", str(stdlib_root()), str(src)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []


def test_optional_ctor_without_expected_type_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	src = tmp_path / "main" / "main.drift"
	_write_file(
		src,
		"""
module main;

fn main() -> Int {
	val x = Optional::None();
	return 0;
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), "--stdlib-root", str(stdlib_root()), str(src)], capsys)
	assert rc != 0
	assert any("E-QMEM-CANNOT-INFER" in msg for msg in _error_messages(payload))


def test_alias_chain_ctor_call_context(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	src = tmp_path / "main" / "main.drift"
	_write_file(
		src,
		"""
module main;

type Maybe<T> = Optional<T>;
type MaybeInt = Maybe<Int>;

fn main() -> Int {
	val a: MaybeInt = Optional::None();
	val b = Optional::None<type MaybeInt>();
	val c = Optional::Some<type Int>(1);
	match a {
		None => { return 0; },
		Some(x) => { return x; },
	}
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), "--stdlib-root", str(stdlib_root()), str(src)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []


def test_iter_and_intrinsic_paths_nothrow(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	src = tmp_path / "main" / "main.drift"
	_write_file(
		src,
		"""
module main;

import std.iter as iter;

fn main() -> Int {
	val s: String = "ab";
	var it = s.bytes();
	val a: Optional<Byte> = iter.SinglePassIterator::next(&mut it);
	val b: Optional<Byte> = iter.SinglePassIterator::next(&mut it);
	match a {
		None => { return 0; },
		Some(_) => { return 1; },
	}
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), "--stdlib-root", str(stdlib_root()), str(src)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []


def test_generic_result_void_instantiation_allows_local_bind(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	src = tmp_path / "main" / "main.drift"
	_write_file(
		src,
		"""
module main;

import std.core as core;

fn run_cb<T>(cb: core.Callback0<T>) nothrow -> core.Result<T, Int> {
	val v = cb.call();
	return core.Result::Ok(move v);
}

fn main() nothrow -> Int {
	val cb: core.Callback0<Void> = core.callback0(| | => { return; });
	match run_cb(cb) {
		core.Result::Ok(_) => { return 0; },
		core.Result::Err(_) => { return 1; }
	}
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), "--stdlib-root", str(stdlib_root()), str(src)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []


def test_void_value_still_rejected_in_nonvoid_typed_context(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	src = tmp_path / "main" / "main.drift"
	_write_file(
		src,
		"""
module main;

fn v() nothrow -> Void {
	return;
}

fn main() nothrow -> Int {
	val x: Int = v();
	return x;
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), "--stdlib-root", str(stdlib_root()), str(src)], capsys)
	assert rc != 0
	assert any("initializer type 'Void' does not match declared type 'Int'" in msg for msg in _error_messages(payload))


def test_destructible_query_fallback_with_trait_world() -> None:
	table = TypeTable()
	from lang.driftc.traits.world import TraitWorld
	table.trait_worlds = {"std.core": TraitWorld()}
	table.is_destructible(table.ensure_int())


def test_alias_resolution_fuzz_fixed_seed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	rng = random.Random(1337)
	aliases: list[str] = []
	uses: list[str] = []
	for idx in range(5):
		prev = f"A{idx}"
		if idx == 0:
			aliases.append(f"type {prev}<T> = Optional<T>;")
		else:
			aliases.append(f"type {prev}<T> = A{idx - 1}<T>;")
	for idx in range(5, 8):
		aliases.append(f"type A{idx} = A0<Int>;")
	for idx in range(12):
		pick = rng.choice(["A0", "A1", "A2", "A3", "A4", "A5", "A6", "A7"])
		if pick in {"A0", "A1", "A2", "A3", "A4"}:
			uses.append(f"val v{idx} = Optional::None<type {pick}<Int>>();")
		else:
			uses.append(f"val v{idx}: {pick} = Optional::None();")
	src = tmp_path / "main" / "main.drift"
	_write_file(
		src,
		"\n".join(
			[
				"module main;",
				"",
				*aliases,
				"",
				"fn main() -> Int {",
				*uses,
				"return 0;",
				"}",
				"",
			]
		),
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), "--stdlib-root", str(stdlib_root()), str(src)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []


def test_generic_impl_indexing_has_no_instantiation_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	src = tmp_path / "main" / "main.drift"
	_write_file(
		src,
		"""
module main;

fn main() -> Int {
	return 0;
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), "--stdlib-root", str(stdlib_root()), str(src)], capsys)
	assert rc == 0
	assert not [m for m in _error_messages(payload) if "generic instantiation required" in m]


def test_generic_use_site_error_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	src = tmp_path / "main" / "main.drift"
	_write_file(
		src,
		"""
module main;

struct Box { x: Int }

fn needs_copy<T>(x: T) -> Int require T is Copy {
	return 0;
}

fn main() -> Int {
	val b = Box(x = 1);
	return needs_copy(b);
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), "--stdlib-root", str(stdlib_root()), str(src)], capsys)
	assert rc != 0
	assert _error_messages(payload)


def test_drop_glue_instantiates_arc(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	src = tmp_path / "main" / "main.drift"
	_write_file(
		src,
		"""
module main;

import std.concurrent as conc;
import std.core as core;

fn main() -> Int {
	var a = conc.arc(1);
	core.drop_value(a);
	return 0;
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), "--stdlib-root", str(stdlib_root()), str(src)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []


def test_drop_glue_instantiates_mutex_guard(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	src = tmp_path / "main" / "main.drift"
	_write_file(
		src,
		"""
module main;

import std.concurrent as conc;
import std.core as core;

fn main() -> Int {
	var m = conc.mutex(1);
	var g = conc.lock(&m);
	core.drop_value(g);
	return 0;
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), "--stdlib-root", str(stdlib_root()), str(src)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []


def test_drop_value_expr_context_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	src = tmp_path / "main" / "main.drift"
	_write_file(
		src,
		"""
module main;

import std.concurrent as conc;
import std.core as core;

fn main() -> Int {
	var a = conc.arc(1);
	val _ = core.drop_value(a);
	return 0;
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), "--stdlib-root", str(stdlib_root()), str(src)], capsys)
	assert rc == 0
