# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 1 Stage 3 — Step 1 regression: `conc.arc<T>(value)` must
reject calls where the resolved `T` is an interface type.

The fat `Arc<Interface>` representation constructs its
`{ctrl, data, vtable}` layout from a concrete allocation and an
explicit `as_interface<I>()` coercion.  Allowing `conc.arc(iface_value)`
directly would either (a) silently store the interface fat-pointer
as the "value" field of an `ArcBox<InterfaceFatPtr>` — breaking the
one-allocation / one-control-block invariant — or (b) require a
separate runtime path for "Arc of something that is already an
interface," which the design explicitly avoids.

The diagnostic must:
- Fire at typecheck, not codegen.
- Direct the user to the supported spelling:
  `conc.arc(concrete).as_interface<type I>()`.
- NOT fire for `conc.arc(concrete_struct_value)` — the common
  case — or for nested generic T (e.g. `conc.arc(Arc<X>)`).

Stage 2 allowed this construction because the layout specialization
for Arc<Interface> did not exist yet; Stage 3 forbids it as a
precondition of turning on the fat layout.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _compile_capture(tmp_path: Path, source: str) -> tuple[int, str]:
	"""Compile `source` as main::main via the in-process driver.
	Returns (rc, stderr) — stderr is captured by running through a
	subprocess so diagnostic text is available for substring checks.
	"""
	mod_root = tmp_path / "mods"
	main_src = mod_root / "main" / "main.drift"
	_write_file(main_src, source)
	out_bin = tmp_path / "out"
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		str(main_src),
		"-M", str(mod_root),
		"-o", str(out_bin),
		"--dev",
	]
	root = stdlib_root()
	if root:
		cmd += ["--stdlib-root", str(root)]
	env = {}
	import os
	env.update(os.environ)
	env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3])
	res = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
	return res.returncode, (res.stderr or "")


_ARC_OF_INTERFACE_REJECTED = """
module main;

import std.concurrent as conc;

pub interface Speaker {
	fn speak(self: &Self) nothrow -> Int;
}

pub struct Dog {
	pub tag: Int
}

implement Speaker for Dog {
	pub fn speak(self: &Dog) nothrow -> Int {
		return self.tag;
	}
}

fn main() nothrow -> Int {
	// Construct a concrete Arc<Dog>, then coerce ONCE to Arc<Speaker>.
	// Downstream code then tries to re-wrap the interface view in
	// another `conc.arc(...)` — this is the shape Stage 3 rejects.
	var d: Speaker = Dog(tag = 1);
	val oops = conc.arc(move d);   // <-- must be rejected.
	return 0;
}
""".lstrip()


_ARC_OF_CONCRETE_STILL_WORKS = """
module main;

import std.concurrent as conc;

pub struct Payload {
	pub n: Int
}

fn main() nothrow -> Int {
	// Plain Arc<Concrete> — the common case.  Must keep compiling.
	val p = conc.arc(Payload(n = 42));
	return p.get().n - 42;
}
""".lstrip()


_NESTED_GENERIC_ARC_STILL_WORKS = """
module main;

import std.concurrent as conc;

pub struct Inner {
	pub v: Int
}

fn main() nothrow -> Int {
	// `Arc<Arc<Inner>>` — Inner is concrete, outer T is
	// `conc.Arc<Inner>` which is a STRUCT, not an interface.
	// Must still compile — the rejection is only for T=interface.
	var inner = conc.arc(Inner(v = 7));
	val outer = conc.arc(move inner);
	return 0;
}
""".lstrip()


def test_arc_of_interface_is_rejected_at_typecheck(tmp_path: Path) -> None:
	"""The core gate: `conc.arc(iface_value)` is rejected at
	typecheck.  Rejection must fire before MIR lowering."""
	rc, stderr = _compile_capture(tmp_path, _ARC_OF_INTERFACE_REJECTED)
	assert rc != 0, (
		"conc.arc<T>(iface_value) compiled cleanly — Stage 3 requires "
		f"this to be a typecheck error.\nstderr:\n{stderr}"
	)
	# Must be a typecheck diagnostic, not a codegen / MIR internal.
	assert "internal:" not in stderr, (
		"rejection must fire at typecheck, not as an internal "
		f"compiler error.\nstderr:\n{stderr}"
	)
	# Pin the diagnostic code — this is the user-facing contract.
	assert "E_ARC_OF_INTERFACE_DIRECT" in stderr, (
		"rejection diagnostic must carry the `E_ARC_OF_INTERFACE_DIRECT` "
		f"code so downstream tooling can key off it.\nstderr:\n{stderr}"
	)
	# The diagnostic should point at the supported spelling so the
	# user knows what to do.  Accept several natural phrasings.
	directive_ok = (
		"as_interface" in stderr
		or "concrete" in stderr.lower()
	)
	assert directive_ok, (
		"rejection diagnostic should direct the caller to "
		"`conc.arc(concrete).as_interface<type I>()` but the stderr "
		f"mentions neither 'as_interface' nor 'concrete'.\n"
		f"stderr:\n{stderr}"
	)


def test_arc_of_concrete_struct_still_compiles(tmp_path: Path) -> None:
	"""Negative control: the rejection must NOT fire for plain
	`conc.arc(concrete_struct)` — that is the most common usage
	site and keeps working across Stage 3."""
	rc, stderr = _compile_capture(tmp_path, _ARC_OF_CONCRETE_STILL_WORKS)
	assert rc == 0, (
		f"Arc<ConcreteStruct> construction broken by Stage 3 Step 1 "
		f"rejection — this must keep working.\nstderr:\n{stderr}"
	)


def test_nested_arc_of_arc_still_compiles(tmp_path: Path) -> None:
	"""Negative control: `conc.arc(conc.arc(Inner{...}))` is
	`Arc<Arc<Inner>>`.  The outer T is a STRUCT (Arc), not an
	interface, so the Stage 3 rejection must not fire."""
	rc, stderr = _compile_capture(tmp_path, _NESTED_GENERIC_ARC_STILL_WORKS)
	assert rc == 0, (
		f"Arc<Arc<Inner>> rejected by Stage 3 Step 1 — the rejection "
		f"must be specific to T=interface, not T=struct-that-happens-to-be-Arc.\n"
		f"stderr:\n{stderr}"
	)
