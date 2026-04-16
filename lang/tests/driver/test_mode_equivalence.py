# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Mode equivalence: source-compiled and package-compiled programs must
produce identical runtime behavior.

This is the Option B invariant: after ingress, there is one semantic
pipeline. Package origin does not change compilation semantics.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _compile_and_run(
	tmp_path: Path,
	source: str,
	*,
	use_package_stdlib: bool,
	label: str,
) -> tuple[int, str, str]:
	"""Compile and run consumer, returning (exit_code, stdout, stderr)."""
	from lang.driftc.parser import stdlib_root
	consumer_dir = tmp_path / f"{label}_src"
	consumer_dir.mkdir(exist_ok=True)
	(consumer_dir / "consumer.drift").write_text(source)
	out_bin = tmp_path / f"{label}_bin"

	if use_package_stdlib:
		from lang.tests.driver.pkg_test_helpers import _build_signed_stdlib, STD_VERSION
		pkg_root, trust_path, core_trust_path, empty_stdlib = _build_signed_stdlib(tmp_path)
		argv = [
			sys.executable, "-m", "lang.driftc.driftc", "--dev",
			str(consumer_dir / "consumer.drift"),
			"--stdlib-root", str(empty_stdlib),
			"--package-root", str(pkg_root),
			"--dep", f"std@{STD_VERSION}",
			"--trust-store", str(trust_path),
			"--dev-core-trust-store", str(core_trust_path),
			"--target-word-bits", "64",
			"--entry", "consumer::main",
			"-o", str(out_bin),
		]
	else:
		argv = [
			sys.executable, "-m", "lang.driftc.driftc", "--dev",
			str(consumer_dir / "consumer.drift"),
			"--stdlib-root", str(stdlib_root()),
			"--entry", "consumer::main",
			"-o", str(out_bin),
		]

	res = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=180)
	if res.returncode != 0:
		return -1, "", res.stderr[:500]
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=10)
	return run.returncode, run.stdout, run.stderr


EQUIVALENCE_SOURCE = """\
module consumer;
import std.core as core;
import std.format as fmt;
import std.containers as containers;
import std.log as log;

fn consume_map(m: containers.HashMap<String, String>) nothrow -> Int {
\treturn m.len();
}

pub fn main() nothrow -> Int {
\t// Exercise format_int + map literal + HashMap
\tval n = consume_map({"a": fmt.format_int(1), "b": fmt.format_int(2)});
\tif n != 2 { return 1; }

\t// Exercise Cell<T> generic
\tvar count = core.cell(0);
\tcount.set(count.get() + 1);
\tif count.get() != 1 { return 2; }

\t// Exercise logger (full emit path)
\tvar cb = log.config_builder();
\tcb.sink(log.stderr_sink());
\tcb.min_level(log.Level::Error());
\tval cfg = cb.build();
\tval logger = log.create_logger("test", cfg);
\tlogger.info("test", {"k": fmt.format_int(42)});

\treturn 0;
}
"""


def test_source_and_package_produce_same_exit_code(tmp_path: Path) -> None:
	"""Same program compiled via source and package must produce same exit code."""
	src_rc, src_out, src_err = _compile_and_run(
		tmp_path, EQUIVALENCE_SOURCE, use_package_stdlib=False, label="source"
	)
	pkg_rc, pkg_out, pkg_err = _compile_and_run(
		tmp_path, EQUIVALENCE_SOURCE, use_package_stdlib=True, label="package"
	)
	assert src_rc == 0, f"source mode failed: {src_err}"
	assert pkg_rc == 0, f"package mode failed: {pkg_err}"
	assert src_rc == pkg_rc, (
		f"mode divergence: source exit={src_rc}, package exit={pkg_rc}"
	)
