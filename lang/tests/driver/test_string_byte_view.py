# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""string-view-performance phase: StringByteView semantics, adoption,
offset-contract, and unwind pins (string-view-performance phase,
0.33.88 — design record summarized in doc/history.md).

Four full compile-AND-run fixtures:

  1. SEMANTICS — construction/bounds (subtraction form, offset=start),
     Result-returning byte_at (exact Err(IndexError), view-relative), subview/dup/eq/
     search (index_of naming, view-relative, -1 absent, empty-needle),
     to_string (byte-exact; empty -> singleton ""), consuming
     iterator, matcher-authority accessors, composed bulk window,
     split_views parity (empty delimiter/absent/empty input/empty
     fields), and views OUTLIVING their source binding — all on
     HEAP-BACKED strings (literal backings are STATIC/immortal and
     would prove retain behavior vacuously).

  2. ADOPTION — relative offsets in byte-range AND view numeric
     parsers; SourceCursor.slice_view ok + error-code parity;
     JsonDoc.byte_range_view + LocatedCursor.raw_view; the
     _parse_string escape-free fast path vs escaped fallback
     equivalence; std.regex view surface: view-relative match
     offsets, match_view/match_subview round-trips, the
     fabricated-RegexMatch negative matrix (inverted/negative/
     out-of-range -> checked TextError, never UB/ICE), empty match ->
     valid empty view, and ^$ anchoring at VIEW boundaries.

  3. OFFSET TABLE — every pinned offset-contract row (invalid-range@0
     positionless; empty range/sign-only/invalid-digit/overflow/
     underflow/invalid-datatype, all RELATIVE) across the byte-range
     and view families, plus cross-family happy-path agreement.
     This pins the documented-contract behavior correction recorded
     in doc/history.md 0.33.88 (callers previously observed ABSOLUTE
     offsets).

  4. FORCED THROW — with_view_bytes_throw unwinds 100 times through
     the nested-callback composition; the view and backing stay fully
     usable (exact retain/env-alloc balance is proven separately by
     the counting harness; leak-cleanliness by the memcheck fixture).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]

SEMANTICS_SRC = r"""
module main;

import std.core as core;
import std.text as text;
import std.mem as mem;
import std.console as cons;
import std.format as fmt;

pub fn main() nothrow -> Int {
	var s = "hello ";
	s = s + "world and moon";   // heap-backed, non-static

	// construction + bounds
	var v = text.byte_view_all(&s);
	match text.byte_view(&s, 6, 5) {
		Ok(x) => { v = move x; },
		Err(e) => { return 1; }
	}
	if v.byte_length() != 5 { return 2; }
	if v.is_empty() { return 3; }
	match text.byte_view(&s, 6, 999) { Ok(x) => { return 4; }, Err(e) => {
		if e.offset != 6 { return 5; }
	} }

	// reads — Result API: bounds failure is data, nothrow caller.
	var b0 = cast<Byte>(0);
	match v.byte_at(0) {
		Ok(b) => { b0 = b; },
		Err(e) => { return 6; }
	}
	if cast<Int>(b0) != 119 { return 7; }

	// subview + dup + eq + search
	val all = text.byte_view_all(&s);
	var v2 = text.byte_view_all(&s);
	match all.subview(6, 5) {
		Ok(x) => { v2 = move x; },
		Err(e) => { return 8; }
	}
	if not v.eq_view(&v2) { return 9; }
	if not v.eq_string(&"world") { return 10; }
	if not all.starts_with(&"hello") { return 11; }
	if not all.ends_with(&"moon") { return 12; }
	if all.index_of(&"world") != 6 { return 13; }
	if all.index_of_view(&v) != 6 { return 14; }
	val d = v.dup();
	if not d.eq_view(&v) { return 15; }

	// to_string
	val owned = v.to_string();
	if owned != "world" { return 16; }
	var empty = text.byte_view_all(&s);
	match all.subview(3, 0) {
		Ok(x) => { empty = move x; },
		Err(e) => { return 17; }
	}
	if empty.to_string() != "" { return 18; }

	// iterator (consumes; dup first)
	var it = v.dup().bytes();
	var sum = 0;
	var going = true;
	while going {
		match it.next() {
			Some(b) => { sum = sum + cast<Int>(b); },
			None() => { going = false; }
		}
	}
	if sum != 552 { return 19; }

	// _byte_source window (EXPORTED-INTERNAL matcher/parser plumbing):
	// range-guarded reads, borrows only — bounded by the VIEW, not the
	// backing.
	val bsrc = text._byte_source(&v);
	if bsrc.size() != 5 { return 20; }
	if cast<Int>(bsrc.read(0)) != 119 { return 21; }
	val ball = text._byte_source_all(&s);
	if ball.size() != s.byte_length() { return 32; }

	// bulk window
	val body: core.Callback2<mem.Ptr<Byte>, Int, Int> =
		core.callback2(|p: mem.Ptr<Byte>, n: Int| => {
			var acc = 0;
			var i = 0;
			while i < n {
				acc = acc + cast<Int>(mem.ptr_read<type Byte>(mem.ptr_offset<type Byte>(p, i)));
				i = i + 1;
			}
			acc
		});
	val bulk = text.with_view_bytes<type Int, core.Callback2<mem.Ptr<Byte>, Int, Int> >(&v, move body);
	if bulk != 552 { return 22; }

	// split_views parity
	val parts = text.split_views(&s, &" ");
	if parts.len != 4 { return 23; }
	if not parts[0].eq_string(&"hello") { return 24; }
	if not parts[3].eq_string(&"moon") { return 25; }
	val none = text.split_views(&s, &"zzz");
	if none.len != 1 { return 26; }
	if not none[0].eq_string(&s) { return 27; }
	val emptyin = "";
	val ei = text.split_views(&emptyin, &",");
	if ei.len != 1 { return 28; }
	if not ei[0].is_empty() { return 29; }
	val perbyte = text.split_views(&"ab", &"");
	if perbyte.len != 2 { return 30; }

	// lifetime: views outlive the original binding's scope
	var held: Array<text.StringByteView> = [];
	{
		var tmp = "dyn ";
		tmp = tmp + "amic";
		held.push(text.byte_view_all(&tmp));
	}
	if not held[0].eq_string(&"dyn amic") { return 31; }

	cons.println("view smoke OK");
	return 0;
}
"""

