# Robustness triage probe.
# Generates pathological Drift sources, runs driftc against them, classifies the outcome.
# This is throwaway scaffolding for the robustness-matrix walk; not a test.
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DRIFTC = [sys.executable, "-m", "lang.driftc.driftc", "--dev", "--stdlib-root", str(ROOT / "stdlib"), "--entry", "main::main"]


def classify(rc: int, stdout: str, stderr: str, timed_out: bool) -> tuple[str, str]:
	"""Return (failure_shape, phase_guess)."""
	if timed_out:
		return ("timeout/hang", "?")
	if rc == 0:
		return ("ok", "n/a")
	if "Traceback (most recent call last)" in stderr:
		# Python crash. Try to identify which phase from the deepest frame.
		phase = "?"
		for line in stderr.splitlines():
			if "lang/driftc/parser/" in line:
				phase = "parser"
			elif "lang/driftc/stage1/" in line or "ast_to_hir" in line:
				phase = "stage1"
			elif "lang/driftc/checker/" in line or "type_checker" in line:
				phase = "checker"
			elif "lang/driftc/stage2/" in line or "hir_to_mir" in line:
				phase = "stage2"
			elif "lang/codegen/llvm/" in line:
				phase = "codegen"
		# Identify exception class — match anywhere in line, not just start.
		exc = "?"
		exc_keywords = ["RecursionError", "MemoryError", "SystemError", "RuntimeError", "AssertionError", "KeyError", "AttributeError", "TypeError", "ValueError"]
		for line in stderr.splitlines():
			for kw in exc_keywords:
				if kw + ":" in line or line.strip().startswith(kw):
					exc = kw
					break
			if exc != "?":
				break
		return (f"python exc: {exc}", phase)
	if rc < 0:
		# Killed by signal.
		try:
			signame = signal.Signals(-rc).name
		except Exception:
			signame = f"signal {-rc}"
		return (f"crash/{signame}", "?")
	# Plain non-zero. Could be a clean diagnostic or an unstructured error.
	if " error:" in stderr or " error: " in stderr:
		return ("clean diagnostic", "?")
	return (f"non-zero rc={rc}", "?")


def run(label: str, source: str, timeout: int = 30) -> dict:
	tmp = Path("/tmp/robustness_probe.drift")
	tmp.write_text(source)
	out = Path("/tmp/robustness_probe.bin")
	cmd = DRIFTC + [str(tmp), "-o", str(out)]
	timed_out = False
	try:
		res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
		rc, sout, serr = res.returncode, res.stdout, res.stderr
	except subprocess.TimeoutExpired as ex:
		rc, sout, serr = 124, ex.stdout or "", ex.stderr or ""
		timed_out = True
	shape, phase = classify(rc, sout, serr, timed_out)
	return {
		"label": label,
		"shape": shape,
		"phase": phase,
		"rc": rc,
		"stderr_head": serr[:300],
	}


def report(rows: list[dict]) -> None:
	for r in rows:
		print(f"[{r['shape']:26}] phase={r['phase']:10} rc={r['rc']:5}  {r['label']}")
		if r["stderr_head"].strip():
			head = r["stderr_head"].splitlines()[0][:200]
			print(f"    └─ {head}")


# ── Generators ──────────────────────────────────────────────────────────


def gen_nested_blocks(n: int) -> str:
	body = "return 0;"
	for _ in range(n):
		body = "{\n" + body + "\n}"
	return f"module main;\npub fn main() nothrow -> Int {{\n{body}\n}}\n"


def gen_nested_if(n: int) -> str:
	body = "return 0;"
	for _ in range(n):
		body = "if true {\n" + body + "\n} else { return 1; }"
	return f"module main;\npub fn main() nothrow -> Int {{\n{body}\n}}\n"


def gen_nested_paren_expr(n: int) -> str:
	expr = "1"
	for _ in range(n):
		expr = "(" + expr + ")"
	return f"module main;\npub fn main() nothrow -> Int {{\n\treturn {expr};\n}}\n"


def gen_long_add_chain(n: int) -> str:
	expr = "1" + "+1" * n
	return f"module main;\npub fn main() nothrow -> Int {{\n\treturn {expr};\n}}\n"


def gen_else_if_chain(n: int) -> str:
	parts = []
	for i in range(n):
		parts.append(f"if x == {i} {{ return {i}; }}")
	body = " else ".join(parts) + " else { return -1; }"
	return f"module main;\npub fn main() nothrow -> Int {{\n\tvar x = 0;\n\t{body}\n}}\n"


def gen_huge_match(n: int) -> str:
	arms = []
	arms.append("variant E {")
	arms.extend([f"\tC{i}," for i in range(n)])
	arms.append("}")
	body_arms = "\n".join([f"\t\tE::C{i}() => {{ return {i}; }}," for i in range(n)])
	return (
		"module main;\n"
		+ "\n".join(arms) + "\n"
		+ "pub fn main() nothrow -> Int {\n"
		+ "\tvar e = E::C0();\n"
		+ "\tmatch e {\n" + body_arms + "\n\t}\n"
		+ "}\n"
	)


