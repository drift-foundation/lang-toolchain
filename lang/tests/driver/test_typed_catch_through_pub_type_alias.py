# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG carrier: typed-catch through a `pub type Alias =
inner.PubError` re-export loses the underlying Path-A struct schema.

Bisected by the mariadb-rpc team alongside app-team issue #2; called
out by the user as §B follow-up after #2 landed.  This file pins the
regression in its own driver test (separate from the §A interface-
metadata regression to keep root-cause classification clean).

**Shape:**

  // inner module / inner package
  pub error ManagedError { tag: String }

  // outer / re-export
  pub type ManagedError = inner.ManagedError;

  // consumer (catch syntax uses colon-qualified module:Name, NOT dot)
  try { ... } catch outer:ManagedError(e) {
      val t = e.tag;        // pre-fix: E_TYPED_CATCH_FIELD_NOT_IN_SCHEMA
  }

**Pre-fix shape** (carrier verified deterministic at 0.31.102):

  <source>:N:M: error: field 'tag' is not declared on `pub error`
    type '<outer-mod>:ManagedError' and is not an Error envelope
    method/field [E_TYPED_CATCH_FIELD_NOT_IN_SCHEMA]

Note the FQN in the diagnostic: it says `<outer-mod>:ManagedError`
rather than the underlying `<inner-mod>:ManagedError`.  The
type-checker's typed-catch field-schema lookup at
`type_checker.py:9061` runs:

    struct_id = type_table.get_nominal(
        kind=TypeKind.STRUCT,
        module_id=<outer-mod>,         # alias module, NOT def module
        name="ManagedError",
    )

`get_nominal` queries by (kind, module, name).  The Path-A struct face
for `pub error ManagedError` was registered against the DEFINING
module (`<inner-mod>`), not the alias module.  Lookup misses,
`struct_id is None`, control falls through to "field not declared on
`pub error`" → `E_TYPED_CATCH_FIELD_NOT_IN_SCHEMA`.

`pub type` aliases of regular STRUCTs are already canonicalized via
`_resolve_alias_target_struct` in `parser/__init__.py:4688`; the
typed-catch path missed the same canonicalization.  This is a
parallel/sibling slice -- not a new design.

**Fix shape (designed; reviewed before implementation):**
Either (a) resolve the event_fqn through the alias chain at parse /
stage1 lowering time (so by the time the type-checker reads
`arm.event_fqn` it's already the canonical pub-error FQN), or (b)
follow the alias chain at the type-checker's `get_nominal` lookup
site.  Both compile-time only -- no ABI / package-format change.
Approach selected after read-only investigation; see history entry.

Carriers (matching the same-pkg + cross-pkg coverage explicitly
requested):

  V1.  Control: same-package, catch via direct `pub error` name --
       must pass on both pre- and post-fix.  Locks the failure axis to
       the alias path specifically.
  V2.  THE BUG (same-package): `pub type Alias = Inner` then catch via
       `Alias(e)` then `e.scalar_field`.  Pre-fix:
       `E_TYPED_CATCH_FIELD_NOT_IN_SCHEMA`.  Post-fix: compile + run
       clean.
  V3.  THE BUG (cross-package): producer pkg has `pub error Inner` +
       `pub type Alias = Inner`; consumer catches via `producer.Alias`.
       Pins that the fix carries the canonicalization through package
       metadata round-trip, not just same-compilation alias resolution.
"""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _compile_source(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
	"""Compile a single-source consumer (no packages).  Used for V1/V2."""
	src_dir = tmp_path / "src"
	src_dir.mkdir(exist_ok=True)
	src = src_dir / "main.drift"
	src.write_text(source)
	out_bin = tmp_path / "main_bin"
	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--target-word-bits", "64",
		"--stdlib-root", str(ROOT / "stdlib"),
		"--entry", "main::main",
		str(src),
		"-o", str(out_bin),
	]
	return subprocess.run(
		cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=60,
	)


def _build_and_sign_pkg(
	tmp_path: Path,
	pkg_id: str,
	sources: dict[str, str],
	deps: list[tuple[str, str]] | None = None,
	priv_key_bytes: bytes | None = None,
	trust_path_existing: Path | None = None,
) -> tuple[Path, Path, bytes]:
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from lang.driftc.packages.signature_v0 import compute_ed25519_kid

	lib_dir = tmp_path / f"{pkg_id}_src"
	lib_dir.mkdir(exist_ok=True)
	for fname, text in sources.items():
		(lib_dir / fname).write_text(text)
	pkg_root_dir = tmp_path / "pkg_root" / pkg_id / "0.1.0"
	pkg_root_dir.mkdir(parents=True, exist_ok=True)
	dmp = pkg_root_dir / f"{pkg_id}.dmp"
	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--dev", "-M", str(lib_dir), "--stdlib-root", str(ROOT / "stdlib"),
	]
	if deps:
		cmd += ["--package-root", str(tmp_path / "pkg_root")]
		for dep_id, dep_ver in deps:
			cmd += ["--dep", f"{dep_id}@{dep_ver}"]
		if trust_path_existing is not None:
			cmd += ["--trust-store", str(trust_path_existing)]
	for fname in sources:
		cmd.append(str(lib_dir / fname))
	cmd += [
		"--package-id", pkg_id,
		"--package-version", "0.1.0",
		"--package-target", "drift-dev",
		"--emit-package", str(dmp),
		"--test-build-only",
	]
	res = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=60)
	assert res.returncode == 0, f"build of {pkg_id} failed:\n{res.stderr[-1500:]}"

	if priv_key_bytes is None:
		priv = Ed25519PrivateKey.generate()
		priv_key_bytes = priv.private_bytes_raw()
	else:
		priv = Ed25519PrivateKey.from_private_bytes(priv_key_bytes)
	pub_raw = priv.public_key().public_bytes_raw()
	kid = compute_ed25519_kid(pub_raw)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")
	pkg_bytes = dmp.read_bytes()
	sig = priv.sign(pkg_bytes)
	(dmp.with_suffix(".sig")).write_text(json.dumps({
		"format": "dmir-pkg-sig", "version": 0,
		"package_sha256": f"sha256:{hashlib.sha256(pkg_bytes).hexdigest()}",
		"signatures": [{
			"algo": "ed25519", "kid": kid,
			"sig": base64.b64encode(sig).decode("ascii"),
			"pubkey": pub_b64,
		}],
	}, separators=(",", ":"), sort_keys=True))

	if trust_path_existing is not None:
		trust_path = trust_path_existing
	else:
		trust_path = tmp_path / "trust.json"
		trust_path.write_text(json.dumps({
			"format": "drift-trust", "version": 0,
			"keys": {kid: {"algo": "ed25519", "pubkey": pub_b64}},
			"namespaces": {
				"producer_pkg.*": [kid],
				"std.*": [kid],
			},
			"revoked": [],
		}, separators=(",", ":"), sort_keys=True))
	return tmp_path / "pkg_root", trust_path, priv_key_bytes


def _compile_consumer(
	tmp_path: Path,
	pkg_root: Path,
	trust_path: Path,
	deps: list[tuple[str, str]],
	source: str,
) -> subprocess.CompletedProcess[str]:
	src_dir = tmp_path / "consumer_src"
	src_dir.mkdir(exist_ok=True)
	src = src_dir / "main.drift"
	src.write_text(source)
	out_bin = tmp_path / "main_bin"
	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--target-word-bits", "64",
		"--stdlib-root", str(ROOT / "stdlib"),
		"--package-root", str(pkg_root),
	]
	for dep_id, dep_ver in deps:
		cmd += ["--dep", f"{dep_id}@{dep_ver}"]
	cmd += [
		"--trust-store", str(trust_path),
		"--entry", "main::main",
		str(src),
		"-o", str(out_bin),
	]
	return subprocess.run(
		cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=60,
	)


# ─── V1: control (direct pub-error catch, no alias) ────────────────


_V1_SRC = """\
module main;
import std.core as core;

pub error Inner { tag: String }

pub fn maybe_throw(want: Bool) throws Inner -> Int {
	if want { throw Inner(tag = "hit"); }
	return 0;
}

pub fn main() nothrow -> Int {
	try {
		val n = maybe_throw(true);
		return 99;
	} catch main:Inner(e) {
		if e.tag == "hit" { return 0; }
		return 1;
	}
}
"""


def test_v1_control_direct_pub_error_catch(tmp_path: Path) -> None:
	"""Control: same-module catch via the direct `pub error` name
	must succeed.  Locks the failure axis (in V2/V3) to the alias
	path specifically -- if this regresses, the bug is elsewhere."""
	res = _compile_source(tmp_path, _V1_SRC)
	assert res.returncode == 0, (
		f"V1 control compile failed -- the basic typed-catch + "
		f"scalar-field projection path is broken; bug is not "
		f"alias-specific.\n\n{res.stderr[-1500:]}"
	)
	out_bin = tmp_path / "main_bin"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=10)
	assert run.returncode == 0, f"V1 binary exited {run.returncode}; expected 0"


# ─── V2: THE BUG (same-module, `pub type Alias = Inner`) ───────────


_V2_SRC = """\
module main;
import std.core as core;

pub error Inner { tag: String }
pub type Alias = Inner;

pub fn maybe_throw(want: Bool) throws Inner -> Int {
	if want { throw Inner(tag = "hit"); }
	return 0;
}

pub fn main() nothrow -> Int {
	try {
		val n = maybe_throw(true);
		return 99;
	} catch main:Alias(e) {
		if e.tag == "hit" { return 0; }
		return 1;
	}
}
"""


def test_v2_same_module_catch_via_pub_type_alias(tmp_path: Path) -> None:
	"""THE BUG (same-module): `pub type Alias = Inner` then catch
	via `Alias(e)` then `e.scalar_field` fails with
	`E_TYPED_CATCH_FIELD_NOT_IN_SCHEMA` because the typed-catch
	field-schema lookup uses the alias's `module_id`, not the
	underlying pub-error's defining module.

	Post-fix expectation: compile + run; binary returns 0 (tag
	matched 'hit')."""
	res = _compile_source(tmp_path, _V2_SRC)
	assert "E_TYPED_CATCH_FIELD_NOT_IN_SCHEMA" not in res.stderr, (
		f"V2: typed-catch field-schema lookup still fails on the "
		f"`pub type Alias = Inner` re-export path.  The alias "
		f"canonicalization at typed-catch registration / field "
		f"lookup was reverted or never landed.\n\n{res.stderr[-1500:]}"
	)
	assert res.returncode == 0, (
		f"V2 compile failed but NOT with the known shape:\n"
		f"{res.stderr[-1500:]}"
	)
	out_bin = tmp_path / "main_bin"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=10)
	assert run.returncode == 0, (
		f"V2 binary exited {run.returncode}; expected 0 (tag should "
		f"match 'hit')"
	)


# ─── V3: THE BUG (cross-package alias of pub-error) ────────────────


# V3 producer is a single-module package containing both the
# `pub error` and the `pub type` alias.  This is the smallest cross-
# package carrier that still exercises the package-metadata round-trip
# for the alias.  V4 covers the multi-module facade pattern (maria's
# actual `mariadb.rpc.managed` + `mariadb.rpc.api` shape).

_V3_PRODUCER_SRC = """\
module producer_pkg;
import std.core as core;
export { Inner, Alias, do_throw };

pub error Inner { tag: String }
pub type Alias = Inner;

pub fn do_throw() throws Inner -> Int { throw Inner(tag = "hit"); }
"""

_V3_CONSUMER_SRC = """\
module main;
import std.core as core;
import producer_pkg as producer_pkg;

pub fn main() nothrow -> Int {
	try {
		val n = producer_pkg.do_throw();
		return 99;
	} catch producer_pkg:Alias(e) {
		if e.tag == "hit" { return 0; }
		return 1;
	}
}
"""


# Signature of the known cross-package typed-throws metadata gap
# (orthogonal to §B, pre-existing).  Used by V3/V4 to runtime-xfail
# the test ONLY when this specific upstream gap is what's blocking
# them -- so an actual §B regression still fails loud.
_KNOWN_CROSS_PKG_THROWS_GAP_MARKER = "is declared nothrow but may throw"


def test_v3_cross_package_catch_via_pub_type_alias(tmp_path: Path) -> None:
	"""THE BUG (cross-package, single-module producer): same shape as
	V2, but the `pub type Alias = Inner` re-export lives in a producer
	package.  Pins that the alias canonicalization carries through
	package metadata round-trip, not just same-compilation alias
	resolution.

	If V2 passes but V3 fails, the canonicalization works for
	in-source aliases but the package-metadata round-trip drops
	alias-target identity.

	Note on test layout: decorator-level `@pytest.mark.xfail` was
	intentionally NOT used here because it would mask a real §B
	regression -- if the alias schema lookup regressed back to
	`E_TYPED_CATCH_FIELD_NOT_IN_SCHEMA`, a strict-xfail test would
	still report xfail (the test "failed as expected") instead of a
	real failure.  Instead we ASSERT the §B-specific shape FIRST,
	then runtime-xfail only when the failure matches the known
	upstream gap.

	Post-fix expectation (once cross-pkg narrow-throws metadata
	slice lands): compile + run; binary returns 0."""
	pkg_root, trust_path, _priv = _build_and_sign_pkg(
		tmp_path, "producer_pkg", {"producer.drift": _V3_PRODUCER_SRC},
	)
	res = _compile_consumer(
		tmp_path, pkg_root, trust_path,
		deps=[("producer_pkg", "0.1.0")],
		source=_V3_CONSUMER_SRC,
	)
	# §B-specific assertion FIRST.  If this fires, the alias schema
	# lookup regressed -- the test must fail loud, not xfail.
	assert "E_TYPED_CATCH_FIELD_NOT_IN_SCHEMA" not in res.stderr, (
		f"V3 §B REGRESSION: cross-package alias schema lookup broken "
		f"(`pub type Alias = Inner` re-export drops the underlying "
		f"Path-A struct schema).  This is the bug §B fixes; if it's "
		f"back, the alias canonicalization in `type_checker.py:"
		f"_canonical_pub_error_fqn`, `checker/__init__.py:"
		f"_alias_to_pub_error_fqn`, or `hir_to_mir.py:"
		f"_canonical_event_fqn_for_alias` was reverted or never "
		f"covered the cross-pkg path.\n\n{res.stderr[-1500:]}"
	)
	# Known orthogonal gap: cross-package typed-throws coverage gap
	# (producer's `throws Inner` declaration not round-tripped through
	# package metadata).  Runtime-xfail only when the failure matches
	# THIS specific signature, so other unexpected failures still
	# show up as hard failures.  Verified 2026-05-17 by re-running
	# with `catch producer_pkg:Inner(e)` (direct, no alias) -- SAME
	# failure shape, confirming this is independent of §B.  Drop the
	# xfail and the marker check below when the cross-pkg narrow-
	# throws metadata slice lands.
	if res.returncode != 0 and _KNOWN_CROSS_PKG_THROWS_GAP_MARKER in res.stderr:
		pytest.xfail(
			"cross-package typed-throws metadata gap (orthogonal "
			"to §B alias canonicalization)"
		)
	assert res.returncode == 0, (
		f"V3 compile failed with an unexpected shape (not the §B "
		f"alias schema bug, not the known cross-pkg throws gap):\n"
		f"{res.stderr[-1500:]}"
	)
	out_bin = tmp_path / "main_bin"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=10)
	assert run.returncode == 0, f"V3 binary exited {run.returncode}; expected 0"


