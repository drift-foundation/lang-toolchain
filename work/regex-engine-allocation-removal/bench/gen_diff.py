# regex-engine-allocation-removal: dual-engine shadow differential
# generator (reviewer blocker 6).
#
# Emits generated/diff_main.drift containing:
#   * a VERBATIM copy of the LEGACY engine (the regex.drift captured
#     in legacy_regex.drift.snapshot, taken from the pre-rewrite
#     tree), with every top-level symbol renamed Lg<name> so it
#     coexists with the post-rewrite std.regex in one binary;
#   * a generated corpus of patterns x inputs (seeded, deterministic:
#     valid patterns from the supported grammar plus deliberately
#     invalid ones);
#   * a harness comparing, case by case: compile success AND error
#     tag/offset; find_first presence AND exact spans; is_match; the
#     view entry point (find_first_view over byte_view_all must equal
#     the String result).  Any divergence prints DIFF lines and the
#     process exits nonzero.
#
# Run AFTER the engine rewrite: the legacy snapshot is the shadow.
from __future__ import annotations

import random
import re
from pathlib import Path

BENCH = Path(__file__).resolve().parent
GEN = BENCH / "generated"
SNAPSHOT = BENCH / "legacy_regex.drift.snapshot"

SEED = 20260726
N_VALID = 240
N_INVALID = 40
INPUTS_PER_PATTERN = 4

# top-level symbols of the legacy file, renamed Lg<name>; longest
# first so e.g. is_match_view is renamed before is_match
SYMBOLS = sorted([
	"Parser", "_new_parser", "_peek", "_advance", "_at_end", "_err",
	"_parse_alternation", "_parse_sequence", "_parse_quantified",
	"_parse_atom", "_parse_escape", "_class_digit", "_class_word",
	"_class_space", "_parse_char_class", "_maybe_range",
	"_NfaOp", "_NfaProg", "_node_size", "_emit_node", "_nfa_compile",
	"_make_bitmap", "_clear_bitmap", "_add_state", "_byte_matches",
	"_try_match_at_range", "_try_match_at", "_find_from_range",
	"_find_from", "_substr",
	"compile", "is_match_view", "is_match", "find_first_view",
	"find_first", "match_subview", "match_view",
	"replace_first", "replace_all",
	"RegexError", "RegexMatch", "RegexNode", "CharClass", "CharRange",
	"Quantifier", "AnchorKind", "Regex",
], key=len, reverse=True)


def legacy_module() -> str:
	src = SNAPSHOT.read_text()
	src = re.sub(r"^module std\.regex;\n", "", src, flags=re.M)
	src = re.sub(r"^export \{.*?\};\n", "", src, flags=re.S | re.M)
	src = re.sub(r"^import .*\n", "", src, flags=re.M)
	for sym in SYMBOLS:
		src = re.sub(rf"\b{re.escape(sym)}\b", f"Lg{sym}", src)
	return src


# --------------------------------------------- pattern generation

class Gen:
	def __init__(self, rng: random.Random):
		self.rng = rng

	def literal(self):
		return self.rng.choice("abcde0123x")

	def klass(self):
		r = self.rng
		neg = "^" if r.random() < 0.25 else ""
		parts = []
		for _ in range(r.randint(1, 3)):
			if r.random() < 0.5:
				lo = r.choice("abcx")
				hi = chr(ord(lo) + r.randint(1, 5))
				parts.append(f"{lo}-{hi}")
			else:
				parts.append(r.choice("abcde0123"))
		return f"[{neg}{''.join(parts)}]"

	def escape(self):
		return "\\" + self.rng.choice("dDwWsS")

	def atom(self, depth):
		r = self.rng
		roll = r.random()
		if depth > 0 and roll < 0.18:
			return f"({self.alternation(depth - 1)})"
		if roll < 0.35:
			return self.klass()
		if roll < 0.45:
			return self.escape()
		if roll < 0.52:
			return "."
		return self.literal()

	def quantified(self, depth):
		a = self.atom(depth)
		r = self.rng.random()
		if r < 0.15:
			return a + "*"
		if r < 0.30:
			return a + "+"
		if r < 0.38:
			return a + "?"
		return a

	def sequence(self, depth):
		n = self.rng.randint(1, 5)
		body = "".join(self.quantified(depth) for _ in range(n))
		if self.rng.random() < 0.12:
			body = "^" + body
		if self.rng.random() < 0.12:
			body = body + "$"
		return body

	def alternation(self, depth):
		n = self.rng.choices([1, 2, 3, 4], weights=[5, 3, 2, 1])[0]
		return "|".join(self.sequence(depth) for _ in range(n))

	def pattern(self):
		return self.alternation(2)

	def invalid(self):
		r = self.rng
		base = self.pattern()
		mode = r.randint(0, 5)
		if mode == 0:
			return "(" + base
		if mode == 1:
			return base + "["
		if mode == 2:
			return "*" + base
		if mode == 3:
			return base + "\\"
		if mode == 4:
			return "[]" + base
		return base + "\\q"

	def subject(self):
		r = self.rng
		n = r.randint(0, 48)
		return "".join(r.choice("abcde0123x,  \t") for _ in range(n))