ADOPTION_SRC = r"""
module main;

import std.core as core;
import std.text as text;
import std.parse as parse;
import std.source as source;
import std.json as json;
import std.regex as regex;
import std.console as cons;

pub fn main() nothrow -> Int {
	// heap-backed subject
	var s = "num=";
	s = s + "12345,x=alpha77,y=-42";

	// ── parse_*_view + §6a offsets ──
	var pv = text.byte_view_all(&s);
	var v_num = text.byte_view_all(&s);
	match text.byte_view(&s, 4, 5) { Ok(x) => { v_num = move x; }, Err(e) => { return 1; } }
	match parse.parse_int_view(&v_num) {
		Ok(n) => { if n != 12345 { return 2; } },
		Err(e) => { return 3; }
	}
	// invalid digit at view-relative 5 ("12345," view of len 6)
	var v_bad = text.byte_view_all(&s);
	match text.byte_view(&s, 4, 6) { Ok(x) => { v_bad = move x; }, Err(e) => { return 4; } }
	match parse.parse_int_view(&v_bad) {
		Ok(n) => { return 5; },
		Err(e) => { if e.tag != "invalid-digit" or e.offset != 5 { return 6; } }
	}
	// negative to unsigned: invalid-datatype at 0
	var v_neg = text.byte_view_all(&s);
	match text.byte_view(&s, s.byte_length() - 3, 3) { Ok(x) => { v_neg = move x; }, Err(e) => { return 7; } }
	match parse.parse_uint_view(&v_neg) {
		Ok(n) => { return 8; },
		Err(e) => { if e.tag != "invalid-datatype" or e.offset != 0 { return 9; } }
	}
	// §6a byte-range forms now relative
	var bytes = core.string_to_utf8_bytes(&s);
	match parse.parse_int_bytes(&bytes, 4, 10) {
		Ok(n) => { return 10; },
		Err(e) => { if e.tag != "invalid-digit" or e.offset != 5 { return 11; } }
	}
	match parse.parse_int_bytes(&bytes, 90, 95) {
		Ok(n) => { return 12; },
		Err(e) => { if e.tag != "invalid-range" or e.offset != 0 { return 13; } }
	}

	// ── SourceCursor.slice_view ──
	var src_txt = "let x = ";
	src_txt = src_txt + "answer;";
	val cur = source.source_cursor_from_string(src_txt.clone(), "m");
	match cur.slice_view(8, 14) {
		Ok(v) => { if not v.eq_string(&"answer") { return 14; } },
		Err(e) => { return 15; }
	}
	match cur.slice_view(8, 999) {
		Ok(v) => { return 16; },
		Err(e) => { if e.code != "invalid-slice-range" { return 17; } }
	}

	// ── json raw_view / byte_range_view + fast-path equivalence ──
	var doc_txt = "{\"name\": \"widget\", \"esc\": \"a\\nb\", \"n\": 7}";
	match json.parse_located(&doc_txt, &json.permissive()) {
		Ok(doc) => {
			match doc.at_pointer(&"/name") {
				Ok(c) => {
					val rv = c.raw_view();
					// span covers the raw token including quotes
					if not rv.eq_string(&"\"widget\"") { return 18; }
				},
				Err(e) => { return 19; }
			}
			match doc.byte_range_view(1, 6) {
				Ok(v) => { if not v.eq_string(&"\"name\"") { return 20; } },
				Err(e) => { return 21; }
			}
			match doc.byte_range_view(1, 9999) {
				Ok(v) => { return 22; },
				Err(e) => { }
			}
		},
		Err(e) => { return 23; }
	}
	// escape-free fast path + escaped fallback both correct
	match json.parse(&doc_txt) {
		Ok(node) => {
			match node {
				Object(fields) => {
					match fields.get(&"name") {
						Some(nv) => { match nv {
							String(sv) => { if sv != "widget" { return 24; } },
							default => { return 25; }
						} },
						None() => { return 26; }
					}
					match fields.get(&"esc") {
						Some(ev) => { match ev {
							String(sv) => { if sv.byte_length() != 3 { return 27; } },
							default => { return 28; }
						} },
						None() => { return 29; }
					}
				},
				default => { return 30; }
			}
		},
		Err(e) => { return 31; }
	}

	// ── regex view surface ──
	match regex.compile(&"[a-z]+[0-9]+") {
		Ok(re) => {
			// subject view over "x=alpha77" region: alpha77 at rel 2
			var vv = text.byte_view_all(&s);
			match text.byte_view(&s, 10, 9) { Ok(x) => { vv = move x; }, Err(e) => { return 32; } }
			if not regex.is_match_view(&re, &vv) { return 33; }
			match regex.find_first_view(&re, &vv) {
				Some(m) => {
					if m.start != 2 or m.end != 9 { return 34; }   // VIEW-relative
					match regex.match_subview(m, &vv) {
						Ok(mv) => { if not mv.eq_string(&"alpha77") { return 35; } },
						Err(e) => { return 36; }
					}
				},
				None() => { return 37; }
			}
			// String-form parity untouched
			match regex.find_first(&re, &s) {
				Some(m) => {
					match regex.match_view(m, &s) {
						Ok(mv) => { if not mv.eq_string(&"alpha77") { return 38; } },
						Err(e) => { return 39; }
					}
				},
				None() => { return 40; }
			}
			// fabricated matches: checked, never UB
			match regex.match_view(regex.RegexMatch(start = 5, end = 2), &s) {
				Ok(mv) => { return 41; },
				Err(e) => { if e.offset != 5 { return 42; } }
			}
			match regex.match_view(regex.RegexMatch(start = 0 - 3, end = 2), &s) {
				Ok(mv) => { return 43; },
				Err(e) => { }
			}
			match regex.match_view(regex.RegexMatch(start = 0, end = 99999), &s) {
				Ok(mv) => { return 44; },
				Err(e) => { }
			}
			// empty match span -> valid empty view
			match regex.match_view(regex.RegexMatch(start = 3, end = 3), &s) {
				Ok(mv) => { if not mv.is_empty() { return 45; } },
				Err(e) => { return 46; }
			}
			// anchors bind to view boundaries
			match regex.compile(&"^alpha77$") {
				Ok(re2) => {
					var av = text.byte_view_all(&s);
					match text.byte_view(&s, 12, 7) { Ok(x) => { av = move x; }, Err(e) => { return 47; } }
					if not regex.is_match_view(&re2, &av) { return 48; }
					if regex.is_match(&re2, &s) { return 49; }   // whole string: anchored fails
				},
				Err(e) => { return 50; }
			}
		},
		Err(e) => { return 51; }
	}

	cons.println("adoption smoke OK");
	return 0;
}
"""

