# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Stdlib Err carrier migration to `pub error` (Slice 5 follow-up).

Phase 5a of the pub-error track requires `Result<T, E>.or_throw()`
to have `E` as a `pub error` type.  Several public stdlib Result
carriers still use the legacy `pub struct E + implement Throw for E`
shape — that blocks downstream throws / auto-try chaining over those
APIs.  Reported by drift-net-tls on 0.31.66+abi14:

	`or_throw()` requires the Err type of `Result<T, E>` to be a
	`pub error` type (got `std.parse.ParseError`)
	[E_OR_THROW_NOT_ERROR_TYPE]

	`or_throw()` requires the Err type of `Result<T, E>` to be a
	`pub error` type (got `std.net.NetError`)
	[E_OR_THROW_NOT_ERROR_TYPE]

This test pins the contract: every public stdlib Result error carrier
that downstream packages auto-try over MUST be a `pub error`.  Probes
exercise the auto-try shape inside a `throws` function — that's the
shape that triggers the `or_throw` synthesis path the checker rejects
when E is not a `pub error`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


def _compile(tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str) -> tuple[int, list[dict]]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	rc = driftc_main(["--stdlib-root", "stdlib", "--test-build-only", str(src), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	errs = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	return rc, errs


def _ok(rc: int, errs: list[dict], label: str) -> None:
	assert rc == 0, (
		f"{label}: rc={rc}, errs:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)


# ── Probe 1 ─ std.parse.parse_int auto-try inside throws ───────────


def test_parse_parse_int_auto_try(tmp_path, capsys):
	"""`parse.parse_int(s)` returning `Result<Int, ParseError>` used
	with auto-try inside a `throws` function.  Auto-try lowers to
	`or_throw`, which requires ParseError to be a `pub error`."""
	rc, errs = _compile(tmp_path, capsys, """
module main;

import std.core as core;
import std.parse as parse;

fn read_int(s: String) throws -> Int {
	return parse.parse_int(s);
}

pub fn main() nothrow -> Int {
	try {
		return read_int("123");
	} catch {
		return -1;
	}
}
""")
	_ok(rc, errs, "parse.parse_int auto-try requires ParseError pub error")


# ── Probe 2 ─ std.net.connect auto-try inside throws ───────────────


def test_net_connect_auto_try(tmp_path, capsys):
	"""`net.connect(&addr, timeout)` returning
	`Result<TcpStream, NetError>` used with auto-try inside a
	`throws` function.  Auto-try lowers to `or_throw`, which requires
	NetError to be a `pub error`."""
	rc, errs = _compile(tmp_path, capsys, """
module main;

import std.core as core;
import std.net as net;
import std.concurrent as conc;

fn open_conn() throws -> net.TcpStream {
	val addr = net.socket_addr("127.0.0.1", 0);
	val timeout = conc.Duration(millis = 100);
	return net.connect(&addr, timeout);
}

pub fn main() nothrow -> Int {
	try {
		val _s = open_conn();
		return 0;
	} catch {
		return 1;
	}
}
""")
	_ok(rc, errs, "net.connect auto-try requires NetError pub error")


# ── Probe 3 ─ std.io read auto-try inside throws ───────────────────


def test_io_read_auto_try(tmp_path, capsys):
	"""`io.input_read` returning `Result<Int, IoError>` used with
	auto-try inside a `throws` function.  Requires IoError to be a
	`pub error`."""
	rc, errs = _compile(tmp_path, capsys, """
module main;

import std.core as core;
import std.io as io;
import std.concurrent as conc;

fn read_some(stdin: &io.InputStream, buf: &mut io.Buffer) throws -> Int {
	val timeout = conc.Duration(millis = 0);
	return io.input_read(stdin, buf, timeout);
}

pub fn main() nothrow -> Int {
	return 0;
}
""")
	_ok(rc, errs, "io.input_read auto-try requires IoError pub error")


# ── Probe 4 ─ std.text.utf8_from_bytes auto-try ────────────────────


def test_text_utf8_auto_try(tmp_path, capsys):
	"""`text.utf8_from_bytes` returning `Result<String, Utf8Error>`
	used with auto-try."""
	rc, errs = _compile(tmp_path, capsys, """
module main;

import std.core as core;
import std.text as text;

fn decode(bytes: &Array<Byte>) throws -> String {
	return text.utf8_from_bytes(bytes);
}

pub fn main() nothrow -> Int {
	var bytes: Array<Byte> = [];
	try {
		val _s = decode(&bytes);
		return 0;
	} catch {
		return 1;
	}
}
""")
	_ok(rc, errs, "text.utf8_from_bytes auto-try requires Utf8Error pub error")


# ── Probe 5 ─ std.codec.hex_decode auto-try ────────────────────────


def test_codec_hex_decode_auto_try(tmp_path, capsys):
	"""`codec.hex_decode` returning `Result<Array<Byte>, CodecError>`
	used with auto-try."""
	rc, errs = _compile(tmp_path, capsys, """
module main;

import std.core as core;
import std.codec as codec;

fn decode_hex(s: &String) throws -> Array<Byte> {
	return codec.hex_decode(s);
}

pub fn main() nothrow -> Int {
	val s = "abcd";
	try {
		val _b = decode_hex(&s);
		return 0;
	} catch {
		return 1;
	}
}
""")
	_ok(rc, errs, "codec.hex_decode auto-try requires CodecError pub error")


# ── Probe 6 ─ std.time.parse_iso8601_utc auto-try ──────────────────


def test_time_parse_iso8601_auto_try(tmp_path, capsys):
	"""`time.parse_iso8601_utc` returning
	`Result<UtcTimestamp, TimeParseError>` used with auto-try."""
	rc, errs = _compile(tmp_path, capsys, """
module main;

import std.core as core;
import std.time as time;

fn parse_ts(s: String) throws -> time.UtcTimestamp {
	return time.parse_iso8601_utc(s);
}

pub fn main() nothrow -> Int {
	try {
		val _t = parse_ts("2026-01-01T00:00:00Z");
		return 0;
	} catch {
		return 1;
	}
}
""")
	_ok(rc, errs, "time.parse_iso8601_utc auto-try requires TimeParseError pub error")


# ── Probe 7 ─ std.json.parse auto-try ──────────────────────────────


def test_json_parse_auto_try(tmp_path, capsys):
	"""`json.parse(&s)` returning `Result<JsonNode, JsonErrorData>`
	used with auto-try."""
	rc, errs = _compile(tmp_path, capsys, """
module main;

import std.core as core;
import std.json as json;

fn parse_json(s: &String) throws -> json.JsonNode {
	return json.parse(s);
}

pub fn main() nothrow -> Int {
	val s = "{}";
	try {
		val _n = parse_json(&s);
		return 0;
	} catch {
		return 1;
	}
}
""")
	_ok(rc, errs, "json.parse auto-try requires JsonErrorData pub error")
