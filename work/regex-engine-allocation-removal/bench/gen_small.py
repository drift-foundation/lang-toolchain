# regex-engine-allocation-removal: generator for the SMALL-SUBJECT
# representative suite (reviewer workload correction: typical regex
# subjects are 64 B - 4 KiB, ~250 B — these are the PRIMARY gate).
# Emits bench/generated/{ops_small.drift, counts_small.drift,
# driver_small.c, small_meta.json} from one scenario table so the
# Drift builders, the C driver, the model replica, and the reporting
# can never drift apart.
#
# Scenarios (x sizes 64/128/256/512/1024/4096):
#   early    — [a-z]+[0-9]+ hit at offset 0 ("id42," prefix)
#   late     — hit in the last 5 bytes (",id42" suffix; every earlier
#              candidate start fails)
#   nomatch  — filler only, full failed scan
#   anchored — ^[a-z,x]-class full-subject validation via is_match
#   alt      — 6-branch alternation (get|post|put|delete|head|patch),
#              hit in the last 6 bytes (",patch" suffix)
# Forms: String everywhere; StringByteView for late/nomatch at
# 256/4096; compile+match (cm_) rows at 256 for late/nomatch/anchored/
# alt; everything else is compile-once/repeated-match.
from __future__ import annotations

import json
from pathlib import Path

BENCH = Path(__file__).resolve().parent
GEN = BENCH / "generated"

SIZES = [64, 128, 256, 512, 1024, 4096]
UNIT = "worker,tunnel,socket,buffer,"          # 28 B, no digits, no verbs
P1 = "[a-z]+[0-9]+"
PA = "^[a-z,x]+$"
PALT = "(get|post|put|delete|head|patch)"

COUNT_SIZES = {"late": SIZES, "nomatch": SIZES,
               "early": [256, 4096], "anchored": [256, 4096],
               "alt": [256, 4096]}
VIEW_COUNT = {"late": [256, 4096], "nomatch": [256, 4096]}
COUNT_REPS = 100