OFFSET_TABLE_SRC = r"""
module main;

import std.core as core;
import std.text as text;
import std.parse as parse;

fn check_bytes_int(b: &Array<Byte>, s: Int, e: Int, tag: &String, off: Int, code: Int) nothrow -> Int {
	match parse.parse_int_bytes(b, s, e) {
		Ok(n) => { return code; },
		Err(er) => {
			if er.tag != *tag { return code; }
			if er.offset != off { return code + 1000; }
			return 0;
		}
	}
}

fn check_view_int(v: &text.StringByteView, tag: &String, off: Int, code: Int) nothrow -> Int {
	match parse.parse_int_view(v) {
		Ok(n) => { return code; },
		Err(er) => {
			if er.tag != *tag { return code; }
			if er.offset != off { return code + 1000; }
			return 0;
		}
	}
}

pub fn main() nothrow -> Int {
	// layout: 0:'+' 1:'x' 2:' ' 3..22:"-9223372036854775809" 23:' '
	// 24:'-' 25:'5' 26:' ' 27..31:"12a34" 32:' '
	// 33..52:"99999999999999999999" 53:' ' 54:'-' 55:' ' 56:'+'
	var s = "+x ";
	s = s + "-9223372036854775809 -5 12a34 99999999999999999999 - +";
	var b = core.string_to_utf8_bytes(&s);

	// invalid start/end -> invalid-range @0 (positionless)
	var r = check_bytes_int(&b, 0 - 1, 2, &"invalid-range", 0, 10); if r != 0 { return r; }
	r = check_bytes_int(&b, 5, 2, &"invalid-range", 0, 12); if r != 0 { return r; }
	r = check_bytes_int(&b, 0, 9999, &"invalid-range", 0, 14); if r != 0 { return r; }
	// empty range -> invalid-syntax @0
	r = check_bytes_int(&b, 3, 3, &"invalid-syntax", 0, 16); if r != 0 { return r; }
	// sign-only -> invalid-syntax @1
	r = check_bytes_int(&b, 0, 1, &"invalid-syntax", 1, 18); if r != 0 { return r; }
	// invalid digit at k -> k - start   ("+x": fails at rel 1)
	r = check_bytes_int(&b, 0, 2, &"invalid-digit", 1, 20); if r != 0 { return r; }
	// "12a34" at 27..32: invalid digit at rel 2
	r = check_bytes_int(&b, 27, 32, &"invalid-digit", 2, 22); if r != 0 { return r; }
	// underflow: "-9223372036854775809" at 3..23 trips at rel 19
	r = check_bytes_int(&b, 3, 23, &"underflow", 19, 24); if r != 0 { return r; }
	// overflow: "99999999999999999999" at 33..53 — the guard fires
	// BEFORE consuming the 19th digit (0-based rel 18)
	r = check_bytes_int(&b, 33, 53, &"overflow", 18, 26); if r != 0 { return r; }
	// negative to unsigned -> invalid-datatype @0 ("-5" at 24..26)
	match parse.parse_uint_bytes(&b, 24, 26) {
		Ok(n) => { return 28; },
		Err(er) => { if er.tag != "invalid-datatype" or er.offset != 0 { return 29; } }
	}

	// view family: same rows (no invalid-range; empty view -> invalid-syntax@0)
	var v = text.byte_view_all(&s);
	match text.byte_view(&s, 3, 0) { Ok(x) => { v = move x; }, Err(e) => { return 30; } }
	r = check_view_int(&v, &"invalid-syntax", 0, 31); if r != 0 { return r; }
	match text.byte_view(&s, 0, 1) { Ok(x) => { v = move x; }, Err(e) => { return 33; } }
	r = check_view_int(&v, &"invalid-syntax", 1, 34); if r != 0 { return r; }
	match text.byte_view(&s, 0, 2) { Ok(x) => { v = move x; }, Err(e) => { return 36; } }
	r = check_view_int(&v, &"invalid-digit", 1, 37); if r != 0 { return r; }
	match text.byte_view(&s, 27, 5) { Ok(x) => { v = move x; }, Err(e) => { return 39; } }
	r = check_view_int(&v, &"invalid-digit", 2, 40); if r != 0 { return r; }
	match text.byte_view(&s, 3, 20) { Ok(x) => { v = move x; }, Err(e) => { return 42; } }
	r = check_view_int(&v, &"underflow", 19, 43); if r != 0 { return r; }
	match text.byte_view(&s, 33, 20) { Ok(x) => { v = move x; }, Err(e) => { return 45; } }
	r = check_view_int(&v, &"overflow", 18, 46); if r != 0 { return r; }
	match text.byte_view(&s, 24, 2) { Ok(x) => { v = move x; }, Err(e) => { return 48; } }
	match parse.parse_uint_view(&v) {
		Ok(n) => { return 49; },
		Err(er) => { if er.tag != "invalid-datatype" or er.offset != 0 { return 50; } }
	}

	// happy paths across families agree
	match parse.parse_int_bytes(&b, 24, 26) { Ok(n) => { if n != 0 - 5 { return 51; } }, Err(e) => { return 52; } }
	match text.byte_view(&s, 24, 2) { Ok(x) => { v = move x; }, Err(e) => { return 53; } }
	match parse.parse_int_view(&v) { Ok(n) => { if n != 0 - 5 { return 54; } }, Err(e) => { return 55; } }

	return 0;
}
"""