def gen_huge_struct(n: int) -> str:
	fields = ",\n\t".join(f"f{i}: Int" for i in range(n))
	inits = ", ".join(f"f{i} = {i}" for i in range(n))
	return (
		"module main;\n"
		f"struct S(\n\t{fields}\n);\n"
		"pub fn main() nothrow -> Int {\n"
		f"\tvar s = S({inits});\n"
		"\treturn s.f0;\n"
		"}\n"
	)


def gen_long_function_body(n: int) -> str:
	stmts = "\n\t".join(f"x = x + 1;" for _ in range(n))
	return (
		"module main;\n"
		"pub fn main() nothrow -> Int {\n"
		"\tvar x = 0;\n"
		f"\t{stmts}\n"
		"\treturn x;\n"
		"}\n"
	)


def gen_recursive_struct() -> str:
	# Direct self-reference; should be rejected.
	return (
		"module main;\n"
		"struct Node(child: Node);\n"
		"pub fn main() nothrow -> Int { return 0; }\n"
	)


def gen_mutually_recursive_struct() -> str:
	return (
		"module main;\n"
		"struct A(b: B);\n"
		"struct B(a: A);\n"
		"pub fn main() nothrow -> Int { return 0; }\n"
	)


def gen_many_generic_params(n: int) -> str:
	# Use the params in the signature so they're inferable, plus an explicit
	# type-args call so we don't trip on inference.
	params = ", ".join(f"T{i}" for i in range(n))
	type_args = ", ".join("Int" for _ in range(n))
	# Just declare each as the param type — first one used as arg, rest unused.
	return (
		"module main;\n"
		f"fn id<{params}>(x: T0) nothrow -> T0 {{ return x; }}\n"
		f"pub fn main() nothrow -> Int {{ return id::<{type_args}>(0); }}\n"
	)


def gen_nested_generic(n: int) -> str:
	# Box<Box<Box<...>>> nesting via Array<...> which exists in stdlib.
	t = "Int"
	for _ in range(n):
		t = f"Array<{t}>"
	return (
		"module main;\n"
		f"pub fn main() nothrow -> Int {{\n"
		f"\tvar x: {t};\n"
		"\treturn 0;\n"
		"}\n"
	)


def gen_huge_tuple_call(n: int) -> str:
	params = ", ".join(f"a{i}: Int" for i in range(n))
	args = ", ".join("0" for _ in range(n))
	return (
		"module main;\n"
		f"fn many({params}) nothrow -> Int {{ return a0; }}\n"
		"pub fn main() nothrow -> Int {\n"
		f"\treturn many({args});\n"
		"}\n"
	)


def gen_long_identifier(n: int) -> str:
	name = "x" * n
	return (
		"module main;\n"
		"pub fn main() nothrow -> Int {\n"
		f"\tvar {name} = 7;\n"
		f"\treturn {name};\n"
		"}\n"
	)


# ── Walk ────────────────────────────────────────────────────────────────


def walk():
	rows: list[dict] = []
	# For each category, sweep depths and pick the first failure threshold.
	def sweep(label: str, gen, depths: list[int], timeout: int = 30):
		first_fail = None
		last_ok = None
		for d in depths:
			r = run(f"{label} d={d}", gen(d), timeout=timeout)
			if r["shape"] == "ok":
				last_ok = d
			else:
				first_fail = (d, r)
				break
		if first_fail is None:
			rows.append({
				"label": f"{label}: ok up to d={last_ok}",
				"shape": "ok (no failure in sweep)",
				"phase": "n/a", "rc": 0, "stderr_head": "",
			})
		else:
			d, r = first_fail
			r["label"] = f"{label}: first fail at d={d} (last ok={last_ok})"
			rows.append(r)
	# Finer granularity around the cliffs found in run #1.
	sweep("nested_blocks", gen_nested_blocks, [200, 300, 400, 500])
	sweep("nested_if", gen_nested_if, [50, 100, 150, 200])
	sweep("nested_paren_expr", gen_nested_paren_expr, [200, 300, 400, 500])
	sweep("long_add_chain", gen_long_add_chain, [100, 200, 300, 500])
	sweep("else_if_chain", gen_else_if_chain, [50, 100, 150, 200])
	sweep("huge_match", gen_huge_match, [500, 700, 850, 1000])
	sweep("huge_struct", gen_huge_struct, [500, 1000, 2000, 5000])
	sweep("long_function_body", gen_long_function_body, [5000, 10000, 20000, 50000])
	sweep("many_generic_params", gen_many_generic_params, [10, 50, 200, 500])
	sweep("nested_generic", gen_nested_generic, [10, 50, 100, 200])
	sweep("huge_tuple_call", gen_huge_tuple_call, [50, 200, 500, 1000])
	sweep("long_identifier", gen_long_identifier, [1000, 5000, 10000])
	# Single-shot probes (not parameterized).
	rows.append({**run("recursive_struct (direct self-ref)", gen_recursive_struct()), "label": "recursive_struct (direct self-ref)"})
	rows.append({**run("mutually_recursive_struct", gen_mutually_recursive_struct()), "label": "mutually_recursive_struct"})
	report(rows)


if __name__ == "__main__":
	walk()