def drift_str(s: str) -> str:
	out = s.replace("\\", "\\\\").replace('"', '\\"')
	out = out.replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
	return f'"{out}"'


HARNESS = """
// one case: compare legacy vs current std.regex on every entry point
fn diff_case(mm: &mut Int, cid: Int, pat: &String, inp: &String) nothrow -> Void {
	match Lgcompile(pat) {
		Ok(lre) => {
			match regex.compile(pat) {
				Ok(nre) => {
					var lst = 0 - 1;
					var len2 = 0 - 1;
					match Lgfind_first(&lre, inp) {
						Some(lm) => { lst = lm.start; len2 = lm.end; },
						None() => { }
					}
					var nst = 0 - 1;
					var nen = 0 - 1;
					match regex.find_first(&nre, inp) {
						Some(nm) => { nst = nm.start; nen = nm.end; },
						None() => { }
					}
					if lst != nst or len2 != nen {
						*mm = *mm + 1;
						cons.println("DIFF find case=" + fmt.format_int(cid)
							+ " legacy=" + fmt.format_int(lst) + ":" + fmt.format_int(len2)
							+ " new=" + fmt.format_int(nst) + ":" + fmt.format_int(nen));
					}
					val lim = Lgis_match(&lre, inp);
					val nim = regex.is_match(&nre, inp);
					if lim != nim {
						*mm = *mm + 1;
						cons.println("DIFF is_match case=" + fmt.format_int(cid));
					}
					val v = text.byte_view_all(inp);
					var vst = 0 - 1;
					var ven = 0 - 1;
					match regex.find_first_view(&nre, &v) {
						Some(vm) => { vst = vm.start; ven = vm.end; },
						None() => { }
					}
					if vst != nst or ven != nen {
						*mm = *mm + 1;
						cons.println("DIFF view case=" + fmt.format_int(cid)
							+ " view=" + fmt.format_int(vst) + ":" + fmt.format_int(ven)
							+ " string=" + fmt.format_int(nst) + ":" + fmt.format_int(nen));
					}
				},
				Err(ne) => {
					*mm = *mm + 1;
					cons.println("DIFF compile-flip(new-rejects) case=" + fmt.format_int(cid));
				}
			}
		},
		Err(le) => {
			match regex.compile(pat) {
				Ok(nre2) => {
					*mm = *mm + 1;
					cons.println("DIFF compile-flip(new-accepts) case=" + fmt.format_int(cid));
				},
				Err(ne2) => {
					if le.tag != ne2.tag or le.offset != ne2.offset {
						*mm = *mm + 1;
						cons.println("DIFF compile-error case=" + fmt.format_int(cid)
							+ " legacy=" + le.tag + "@" + fmt.format_int(le.offset)
							+ " new=" + ne2.tag + "@" + fmt.format_int(ne2.offset));
					}
				}
			}
		}
	}
}
"""


def main():
	rng = random.Random(SEED)
	g = Gen(rng)
	cases = []
	for _ in range(N_VALID):
		pat = g.pattern()
		for _ in range(INPUTS_PER_PATTERN):
			cases.append((pat, g.subject()))
	for _ in range(N_INVALID):
		pat = g.invalid()
		cases.append((pat, g.subject()))

	blocks = []
	calls = []
	for bi in range(0, len(cases), 50):
		chunk = cases[bi:bi + 50]
		body = "\n".join(
			f"\tdiff_case(mm, {bi + i}, &{drift_str(p)}, &{drift_str(s)});"
			for i, (p, s) in enumerate(chunk))
		blocks.append(
			f"fn case_block_{bi // 50}(mm: &mut Int) nothrow -> Void {{\n"
			f"{body}\n}}\n")
		calls.append(f"\tcase_block_{bi // 50}(&mut mm);")

	src = f"""// GENERATED by gen_diff.py — do not edit by hand.
// Dual-engine shadow differential: legacy engine (verbatim snapshot,
// Lg-renamed) vs the current std.regex, over {len(cases)} generated
// cases (seed {SEED}).
module main;

import std.core as core;
import std.io as io;
import std.text as text;
import std.regex as regex;
import std.console as cons;
import std.format as fmt;

// ==================== LEGACY ENGINE (snapshot) ====================
{legacy_module()}
// ==================== end legacy engine ===========================
{HARNESS}
{chr(10).join(blocks)}
pub fn main() nothrow -> Int {{
	var mm = 0;
{chr(10).join(calls)}
	cons.println("DIFF-TOTAL " + fmt.format_int(mm) + " mismatches / {len(cases)} cases");
	if mm != 0 {{ return 1; }}
	return 0;
}}
"""
	GEN.mkdir(exist_ok=True)
	(GEN / "diff_main.drift").write_text(src)
	print(f"generated diff_main.drift: {len(cases)} cases")


if __name__ == "__main__":
	main()