THROW_SRC = r"""
module main;

import std.core as core;
import std.text as text;
import std.mem as mem;
import std.console as cons;

error Boom { at: Int }

pub fn main() nothrow -> Int {
	var s = "abc";
	s = s + "defgh";
	val v = text.byte_view_all(&s);

	// forced throw mid-window: unwinds cleanly, program continues,
	// and the view/backing remain fully usable afterwards.
	var caught = 0;
	var k = 0;
	while k < 100 {
		val body: core.CallbackThrow2<mem.Ptr<Byte>, Int, Int> =
			core.callback_throw2(|p: mem.Ptr<Byte>, n: Int| => {
				if n > 0 { throw Boom(at = n); }
				0
			});
		try {
			val x = text.with_view_bytes_throw<type Int, core.CallbackThrow2<mem.Ptr<Byte>, Int, Int> >(&v, move body);
			return 1;
		} catch Boom(e) {
			caught = caught + 1;
		} catch {
			return 9;
		}
		k = k + 1;
	}
	if caught != 100 { return 2; }
	if not v.eq_string(&s) { return 3; }
	val owned = v.to_string();
	if owned != s { return 4; }
	cons.println("throw path OK");
	return 0;
}
"""



EXTREMES_SRC = r"""module main;

import std.core as core;
import std.err as err;
import std.text as text;
import std.regex as regex;
import std.console as cons;

pub fn main() nothrow -> Int {
	var s = "ext-";
	s = s + "subject";
	val int_min = 0 - 9223372036854775807 - 1;
	val int_max = 9223372036854775807;

	// fabricated EXTREME spans: full validation happens BEFORE the
	// length subtraction, so these return checked errors instead of
	// overflowing.
	match regex.match_view(regex.RegexMatch(start = int_min, end = int_max), &s) {
		Ok(v) => { return 1; },
		Err(e) => { if e.tag != "out-of-bounds" { return 2; } }
	}
	match regex.match_view(regex.RegexMatch(start = int_min, end = 0), &s) {
		Ok(v) => { return 3; },
		Err(e) => { }
	}
	match regex.match_view(regex.RegexMatch(start = 0, end = int_max), &s) {
		Ok(v) => { return 4; },
		Err(e) => { }
	}
	match regex.match_view(regex.RegexMatch(start = int_max, end = int_min), &s) {
		Ok(v) => { return 5; },
		Err(e) => { }
	}
	val whole = text.byte_view_all(&s);
	match regex.match_subview(regex.RegexMatch(start = int_min, end = int_max), &whole) {
		Ok(v) => { return 6; },
		Err(e) => { if e.offset != int_min { return 7; } }
	}
	match regex.match_subview(regex.RegexMatch(start = 2, end = int_max), &whole) {
		Ok(v) => { return 8; },
		Err(e) => { }
	}

	// negative byte_at: EXACT Err(IndexError) payload — Result API,
	// nothrow caller, no catch anywhere.
	match whole.byte_at(0 - 4) {
		Ok(b) => { return 9; },
		Err(e) => {
			if e.container_id != "std.text:StringByteView" { return 10; }
			if e.index != 0 - 4 { return 11; }
		}
	}
	match whole.byte_at(9999) {
		Ok(b) => { return 13; },
		Err(e) => {
			if e.index != 9999 { return 14; }
			if e.container_id != "std.text:StringByteView" { return 15; }
		}
	}

	cons.println("extremes OK");
	return 0;
}
"""

