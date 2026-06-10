# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Compile-level pins for the std.source + std.parse frontend toolkit.

The e2e suite (lang/tests/codegen/e2e/std_source_*, std_parse_token_stream)
exercises runtime behavior under memcheck; these driver tests pin the
*public surface*: that every exported type/fn names and compiles, that a
user TokenKind is implementable and usable generically, and that
ParseDiagnostic participates in the value APIs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


# NOTE: `--dev --json` is the TYPECHECK-phase diagnostics preview, not the full
# compile pipeline; it is used here only for the *positive* compile checks (a
# clean program type-checks clean).  The real ownership/leak gate for this
# module is the e2e suite (lang/tests/codegen/e2e/std_parse_token_stream et al.),
# which runs the full pipeline incl. MIR validation under memcheck.
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


def _compile(mod_root: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	paths = sorted(mod_root.rglob("*.drift"))
	return _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)


def test_public_types_compile(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	"""A program naming every exported source/parse frontend symbol
	compiles with zero diagnostics."""
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

import std.source as source;
import std.parse as parse;
import std.core as core;
import std.core.cmp as cmp;

variant Kind {
	Word,
	Punct,
}

implement cmp.Equatable for Kind {
	pub fn eq(self: &Kind, other: &Kind) nothrow -> Bool {
		match *self {
			Kind::Word => { match *other { Kind::Word => { return true; }, default => { return false; } } },
			Kind::Punct => { match *other { Kind::Punct => { return true; }, default => { return false; } } }
		}
	}
}

implement parse.TokenKind for Kind {
	pub fn describe(self: &Kind) nothrow -> String {
		match *self {
			Kind::Word => { return "word"; },
			Kind::Punct => { return "punct"; }
		}
	}
}

fn main() nothrow -> Int {
	// SourcePos / SourceSpan / pos_zero / span helpers
	val p: source.SourcePos = source.pos_zero();
	val sp: source.SourceSpan = source.SourceSpan(source_id = "m", start = p, end = p);
	val _len: Int = source.span_byte_len(&sp);
	val _empty: Bool = source.span_is_empty(&sp);

	// SourceCursor + constructors + methods
	var cur: source.SourceCursor = source.source_cursor_from_string("hi", "m");
	val _sc: Int = cur.peek();
	val _ad: Int = cur.advance();
	val _at: Bool = cur.at_end();
	val _here: source.SourceSpan = cur.span_here();

	// Token / ParseDiagnostic / TokenStream / token_stream
	var toks: Array<parse.Token<Kind>> = [];
	toks.push(parse.Token<type Kind>(kind = Kind::Word(), span = sp));
	var ts: parse.TokenStream<Kind> = parse.token_stream<type Kind>(move toks, sp);
	val _end: Bool = ts.at_end();
	var expected: Array<String> = [];
	expected.push("word");
	val d: parse.ParseDiagnostic = parse.parse_diagnostic("unexpected-token", sp, move expected, Optional<String>::None());
	val _code: String = d.code;
	return 0;
}
""".lstrip(),
	)
	rc, payload = _compile(mod_root, capsys)
	assert rc == 0, payload
	assert payload.get("diagnostics", []) == []


def test_tokenstream_expect_and_drop(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	"""expect() over a generic TokenKind type-checks cleanly, and a
	TokenStream constructed with a moved-in token sequence and then
	dropped compiles with zero diagnostics.

	This is a COMPILE-level check only (K is a payloadless variant; no
	program is run).  Runtime destruction of buffered tokens — including
	a token kind that owns a heap String — is covered by the memcheck
	e2e case `lang/tests/codegen/e2e/std_parse_token_stream`, not here."""
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

import std.source as source;
import std.parse as parse;
import std.core as core;
import std.core.cmp as cmp;

variant K {
	A,
	B,
}

implement cmp.Equatable for K {
	pub fn eq(self: &K, other: &K) nothrow -> Bool {
		match *self {
			K::A => { match *other { K::A => { return true; }, default => { return false; } } },
			K::B => { match *other { K::B => { return true; }, default => { return false; } } }
		}
	}
}

implement parse.TokenKind for K {
	pub fn describe(self: &K) nothrow -> String {
		match *self {
			K::A => { return "A"; },
			K::B => { return "B"; }
		}
	}
}

fn main() nothrow -> Int {
	val p = source.pos_zero();
	val sp = source.SourceSpan(source_id = "m", start = p, end = p);
	var toks: Array<parse.Token<K>> = [];
	toks.push(parse.Token<type K>(kind = K::A(), span = sp));
	toks.push(parse.Token<type K>(kind = K::B(), span = sp));
	var ts = parse.token_stream<type K>(move toks, sp);
	val want = K::A();
	match ts.expect(&want, "A") {
		core.Result::Ok(_) => { },
		core.Result::Err(_) => { return 1; }
	}
	// one token (B) still buffered; ts drops here.
	return 0;
}
""".lstrip(),
	)
	rc, payload = _compile(mod_root, capsys)
	assert rc == 0, payload
	assert payload.get("diagnostics", []) == []
