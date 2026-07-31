# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""std.cli parse_with_builtins / ParseOutcome / version_output (W4).

Pins the five ratified behaviors — stamped default block, unstamped
fallback, simple `<app> <version>` mode, verbatim override, terminal
`--help`/`--version` semantics — plus the proof that plain `parse()`
remains policy-free (prints nothing, reports the request tags).

One Drift program dispatches on its own argv so each scenario is a RUN,
not a compile: four compiles total (unstamped, stamped, stamped with a
real package dependency for the `deps:` line, and unstamped WITH that
dependency for the fallback-purity pin).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

_PROG = '''module main;

import std.cli as cli;
import std.core as core;
import std.console as cons;

fn outcome_code(p: &cli.ArgParser, argv: &Array<String>) nothrow -> Int {
	match p.parse_with_builtins(argv) {
		cli.ParseOutcome::Args(_) => { return 100; },
		cli.ParseOutcome::Terminal(code) => { return code; },
		cli.ParseOutcome::Err(e) => {
			cons.eprintln(e.tag);
			return 101;
		}
	}
}

pub fn main(argv: Array<String>) nothrow -> Int {
	if argv.len < 2 { return 90; }
	val mode = "" + argv[1];
	if mode == "default" {
		var p = cli.parser("clidemo", "", "demo");
		val a: Array<String> = ["clidemo", "--version"];
		return outcome_code(p, a);
	}
	if mode == "simple" {
		var p = cli.parser("clidemo", "9.9.9", "demo");
		val a: Array<String> = ["clidemo", "--version"];
		return outcome_code(p, a);
	}
	if mode == "verbatim" {
		var p = cli.parser("clidemo", "9.9.9", "demo");
		p.version_output("custom line one\\nline two\\n");
		val a: Array<String> = ["clidemo", "-V"];
		return outcome_code(p, a);
	}
	if mode == "help" {
		var p = cli.parser("clidemo", "", "demo");
		val a: Array<String> = ["clidemo", "--help"];
		return outcome_code(p, a);
	}
	if mode == "err" {
		var p = cli.parser("clidemo", "", "demo");
		val a: Array<String> = ["clidemo", "--no-such-flag"];
		return outcome_code(p, a);
	}
	if mode == "verbatim_empty" {
		var p = cli.parser("clidemo", "9.9.9", "demo");
		p.version_output("");
		val a: Array<String> = ["clidemo", "--version"];
		return outcome_code(p, a);
	}
	if mode == "args" {
		var p = cli.parser("clidemo", "", "demo");
		p.flag("go", "g", "run it");
		val a: Array<String> = ["clidemo", "--go"];
		match p.parse_with_builtins(a) {
			cli.ParseOutcome::Args(parsed) => {
				if parsed.has_flag("go", p) {
					cons.println("args-ok:go");
					return 0;
				}
				return 93;
			},
			cli.ParseOutcome::Terminal(_) => { return 94; },
			cli.ParseOutcome::Err(_) => { return 95; }
		}
	}
	if mode == "plainparse" {
		// parse() stays policy-free: NOTHING on stdout, tag reported.
		var p = cli.parser("clidemo", "", "demo");
		val a: Array<String> = ["clidemo", "--version"];
		match p.parse(a) {
			core.Result::Ok(_) => { return 91; },
			core.Result::Err(e) => {
				cons.eprintln(e.tag);
				return 0;
			}
		}
	}
	return 92;
}
'''


def _compile(tmp_path: Path, name: str, extra: list[str],
             with_dep_pool: Path | None = None) -> Path:
	src = tmp_path / f"{name}.drift"
	src.write_text(_PROG)
	out = tmp_path / name
	cmd = [sys.executable, "-m", "lang.driftc.driftc", str(src),
	       "-M", str(tmp_path), "--entry", "main::main", "-o", str(out)]
	if with_dep_pool is not None:
		cmd += ["--package-root", str(with_dep_pool),
		        "--dep", "extlib@1.0.0",
		        "--allow-unsigned-from", str(with_dep_pool)]
	res = subprocess.run(cmd + extra, capture_output=True, text=True,
	                     cwd=ROOT, timeout=sanitizer_timeout(180))
	assert res.returncode == 0, res.stderr[-900:]
	return out


_STAMP = ["--artifact-name", "clidemo-artifact", "--artifact-version", "4.5.6",
          "--artifact-description", "cli demo artifact",
          "--artifact-license", "MIT"]