# ─── V4: facade-module shape (maria team's actual carrier) ─────────


_V4_INNER_SRC = """\
module producer_pkg.inner;
import std.core as core;
export { ManagedError, do_throw };

pub error ManagedError { tag: String }

pub fn do_throw() throws producer_pkg.inner.ManagedError -> Int {
	throw ManagedError(tag = "hit");
}
"""

_V4_API_SRC = """\
module producer_pkg.api;
import std.core as core;
import producer_pkg.inner as inner;
export { ManagedError };

pub type ManagedError = inner.ManagedError;
"""

_V4_CONSUMER_SRC = """\
module main;
import std.core as core;
import producer_pkg.inner as inner;
import producer_pkg.api as api;

pub fn main() nothrow -> Int {
	try {
		val n = inner.do_throw();
		return 99;
	} catch api:ManagedError(e) {
		if e.tag == "hit" { return 0; }
		return 1;
	}
}
"""


def test_v4_facade_module_catch_via_pub_type_alias(tmp_path: Path) -> None:
	"""Facade-module shape -- maria's actual `mariadb.rpc.managed` +
	`mariadb.rpc.api` carrier.  Producer pkg has TWO modules: `inner`
	with `pub error ManagedError`, `api` re-exporting via
	`pub type ManagedError = inner.ManagedError`.  Consumer imports
	both (calls inner.do_throw, catches via api:ManagedError).

	Pins that alias canonicalization works when the alias and the
	underlying pub-error live in different modules within the same
	package -- this is the structural shape the maria team uses, and
	it's a stricter test than V3 (single-module producer) because
	the alias's `module_id` and the pub-error's `module_id` differ.

	See V3 docstring for the runtime-xfail-vs-decorator-xfail
	rationale: §B-specific assertion runs first so regressions
	can't hide behind the orthogonal cross-pkg throws gap.

	Post-fix expectation (once cross-pkg narrow-throws metadata
	slice lands): compile + run; binary returns 0."""
	pkg_root, trust_path, _priv = _build_and_sign_pkg(
		tmp_path, "producer_pkg",
		{
			"inner.drift": _V4_INNER_SRC,
			"api.drift": _V4_API_SRC,
		},
	)
	res = _compile_consumer(
		tmp_path, pkg_root, trust_path,
		deps=[("producer_pkg", "0.1.0")],
		source=_V4_CONSUMER_SRC,
	)
	# §B-specific assertion FIRST -- a regression here must fail
	# loud, not get masked by the orthogonal cross-pkg gap.
	assert "E_TYPED_CATCH_FIELD_NOT_IN_SCHEMA" not in res.stderr, (
		f"V4 §B REGRESSION: facade-module typed-catch through "
		f"`pub type` re-export drops the underlying Path-A struct "
		f"schema.  The alias's `module_id` differs from the underlying "
		f"`pub error`'s `module_id` -- the alias-chain walk must "
		f"follow the target's `module_id` (not stay at the alias's "
		f"module).  Check `_canonical_pub_error_fqn` /\n"
		f"`_alias_to_pub_error_fqn` / `_canonical_event_fqn_for_alias`.\n"
		f"\n{res.stderr[-1500:]}"
	)
	if res.returncode != 0 and _KNOWN_CROSS_PKG_THROWS_GAP_MARKER in res.stderr:
		pytest.xfail(
			"cross-package typed-throws metadata gap (orthogonal "
			"to §B alias canonicalization) -- same root as V3"
		)
	assert res.returncode == 0, (
		f"V4 compile failed with an unexpected shape:\n"
		f"{res.stderr[-1500:]}"
	)
	out_bin = tmp_path / "main_bin"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=10)
	assert run.returncode == 0, f"V4 binary exited {run.returncode}; expected 0"
