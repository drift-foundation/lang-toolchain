# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Differential parity: the ITERATIVE parser vs the PRESERVED RECURSIVE
parser (the oracle), over identical generated inputs (2026-07-27).

A true recursive-vs-iterative differential — NOT the iterative parser
checked against frozen expectations.  std.json's former recursive parser
is retained as a DURABLE bounded-depth oracle (kept OUT of production
stdlib; appended to a throwaway copy — see `_json_oracle_stdlib.py`).  Both
parsers run over the SAME generated corpus:

  * curated scalars/arrays/objects/unicode/number forms and every
    malformed family;
  * a TRUNCATION family — several valid documents cut at EVERY byte prefix;
  * DEEP nested and WIDE array/object documents (deterministically
    generated);

across ALL THREE duplicate-key policies (KeepFirst/KeepLast/Reject) and
BOTH surfaces (`parse_with_config`, `parse_located`), asserting exact
parity of VALUE (compact re-encode), ERROR tag+offset, and — via
EXHAUSTIVE JSON-pointer enumeration over the deep/wide/nested shapes — the
FULL SPAN TREE (every node's span), not just the root.  Any divergence
fails the run.  The oracle fragment's content hash is pinned.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root
from lang.tests.driver._json_oracle_stdlib import build_oracle_stdlib

ROOT = Path(__file__).resolve().parents[3]
_FRAG = ROOT / "lang" / "tests" / "fixtures" / "json_recursive_oracle.drift.frag"
# Re-pinned 2026-07-30 (review-approved): the reject-redundant-call-borrows
# sweep bared 18 explicit borrows in the oracle fragment (one-token
# deletions; IR-equivalent — parity and perf-band gates below prove the
# baseline is unchanged in behavior).
_FRAG_SHA256 = "c3714429c75d3140b451b9f7dcf0d2fc2d4a273ce34fb788c8cac1f7782a80a6"


def _dq(s: str) -> str:
	"""Escape a Python string for embedding in a Drift `"..."` literal."""
	return s.replace("\\", "\\\\").replace('"', '\\"')


def _gen_inputs() -> list[str]:
	curated = [
		"null", "true", "false",
		"0", "-42", "3.14", "-0", "1e10", "1.5e-3", "123456789",
		'""', '"hello"', '"a\\nb\\t\\u0041"', '["\\u00e9"]',
		"[]", "[1,2,3]", "[1,[2,[3,[4]]]]",
		"{}", '{"a":1}', '{"a":1,"b":[true,null],"c":{"d":"x"}}',
		'{"id":12345,"name":"widget","tags":["a","b"],"meta":{"x":1}}',
		'  {  "a" : 1 , "b" : 2 }  ',
		'{"k":1,"k":2,"k":3}', '{"a":1,"a":2,"b":3}',
		# malformed families
		"", "[1,2", '{"a":', '{"a":1', '"unterminated', "[1,]", "{,}",
		"1 2", "[1 2]", "nul", "01", '{"a" 1}', "+5", "[[[",
	]
	# TRUNCATION family: every byte prefix of several valid docs.
	valids = [
		'{"a":1,"b":[10,20],"c":{"d":"x"}}',
		"[1,[2,[3,true]],null]",
		'{"k":true,"v":null,"n":-3.5e2}',
		'["\\u00e9",[1,2,3],{"z":[]}]',
	]
	trunc = [v[:L] for v in valids for L in range(1, len(v) + 1)]
	# DEEP nested (bounded well under the 128 cap).
	deep = ["[" * d + "1" + "]" * d for d in (5, 20, 60, 100)]
	deep_obj = ['{"a":' * d + "1" + "}" * d for d in (5, 20, 60)]
	# WIDE arrays/objects.
	wide = ["[" + ",".join(str(i) for i in range(w)) + "]" for w in (10, 50)]
	wide_obj = ["{" + ",".join(f'"k{i}":{i}' for i in range(w)) + "}" for w in (10, 40)]
	return curated + trunc + deep + deep_obj + wide + wide_obj


def _all_pointers(value, prefix="") -> list[str]:
	"""Every non-root JSON pointer into a python-modelled JSON value."""
	out: list[str] = []
	if isinstance(value, list):
		for i, v in enumerate(value):
			p = f"{prefix}/{i}"
			out.append(p)
			out.extend(_all_pointers(v, p))
	elif isinstance(value, dict):
		for k, v in value.items():
			p = f"{prefix}/{k}"
			out.append(p)
			out.extend(_all_pointers(v, p))
	return out


def _gen_span_docs() -> list[tuple[str, list[str]]]:
	"""(doc_text, all-pointers) for FULL span-tree comparison."""
	import json as _pyjson
	models = [
		# deep spine (array)
		_nest_arr(40),
		# deep spine (object)
		_nest_obj(40),
		# wide array
		list(range(50)),
		# wide object
		{f"k{i}": i for i in range(40)},
		# mixed nested tree
		{"a": 1, "b": [10, 20, {"c": [True, None, "x"]}], "d": {"e": {"f": [1, 2]}}},
	]
	docs = []
	for m in models:
		text = _pyjson.dumps(m, separators=(",", ":"))
		docs.append((text, _all_pointers(m)))
	return docs


def _nest_arr(d: int):
	v = 42
	for _ in range(d):
		v = [v]
	return v


def _nest_obj(d: int):
	v = 42
	for _ in range(d):
		v = {"a": v}
	return v


def _build_src() -> str:
	inputs = _gen_inputs()
	span_docs = _gen_span_docs()
	push_inputs = "\n\t".join(f'inputs.push("{_dq(s)}");' for s in inputs)

	span_blocks = []
	for docstr, ptrs in span_docs:
		pushes = "".join(f'ps.push("{_dq(p)}");' for p in ptrs)
		span_blocks.append(f"""
	{{
		val d = "{_dq(docstr)}";
		var ps: Array<String> = [];
		{pushes}
		var q = 0;
		while q < ps.len {{
			if not agree_pointer_span(d, kl, ps[q]) {{
				cons.println("MISMATCH span-tree " + ps[q]);
				mism = mism + 1;
			}}
			total = total + 1;
			q = q + 1;
		}}
	}}""")
	span_section = "\n".join(span_blocks)

	return _SRC_TEMPLATE.replace("__PUSH_INPUTS__", push_inputs).replace("__SPAN_SECTION__", span_section)


_SRC_TEMPLATE = r"""
module main;

import std.json as json;
import std.core as core;
import std.console as cons;
import std.format as fmt;

fn cfg_for(p: Int) nothrow -> json.JsonParseConfig {
	var b = json.parse_config_builder();
	if p == 0 { b.duplicate_keys(json.DuplicateKeyPolicy::KeepFirst()); }
	if p == 1 { b.duplicate_keys(json.DuplicateKeyPolicy::KeepLast()); }
	if p == 2 { b.duplicate_keys(json.DuplicateKeyPolicy::Reject()); }
	match b.build() {
		core.Result::Ok(c) => { return move c; },
		core.Result::Err(_e) => { return json.permissive(); }
	}
}

fn value_eq(a: &json.JsonNode, b: &json.JsonNode) nothrow -> Bool {
	return json.encode_compact(a) == json.encode_compact(b);
}

fn agree_nonlocated(input: &String, cfg: &json.JsonParseConfig) nothrow -> Bool {
	match json.parse_with_config(input, cfg) {
		core.Result::Ok(inode) => {
			match json._oracle_parse_with_config(input, cfg) {
				core.Result::Ok(rnode) => { return value_eq(inode, rnode); },
				core.Result::Err(_re) => { return false; }
			}
		},
		core.Result::Err(ie) => {
			match json._oracle_parse_with_config(input, cfg) {
				core.Result::Err(re) => { return ie.tag == re.tag and ie.offset == re.offset; },
				core.Result::Ok(_rn) => { return false; }
			}
		}
	}
}

fn agree_located(input: &String, cfg: &json.JsonParseConfig) nothrow -> Bool {
	match json.parse_located(input, cfg) {
		core.Result::Ok(idoc) => {
			match json._oracle_parse_located(input, cfg) {
				core.Result::Ok(rdoc) => {
					val ic = idoc.cursor();
					val rc = rdoc.cursor();
					val isp = ic.span();
					val rsp = rc.span();
					return isp.start == rsp.start and isp.end == rsp.end;
				},
				core.Result::Err(_re) => { return false; }
			}
		},
		core.Result::Err(ie) => {
			match json._oracle_parse_located(input, cfg) {
				core.Result::Err(re) => { return ie.tag == re.tag and ie.offset == re.offset; },
				core.Result::Ok(_rd) => { return false; }
			}
		}
	}
}

// The documents and pointers here are GENERATED valid; both parses AND
// both pointer lookups MUST resolve.  Any failure (either surface, either
// lookup) is a divergence and fails the comparison — no vacuous success.
fn agree_pointer_span(input: &String, cfg: &json.JsonParseConfig, ptr: &String) nothrow -> Bool {
	match json.parse_located(input, cfg) {
		core.Result::Ok(idoc) => {
			match json._oracle_parse_located(input, cfg) {
				core.Result::Ok(rdoc) => {
					match idoc.at_pointer(ptr) {
						core.Result::Ok(ipc) => {
							match rdoc.at_pointer(ptr) {
								core.Result::Ok(rpc) => {
									val a = ipc.span();
									val b = rpc.span();
									return a.start == b.start and a.end == b.end;
								},
								core.Result::Err(_e) => { return false; }   // recursive pointer must resolve
							}
						},
						core.Result::Err(_ie) => { return false; }          // iterative pointer must resolve
					}
				},
				core.Result::Err(_re) => { return false; }                  // recursive parse must succeed
			}
		},
		core.Result::Err(_ie) => { return false; }                          // iterative parse must succeed
	}
}

pub fn main() nothrow -> Int {
	var inputs: Array<String> = [];
	__PUSH_INPUTS__

	var mism = 0;
	var total = 0;
	var i = 0;
	while i < inputs.len {
		val inp = &inputs[i];
		var p = 0;
		while p < 3 {
			val cfg = cfg_for(p);
			if not agree_nonlocated(inp, cfg) {
				cons.println("MISMATCH nonlocated p=" + fmt.format_int(p) + " #" + fmt.format_int(i) + ": " + *inp);
				mism = mism + 1;
			}
			total = total + 1;
			if not agree_located(inp, cfg) {
				cons.println("MISMATCH located p=" + fmt.format_int(p) + " #" + fmt.format_int(i) + ": " + *inp);
				mism = mism + 1;
			}
			total = total + 1;
			p = p + 1;
		}
		i = i + 1;
	}

	// ── FULL span-tree parity: every JSON pointer of each generated shape ──
	val kl = cfg_for(1);
	__SPAN_SECTION__

	cons.println("differential total=" + fmt.format_int(total) + " mismatches=" + fmt.format_int(mism));
	if mism == 0 { return 0; }
	return 1;
}
"""


def test_oracle_fragment_is_pinned() -> None:
	"""The recursive-oracle fragment is a FROZEN comparative baseline; its
	content hash is pinned so an accidental (or silent) edit to the oracle
	is caught."""
	actual = hashlib.sha256(_FRAG.read_bytes()).hexdigest()
	assert actual == _FRAG_SHA256, (
		f"recursive-oracle fragment changed (sha256 {actual}); if intentional, "
		f"re-pin _FRAG_SHA256 and justify the baseline change")


def test_recursive_vs_iterative_differential_parity(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(_build_src())
	out_bin = tmp_path / "diff.bin"
	stdlib = build_oracle_stdlib(tmp_path)
	comp = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(stdlib), str(src), "--entry", "main::main",
		 "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(300))
	assert comp.returncode == 0, f"compile failed:\n{comp.stdout}\n{comp.stderr[-2500:]}"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True,
	                     timeout=sanitizer_timeout(180))
	assert run.returncode == 0, (
		f"iterative parser diverged from the recursive oracle:\n{run.stdout[-3000:]}\n{run.stderr[:800]}")
	summary = [l for l in run.stdout.splitlines() if l.startswith("differential total=")]
	assert summary and summary[0].endswith("mismatches=0"), run.stdout[-3000:]
	total = int(summary[0].split("total=")[1].split(" ")[0])
	assert total >= 1000, f"differential corpus too small ({total})"