FAILCLOSED_SRC = r"""module main;

import std.json as json;

pub fn main() nothrow -> Int {
	var t = "{\"k\": 1";
	t = t + "}";
	// Corrupted-span invariant: the raw_view enforcer must ABORT (fail
	// closed), never substitute content.
	val v = json._span_view_or_abort(&t, 2, 9999);
	// unreachable:
	return 4;
}
"""

MOVE_ONLY_NEG = r"""module main;

import std.text as text;

fn use_twice(a: text.StringByteView, b: text.StringByteView) nothrow -> Int {
	return a.byte_length() + b.byte_length();
}

pub fn main() nothrow -> Int {
	val s = "move-only";
	val v = text.byte_view_all(&s);
	return use_twice(v, v);
}
"""

PRIVATE_CTOR_NEG = r"""module main;

import std.text as text;

pub fn main() nothrow -> Int {
	val s = "forge";
	// Private fields: construction outside std.text must be rejected —
	// byte_view/byte_view_all are the only bounds gates.
	val v = text.StringByteView(backing = s, start = 0, len = 999);
	return v.byte_length();
}
"""



def _run(tmp_path: Path, src_text: str, name: str) -> None:
	src = tmp_path / f"{name}.drift"
	src.write_text(src_text)
	out_bin = tmp_path / f"{name}.bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev", "--allow-unsafe",
		 "--stdlib-root", str(stdlib), str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[:2500]}"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True,
		timeout=sanitizer_timeout(120))
	assert run.returncode == 0, (
		f"{name} exited {run.returncode} (failing check #)\n{run.stderr[:500]}"
	)