def reps_for(n: int) -> int:
	return max(500, 2_000_000 // n)


# ------------------------------------------------ input builders

def py_fill(n: int) -> bytes:
	s = b""
	u = UNIT.encode()
	while len(s) + len(u) <= n:
		s += u
	s += b"x" * (n - len(s))
	return s


def py_input(scen: str, n: int) -> bytes:
	if scen == "early":
		s = b"id42,"
		u = UNIT.encode()
		while len(s) + len(u) <= n:
			s += u
		return s + b"x" * (n - len(s))
	if scen == "late":
		return py_fill(n - 5) + b",id42"
	if scen == "alt":
		return py_fill(n - 6) + b",patch"
	return py_fill(n)  # nomatch / anchored


def pattern_for(scen: str) -> str:
	return {"early": P1, "late": P1, "nomatch": P1,
	        "anchored": PA, "alt": PALT}[scen]


def expected_per_search(scen: str) -> int:
	# what one search adds to the op checksum
	return {"early": 4, "late": 4, "nomatch": 0,
	        "anchored": 1, "alt": 5}[scen]


# ------------------------------------------------ drift emission

DRIFT_BUILDERS = """
// byte-exact builders (mirrored in model.py — keep in sync via
// gen_small.py only)
fn mk_fill_sm(n: Int) nothrow -> String {
	var sb = text.string_builder(n + 32);
	var len = 0;
	while len + 28 <= n {
		text.sb_append_string(&mut sb, &"worker,tunnel,socket,buffer,");
		len = len + 28;
	}
	while len < n {
		text.sb_append_string(&mut sb, &"x");
		len = len + 1;
	}
	return text.sb_build(&mut sb);
}

fn mk_early_sm(n: Int) nothrow -> String {
	var sb = text.string_builder(n + 32);
	text.sb_append_string(&mut sb, &"id42,");
	var len = 5;
	while len + 28 <= n {
		text.sb_append_string(&mut sb, &"worker,tunnel,socket,buffer,");
		len = len + 28;
	}
	while len < n {
		text.sb_append_string(&mut sb, &"x");
		len = len + 1;
	}
	return text.sb_build(&mut sb);
}

fn mk_late_sm(n: Int) nothrow -> String {
	var sb = text.string_builder(n + 32);
	var len = 0;
	while len + 28 <= n - 5 {
		text.sb_append_string(&mut sb, &"worker,tunnel,socket,buffer,");
		len = len + 28;
	}
	while len < n - 5 {
		text.sb_append_string(&mut sb, &"x");
		len = len + 1;
	}
	text.sb_append_string(&mut sb, &",id42");
	return text.sb_build(&mut sb);
}

fn mk_altlate_sm(n: Int) nothrow -> String {
	var sb = text.string_builder(n + 32);
	var len = 0;
	while len + 28 <= n - 6 {
		text.sb_append_string(&mut sb, &"worker,tunnel,socket,buffer,");
		len = len + 28;
	}
	while len < n - 6 {
		text.sb_append_string(&mut sb, &"x");
		len = len + 1;
	}
	text.sb_append_string(&mut sb, &",patch");
	return text.sb_build(&mut sb);
}
"""

DRIFT_LOOPS = """
fn find_len_loop(re: &regex.Regex, s: &String, reps: Int) nothrow -> Int {
	var acc = 0;
	var k = 0;
	while k < reps {
		match regex.find_first(re, s) {
			Some(m) => { acc = acc + m.end - m.start; },
			None() => { acc = acc + 0; }
		}
		k = k + 1;
	}
	return acc;
}

fn find_len_loop_view(re: &regex.Regex, v: &text.StringByteView, reps: Int) nothrow -> Int {
	var acc = 0;
	var k = 0;
	while k < reps {
		match regex.find_first_view(re, v) {
			Some(m) => { acc = acc + m.end - m.start; },
			None() => { acc = acc + 0; }
		}
		k = k + 1;
	}
	return acc;
}

fn is_match_loop(re: &regex.Regex, s: &String, reps: Int) nothrow -> Int {
	var acc = 0;
	var k = 0;
	while k < reps {
		if regex.is_match(re, s) { acc = acc + 1; }
		k = k + 1;
	}
	return acc;
}

fn cm_find_loop(pat: &String, s: &String, reps: Int) nothrow -> Int {
	var acc = 0;
	var k = 0;
	while k < reps {
		match regex.compile(pat) {
			Ok(re) => {
				match regex.find_first(&re, s) {
					Some(m) => { acc = acc + m.end - m.start; },
					None() => { acc = acc + 0; }
				}
			},
			Err(e) => { return 0 - 1; }
		}
		k = k + 1;
	}
	return acc;
}

fn cm_anchor_loop(pat: &String, s: &String, reps: Int) nothrow -> Int {
	var acc = 0;
	var k = 0;
	while k < reps {
		match regex.compile(pat) {
			Ok(re) => {
				if regex.is_match(&re, s) { acc = acc + 1; }
			},
			Err(e) => { return 0 - 1; }
		}
		k = k + 1;
	}
	return acc;
}
"""


def mk_call(scen: str, n: int) -> str:
	return {"early": f"mk_early_sm({n})", "late": f"mk_late_sm({n})",
	        "alt": f"mk_altlate_sm({n})", "nomatch": f"mk_fill_sm({n})",
	        "anchored": f"mk_fill_sm({n})"}[scen]


def re_var(scen: str) -> str:
	return {"early": "re1", "late": "re1", "nomatch": "re1",
	        "anchored": "rea", "alt": "ralt"}[scen]


def gen_ops() -> tuple[str, dict]:
	meta = {}
	rows = []
	subjects = []
	seen = set()

	def subject(scen, n):
		name = f"s_{'fill' if scen in ('nomatch', 'anchored') else scen}_{n}"
		if name not in seen:
			seen.add(name)
			subjects.append(f"\tval {name} = {mk_call(scen, n)};")
		return name

	rc = [70]

	def row(label, expr, expect, reps, n, scen, form):
		rc[0] += 1
		meta[label] = {"reps": reps, "size": n, "scenario": scen,
		               "form": form}
		rows.append(f"""
	var line_{label} = "RESULT {label} us=";
	k = 0;
	while k < iters {{
		val t0 = time.now_monotonic();
		val r = {expr};
		line_{label} = line_{label} + fmt.format_int(time.elapsed_micros(&t0)) + ",";
		if r != {expect} {{ return {rc[0]}; }}
		k = k + 1;
	}}
	cons.println(line_{label});""")

	for scen in ("early", "late", "nomatch", "anchored", "alt"):
		for n in SIZES:
			sub = subject(scen, n)
			reps = reps_for(n)
			exp = expected_per_search(scen) * reps
			if scen == "anchored":
				expr = f"is_match_loop({re_var(scen)}, &{sub}, {reps})"
			else:
				expr = f"find_len_loop({re_var(scen)}, &{sub}, {reps})"
			row(f"sm_{scen}_{n}", expr, exp, reps, n, scen, "string")

	view_decls = []
	for scen in ("late", "nomatch"):
		for n in (256, 4096):
			sub = subject(scen, n)
			reps = reps_for(n)
			exp = expected_per_search(scen) * reps
			vname = f"v_{scen}_{n}"
			view_decls.append(f"\tval {vname} = text.byte_view_all(&{sub});")
			row(f"sv_{scen}_{n}",
			    f"find_len_loop_view(re1, &{vname}, {reps})",
			    exp, reps, n, scen, "view")

	for scen in ("late", "nomatch", "anchored", "alt"):
		n = 256
		sub = subject(scen, n)
		reps = reps_for(n)
		exp = expected_per_search(scen) * reps
		pat = {"late": "p1", "nomatch": "p1", "anchored": "pa",
		       "alt": "palt"}[scen]
		if scen == "anchored":
			expr = f"cm_anchor_loop(&{pat}, &{sub}, {reps})"
		else:
			expr = f"cm_find_loop(&{pat}, &{sub}, {reps})"
		row(f"cm_{scen}_{n}", expr, exp, reps, n, scen, "compile+match")

	src = f"""// GENERATED by gen_small.py — do not edit by hand.
// Small-subject representative suite (PRIMARY perf gate).
module main;

import std.core as core;
import std.text as text;
import std.regex as regex;
import std.time as time;
import std.console as cons;
import std.format as fmt;
{DRIFT_BUILDERS}{DRIFT_LOOPS}
fn run_all(re1: &regex.Regex, rea: &regex.Regex, ralt: &regex.Regex) nothrow -> Int {{
	val p1 = "{P1}";
	val pa = "{PA}";
	val palt = "{PALT}";
{chr(10).join(subjects)}
{chr(10).join(view_decls)}

	val iters = 5;
	var k = 0;
{"".join(rows)}

	return 0;
}}

pub fn main() nothrow -> Int {{
	match regex.compile(&"{P1}") {{
		Err(e) => {{ return 30; }},
		Ok(re1) => {{
			match regex.compile(&"{PA}") {{
				Err(e) => {{ return 31; }},
				Ok(rea) => {{
					match regex.compile(&"{PALT}") {{
						Err(e) => {{ return 32; }},
						Ok(ralt) => {{
							return run_all(&re1, &rea, &ralt);
						}}
					}}
				}}
			}}
		}}
	}}
}}
"""
	return src, meta


def gen_counts() -> tuple[str, str, dict]:
	"""counts_small.drift + driver_small.c + twin map."""
	ops = []
	externs = []
	runs = []
	twins = {}
	mks = set()

	def op(label, scen, n, view=False):
		pat = pattern_for(scen)
		twin = {"early": "sc_compile_p1", "late": "sc_compile_p1",
		        "nomatch": "sc_compile_p1", "anchored": "sc_compile_pa",
		        "alt": "sc_compile_palt"}[scen]
		twins[label] = twin
		mk = {"early": f"smk_early_{n}", "late": f"smk_late_{n}",
		      "alt": f"smk_alt_{n}", "nomatch": f"smk_fill_{n}",
		      "anchored": f"smk_fill_{n}"}[scen]
		mks.add((mk, scen if scen in ("early", "late", "alt") else "fill", n))
		body_match = (
			f"""			var acc = 0;
			var k = 0;
			while k < {COUNT_REPS} {{
				{"if regex.is_match(&re, &s) { acc = acc + 1; }" if scen == "anchored" else ""}"""
		)
		if scen == "anchored":
			inner = f"""			var acc = 0;
			var k = 0;
			while k < {COUNT_REPS} {{
				if regex.is_match(&re, &s) {{ acc = acc + 1; }}
				k = k + 1;
			}}
			return acc;"""
		elif view:
			inner = f"""			val v = text.byte_view_all(&s);
			var acc = 0;
			var k = 0;
			while k < {COUNT_REPS} {{
				match regex.find_first_view(&re, &v) {{
					Some(m) => {{ acc = acc + m.end - m.start; }},
					None() => {{ acc = acc + 0; }}
				}}
				k = k + 1;
			}}
			return acc;"""
		else:
			inner = f"""			var acc = 0;
			var k = 0;
			while k < {COUNT_REPS} {{
				match regex.find_first(&re, &s) {{
					Some(m) => {{ acc = acc + m.end - m.start; }},
					None() => {{ acc = acc + 0; }}
				}}
				k = k + 1;
			}}
			return acc;"""
		ops.append(f"""
pub fn {label}(s: String) nothrow -> Int {{
	match regex.compile(&"{pat}") {{
		Ok(re) => {{
{inner}
		}},
		Err(e) => {{ return 0 - 1; }}
	}}
}}""")
		externs.append(f"extern long {label}(DriftString);")
		runs.append(f'\tRUN({label}, subj_{mk}, "{label}");')

	for scen, sizes in COUNT_SIZES.items():
		for n in sizes:
			op(f"sc_{scen}_{n}", scen, n)
	for scen, sizes in VIEW_COUNT.items():
		for n in sizes:
			op(f"sc_{scen}_view_{n}", scen, n, view=True)

	mk_fns = []
	mk_c_decls = []
	mk_c_init = []
	mk_c_free = []
	for mk, kind, n in sorted(mks):
		call = {"fill": f"mk_fill_sm({n})", "early": f"mk_early_sm({n})",
		        "late": f"mk_late_sm({n})", "alt": f"mk_altlate_sm({n})"}[kind]
		mk_fns.append(f"""
pub fn {mk}() nothrow -> String {{
	return {call};
}}""")
		mk_c_decls.append(f"extern DriftString {mk}(void);")
		mk_c_init.append(f"\tDriftString subj_{mk} = {mk}();")
		mk_c_free.append(f"\tdrift_string_release(subj_{mk});")

	compile_ops = []
	for label, pat in (("sc_compile_p1", P1), ("sc_compile_pa", PA),
	                   ("sc_compile_palt", PALT)):
		compile_ops.append(f"""
pub fn {label}(s: String) nothrow -> Int {{
	match regex.compile(&"{pat}") {{
		Ok(re) => {{ return 1; }},
		Err(e) => {{ return 0 - 1; }}
	}}
}}""")
		externs.append(f"extern long {label}(DriftString);")

	first_subj = sorted(mks)[0][0]
	compile_runs = [f'\tRUN({label}, subj_{first_subj}, "{label}");'
	                for label in ("sc_compile_p1", "sc_compile_pa",
	                              "sc_compile_palt")]

	drift_src = f"""// GENERATED by gen_small.py — do not edit by hand.
module main;

import std.core as core;
import std.text as text;
import std.regex as regex;
{DRIFT_BUILDERS}{"".join(mk_fns)}{"".join(compile_ops)}{"".join(ops)}

pub fn main() nothrow -> Int {{
	val s = {first_subj}();
	if sc_compile_p1(s) != 1 {{ return 1; }}
	return 0;
}}
"""

	shim = (BENCH / "driver.c").read_text()
	shim = shim.split("extern DriftString mk_carrier_64k(void);")[0]
	shim = shim.replace("count-exact C driver (rev 2).",
	                    "GENERATED small-suite count driver (same shim as driver.c).")

	c_src = f"""{shim}
{chr(10).join(mk_c_decls)}

{chr(10).join(externs)}

#define RUN(fn, subj, label) do {{ \\
	DriftString a = drift_string_retain(subj); \\
	reset(); \\
	long r = fn(a); \\
	report(label, r); \\
	if (r < 0) {{ printf("OPFAIL=%s r=%ld\\n", label, r); return 70; }} \\
}} while (0)

int main(void) {{
{chr(10).join(mk_c_init)}

{chr(10).join(compile_runs)}
{chr(10).join(runs)}

{chr(10).join(mk_c_free)}
	printf("DONE\\n");
	return 0;
}}
"""
	return drift_src, c_src, twins


def main():
	GEN.mkdir(exist_ok=True)
	ops_src, meta = gen_ops()
	counts_src, driver_src, twins = gen_counts()
	(GEN / "ops_small.drift").write_text(ops_src)
	(GEN / "counts_small.drift").write_text(counts_src)
	(GEN / "driver_small.c").write_text(driver_src)
	(GEN / "small_meta.json").write_text(json.dumps(
		{"rows": meta, "twins": twins, "count_reps": COUNT_REPS,
		 "sizes": SIZES, "patterns": {"p1": P1, "pa": PA, "palt": PALT}},
		indent=2))
	print(f"generated: {len(meta)} timing rows, {len(twins)} count windows")


if __name__ == "__main__":
	main()