@pytest.fixture(scope="module")
def binaries(tmp_path_factory):
	d = tmp_path_factory.mktemp("cli_builtins")
	unstamped = _compile(d, "app_unstamped", [])
	stamped = _compile(d, "app_stamped", _STAMP)
	# Stamped + a real package dependency for the deps: line.
	pool = d / "pool"
	lib = pool / "src"
	lib.mkdir(parents=True)
	(lib / "extlib.drift").write_text(
		"module extlib;\nexport { answer };\n"
		"pub fn answer() nothrow -> Int { return 0; }\n")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",
		 "--target-word-bits", "64", "-M", str(pool),
		 str(lib / "extlib.drift"),
		 "--emit-package", str(pool / "extlib-1.0.0.dmp"),
		 "--package-id", "extlib", "--package-version", "1.0.0",
		 "--package-target", "test-target"],
		capture_output=True, text=True, cwd=ROOT,
		timeout=sanitizer_timeout(150))
	assert res.returncode == 0, res.stderr[-800:]
	dep_dir = tmp_path_factory.mktemp("cli_builtins_dep")
	dep_src = dep_dir / "app_deps.drift"
	dep_src.write_text(_PROG.replace(
		"import std.console as cons;",
		"import std.console as cons;\nimport extlib as extlib;"))
	dep_out = dep_dir / "app_deps"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", str(dep_src),
		 "-M", str(dep_dir), "--entry", "main::main", "-o", str(dep_out),
		 "--package-root", str(pool), "--dep", "extlib@1.0.0",
		 "--allow-unsigned-from", str(pool)] + _STAMP,
		capture_output=True, text=True, cwd=ROOT,
		timeout=sanitizer_timeout(180))
	assert res.returncode == 0, res.stderr[-900:]
	dep_unstamped = dep_dir / "app_deps_unstamped"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", str(dep_src),
		 "-M", str(dep_dir), "--entry", "main::main",
		 "-o", str(dep_unstamped),
		 "--package-root", str(pool), "--dep", "extlib@1.0.0",
		 "--allow-unsigned-from", str(pool)],
		capture_output=True, text=True, cwd=ROOT,
		timeout=sanitizer_timeout(180))
	assert res.returncode == 0, res.stderr[-900:]
	return {"unstamped": unstamped, "stamped": stamped, "deps": dep_out,
	        "deps_unstamped": dep_unstamped}


def _run(binary: Path, mode: str) -> subprocess.CompletedProcess:
	return subprocess.run([str(binary), mode], capture_output=True,
	                      text=True, timeout=sanitizer_timeout(30))


class TestVersionModes:
	def test_stamped_default_block(self, binaries) -> None:
		from lang.versions import DRIFTC_VERSION, DRIFT_RT_ABI_VERSION
		res = _run(binaries["stamped"], "default")
		assert res.returncode == 0
		assert res.stdout == (
			"clidemo 4.5.6\n"
			"cli demo artifact\n"
			f"driftc {DRIFTC_VERSION}, abi {DRIFT_RT_ABI_VERSION}\n"
			"license: MIT\n")

	def test_stamped_default_with_deps_line(self, binaries) -> None:
		res = _run(binaries["deps"], "default")
		assert res.returncode == 0
		assert res.stdout.endswith("deps: extlib@1.0.0\n")
		assert "clidemo 4.5.6\n" in res.stdout

	def test_unstamped_fallback(self, binaries) -> None:
		from lang.versions import DRIFTC_VERSION, DRIFT_RT_ABI_VERSION
		res = _run(binaries["unstamped"], "default")
		assert res.returncode == 0
		assert res.stdout == (
			"clidemo (unstamped)\n"
			f"driftc {DRIFTC_VERSION}, abi {DRIFT_RT_ABI_VERSION}\n")

	def test_simple_version_mode(self, binaries) -> None:
		res = _run(binaries["stamped"], "simple")
		assert res.returncode == 0
		assert res.stdout == "clidemo 9.9.9\n"

	def test_verbatim_empty_override_prints_nothing(self, binaries) -> None:
		"""version_output("") is a real override — the supplied
		verbatim EMPTY output, not a fall-through to simple mode."""
		res = _run(binaries["stamped"], "verbatim_empty")
		assert res.returncode == 0
		assert res.stdout == ""

	def test_unstamped_with_deps_keeps_exact_fallback(self, binaries) -> None:
		"""Ratified fallback: EXACTLY "<app> (unstamped)" + the
		compiler line — a dependency-bearing unstamped binary must NOT
		print a deps: line."""
		from lang.versions import DRIFTC_VERSION, DRIFT_RT_ABI_VERSION
		res = _run(binaries["deps_unstamped"], "default")
		assert res.returncode == 0
		assert res.stdout == (
			"clidemo (unstamped)\n"
			f"driftc {DRIFTC_VERSION}, abi {DRIFT_RT_ABI_VERSION}\n")

	def test_verbatim_override_wins(self, binaries) -> None:
		res = _run(binaries["stamped"], "verbatim")
		assert res.returncode == 0
		assert res.stdout == "custom line one\nline two\n"


class TestTerminalAndParseUnchanged:
	def test_help_terminal(self, binaries) -> None:
		res = _run(binaries["stamped"], "help")
		assert res.returncode == 0
		assert res.stdout.startswith("Usage: clidemo")

	def test_err_passthrough(self, binaries) -> None:
		res = _run(binaries["stamped"], "err")
		assert res.returncode == 101
		assert res.stdout == ""
		assert "unknown-option" in res.stderr

	def test_args_path_runtime(self, binaries) -> None:
		"""The ordinary ParseOutcome::Args path at runtime."""
		res = _run(binaries["stamped"], "args")
		assert res.returncode == 0
		assert res.stdout == "args-ok:go\n"

	def test_plain_parse_stays_policy_free(self, binaries) -> None:
		res = _run(binaries["stamped"], "plainparse")
		assert res.returncode == 0
		assert res.stdout == "", "parse() must print NOTHING"
		assert "cli-version-requested" in res.stderr