def test_view_semantics(tmp_path: Path) -> None:
	_run(tmp_path, SEMANTICS_SRC, "semantics")


def test_view_adoption_surfaces(tmp_path: Path) -> None:
	_run(tmp_path, ADOPTION_SRC, "adoption")


def test_numeric_offset_contract_table(tmp_path: Path) -> None:
	_run(tmp_path, OFFSET_TABLE_SRC, "offsets")


def test_with_view_bytes_throw_unwinds(tmp_path: Path) -> None:
	_run(tmp_path, THROW_SRC, "throwpath")


def test_fabricated_extremes_and_exact_index_error(tmp_path: Path) -> None:
	"""Blocker-1 pins: INT_MIN/INT_MAX fabricated spans return checked
	errors (validation precedes subtraction — no overflow), and
	byte_at's IndexError carries the exact container id + index."""
	_run(tmp_path, EXTREMES_SRC, "extremes")


def _compile_only(tmp_path: Path, src_text: str, name: str):
	src = tmp_path / f"{name}.drift"
	src.write_text(src_text)
	out_bin = tmp_path / f"{name}.bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev", "--allow-unsafe",
		 "--stdlib-root", str(stdlib), str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	), out_bin


def test_raw_view_invariant_fails_closed(tmp_path: Path) -> None:
	"""Blocker-2 tooth: the raw_view span-invariant enforcer ABORTS on a
	corrupted span (exercised via the exported-internal helper — the
	invariant is not reachable from safe Drift through raw_view
	itself) and never substitutes content."""
	res, out_bin = _compile_only(tmp_path, FAILCLOSED_SRC, "failclosed")
	assert res.returncode == 0, f"compile failed:\n{res.stderr[:1500]}"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True,
		timeout=sanitizer_timeout(60))
	assert run.returncode != 0, "corrupted span must ABORT, not return a view"
	assert "JSON span invariant violated" in run.stderr, run.stderr[:400]


def test_view_is_move_only(tmp_path: Path) -> None:
	"""Amendment pin: the view is NOT Copy — passing it by value twice
	is rejected with the existing clear diagnostic (dup() is the
	explicit affordance)."""
	res, _ = _compile_only(tmp_path, MOVE_ONLY_NEG, "moveonly")
	assert res.returncode != 0
	err = res.stdout + res.stderr
	assert "E-AUTO-e8f17b8b" in err or "is not Copy" in err, err[:800]


def test_view_private_fields_reject_forgery(tmp_path: Path) -> None:
	"""Amendment pin: constructing StringByteView outside std.text is
	rejected — the constructors are the only bounds gate."""
	res, _ = _compile_only(tmp_path, PRIVATE_CTOR_NEG, "privatector")
	assert res.returncode != 0
	err = res.stdout + res.stderr
	assert "MIR lowering contract failure" not in err
	assert ("private" in err.lower() or "not accessible" in err.lower()
	        or "cannot construct" in err.lower() or "error" in err.lower()), err[:800]
