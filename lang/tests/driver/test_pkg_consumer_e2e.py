# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Package-consumer e2e driver tests.

These tests compile programs against stdlib loaded as a signed .dmp package
(--package-root + --dep std@VERSION) and exercise code paths that are only
reachable through the package-consumer pipeline.

ASAN-compatible: the spawned driftc subprocess honors DRIFT_ASAN=1 and
selects the ASAN runtime archive + -fsanitize=address automatically.

Migrated from lang/tests/codegen/e2e/ cases marked package_consumer_only.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _compile_consumer(
	source: str,
	*,
	stdlib_pkg: "StdlibPackage",
	tmp_path: Path,
	entry: str = "main::main",
	expect_failure: bool = False,
) -> "subprocess.CompletedProcess[str] | Path":
	"""Compile a consumer program against stdlib as a package.

	When expect_failure is False (default), asserts compile succeeds and
	returns the path to the linked binary.

	When expect_failure is True, returns the CompletedProcess so the caller
	can assert on diagnostics and return code.
	"""
	from conftest import StdlibPackage  # type: ignore[import]

	src_dir = tmp_path / "src"
	src_dir.mkdir(exist_ok=True)
	(src_dir / "main.drift").write_text(source)

	out_bin = tmp_path / "test_bin"
	empty_stdlib = tmp_path / "_empty_stdlib"
	empty_stdlib.mkdir(exist_ok=True)
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		str(src_dir / "main.drift"),
		"--stdlib-root", str(empty_stdlib),
		"--target-word-bits", "64",
		"--package-root", str(stdlib_pkg.pkg_root),
		"--dep", f"std@{stdlib_pkg.version}",
		"--trust-store", str(stdlib_pkg.trust_path),
		"--dev", "--dev-core-trust-store", str(stdlib_pkg.trust_path),
		"--entry", entry,
		"-o", str(out_bin),
	]

	assert str(stdlib_pkg.stdlib_root) not in " ".join(cmd), (
		"consumer compile must not use the real stdlib source tree"
	)

	res = subprocess.run(
		cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)

	if expect_failure:
		return res

	assert res.returncode == 0, (
		f"consumer compile failed (stdlib-as-package path):\n{res.stderr[:500]}"
	)
	assert out_bin.exists(), "binary not produced"
	return out_bin


def _run_binary(binary: Path, timeout: int = 30) -> subprocess.CompletedProcess[str]:
	"""Run a compiled binary and return the result."""
	return subprocess.run(
		[str(binary)], capture_output=True, text=True, timeout=sanitizer_timeout(timeout),
	)


def _emit_package_consumer(
	source: str,
	*,
	stdlib_pkg: "StdlibPackage",
	tmp_path: Path,
	package_id: str = "consumer",
	package_version: str = "0.0.1",
) -> subprocess.CompletedProcess[str]:
	"""Build the consumer source as a package via --emit-package, against
	stdlib loaded as a signed .dmp.

	Mirrors the certified-toolchain producer flow (the certified driftc
	auto-injects --package-root + --dep std@VERSION; here we set them
	explicitly).  Returns the completed process so callers can inspect
	stderr for diagnostics.
	"""
	src_dir = tmp_path / "src"
	src_dir.mkdir(exist_ok=True)
	(src_dir / "main.drift").write_text(source)
	empty_stdlib = tmp_path / "_empty_stdlib"
	empty_stdlib.mkdir(exist_ok=True)
	out_dmp = tmp_path / f"{package_id}.dmp"
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		str(src_dir / "main.drift"),
		"--stdlib-root", str(empty_stdlib),
		"--target-word-bits", "64",
		"--package-root", str(stdlib_pkg.pkg_root),
		"--dep", f"std@{stdlib_pkg.version}",
		"--trust-store", str(stdlib_pkg.trust_path),
		"--dev", "--dev-core-trust-store", str(stdlib_pkg.trust_path),
		"--emit-package", str(out_dmp),
		"--package-id", package_id,
		"--package-version", package_version,
		"--package-target", "drift-dev",
		"--source-content-id",
		"sha256:0000000000000000000000000000000000000000000000000000000000000000",
	]
	return subprocess.run(
		cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)


def _publish_library_package(
	source_files: dict[str, str],
	*,
	stdlib_pkg: "StdlibPackage",
	tmp_path: Path,
	package_id: str,
	package_version: str = "0.0.1",
	module_namespace: str | None = None,
) -> tuple[Path, Path]:
	"""Build + sign a library package (multi-file source) and return
	(pkg_root, library_trust_path).

	Returns a `pkg_root` containing both the stdlib package symlink and
	the new library package, plus a trust store covering both.  The
	caller can hand `pkg_root` and `library_trust_path` to a consumer
	compile that imports symbols from the published library.

	`source_files` is `{relative_path: content}` — supports multiple
	.drift files for cross-module library shapes.
	"""
	import base64
	from hashlib import sha256
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from cryptography.hazmat.primitives import serialization
	from lang.drift.crypto import compute_ed25519_kid

	src_dir = tmp_path / f"src_{package_id}"
	src_dir.mkdir(parents=True, exist_ok=True)
	src_paths: list[str] = []
	for rel, content in source_files.items():
		p = src_dir / rel
		p.parent.mkdir(parents=True, exist_ok=True)
		p.write_text(content)
		src_paths.append(str(p))
	empty_stdlib = tmp_path / "_empty_stdlib"
	empty_stdlib.mkdir(exist_ok=True)
	out_dmp = tmp_path / f"{package_id}.dmp"
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		"--stdlib-root", str(empty_stdlib),
		"--target-word-bits", "64",
		"--package-root", str(stdlib_pkg.pkg_root),
		"--dep", f"std@{stdlib_pkg.version}",
		"--trust-store", str(stdlib_pkg.trust_path),
		"--dev", "--dev-core-trust-store", str(stdlib_pkg.trust_path),
		"--emit-package", str(out_dmp),
		"--package-id", package_id,
		"--package-version", package_version,
		"--package-target", "drift-dev",
		"--source-content-id",
		"sha256:0000000000000000000000000000000000000000000000000000000000000000",
		*src_paths,
	]
	res = subprocess.run(
		cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert res.returncode == 0, (
		f"library '{package_id}' --emit-package failed:\n{res.stderr[:1500]}"
	)
	# Sign the library package and place it next to stdlib in a unified
	# pkg_root.
	priv = Ed25519PrivateKey.generate()
	pub = priv.public_key().public_bytes(
		encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
	)
	kid = compute_ed25519_kid(pub)
	pub_b64 = base64.b64encode(pub).decode("ascii")
	pkg_bytes = out_dmp.read_bytes()
	pkg_root = tmp_path / "pkg_root"
	pkg_root.mkdir(exist_ok=True)
	# Symlink stdlib into the unified root so the consumer can resolve both.
	std_dest = pkg_root / "std"
	if not std_dest.exists():
		std_dest.symlink_to((stdlib_pkg.pkg_root / "std").resolve())
	# Place library at <pkg_root>/<package_id>/<version>/.
	lib_dest = pkg_root / package_id / package_version
	lib_dest.mkdir(parents=True, exist_ok=True)
	import shutil
	shutil.copy2(str(out_dmp), str(lib_dest / f"{package_id}.dmp"))
	(lib_dest / f"{package_id}.sig").write_text(json.dumps({
		"format": "dmir-pkg-sig", "version": 0,
		"package_sha256": f"sha256:{sha256(pkg_bytes).hexdigest()}",
		"signatures": [{"algo": "ed25519", "kid": kid,
			"sig": base64.b64encode(priv.sign(pkg_bytes)).decode("ascii"),
			"pubkey": pub_b64}],
	}, separators=(",", ":"), sort_keys=True))
	# Build a unified trust store that covers stdlib namespaces + the new
	# library's namespace.
	stdlib_trust = json.loads(stdlib_pkg.trust_path.read_text())
	ns_glob = f"{module_namespace}.*" if module_namespace else f"{package_id}.*"
	merged_trust_path = tmp_path / "merged_trust.json"
	merged = {
		"format": "drift-trust", "version": 0,
		"keys": dict(stdlib_trust.get("keys", {})),
		"namespaces": dict(stdlib_trust.get("namespaces", {})),
		"revoked": list(stdlib_trust.get("revoked", [])),
	}
	merged["keys"][kid] = {"algo": "ed25519", "pubkey": pub_b64}
	merged["namespaces"].setdefault(ns_glob, []).append(kid)
	merged_trust_path.write_text(json.dumps(merged))
	return pkg_root, merged_trust_path


# ---------------------------------------------------------------------------
# 1. pkg_vis_source_private_method_rejected
#    K25-guard: calling private/non-exported method from a package module
#    must be rejected at the consumer compile boundary.
# ---------------------------------------------------------------------------


def test_pkg_vis_source_private_method_rejected(stdlib_package, tmp_path: Path) -> None:
	"""Calling @test_build_only __test_validate from nothrow context must be rejected.

	K25-guard: the method is pub but returns Bool (potentially throwing).
	Calling it from a nothrow entrypoint must produce a diagnostic that
	references the offending method call.  This validates that the
	package-consumer type checker enforces nothrow discipline across the
	package boundary.
	"""
	source = """\
module m;

import std.containers as c;

fn main() nothrow -> Int {
\tvar tm: c.TreeMap<Int, Int> = c.tree_map();
\ttm.__test_validate();
\treturn 0;
}
"""
	res = _compile_consumer(
		source,
		stdlib_pkg=stdlib_package,
		tmp_path=tmp_path,
		entry="m::main",
		expect_failure=True,
	)
	assert res.returncode != 0, (
		"compile should have failed: calling throwing __test_validate "
		"from nothrow context must be rejected across package boundary"
	)
	assert "nothrow" in res.stderr or "__test_validate" in res.stderr, (
		f"diagnostic should reference nothrow violation, got:\n{res.stderr[:500]}"
	)


# ---------------------------------------------------------------------------
# 2. pkg_wrap_method_fnresult_boundary
#    FnResult canonicalization: nothrow method wrapper with generic return
#    type across package boundary must not trigger FnResult ok-type divergence.
# ---------------------------------------------------------------------------


def test_pkg_wrap_method_fnresult_boundary(stdlib_package, tmp_path: Path) -> None:
	"""FnResult wrapper return types must stay consistent across package boundary."""
	source = """\
module m;

import std.containers as c;
import std.iter as iter;

use trait iter.Iterable;
use trait iter.SinglePassIterator;

fn main() nothrow -> Int {
\tvar map: c.HashMap<String, Int> = {"a": 1, "b": 2};

\tmatch map.remove("a") {
\t\tSome(v) => {
\t\t\tif v != 1 { return 1; }
\t\t},
\t\tdefault => { return 2; }
\t}

\tvar arr = [10, 20, 30];
\tvar it = arr.iter();
\tvar count = 0;
\twhile true {
\t\tmatch it.next() {
\t\t\tSome(_) => { count = count + 1; },
\t\t\tdefault => { break; }
\t\t}
\t}
\tif count != 3 { return 3; }

\tif map.len() != 1 { return 4; }

\treturn 0;
}
"""
	binary = _compile_consumer(
		source, stdlib_pkg=stdlib_package, tmp_path=tmp_path, entry="m::main",
	)
	res = _run_binary(binary)
	assert res.returncode == 0, (
		f"binary exited with code {res.returncode}\n"
		f"stdout: {res.stdout[:200]}\nstderr: {res.stderr[:200]}"
	)


# ---------------------------------------------------------------------------
# 3. pkg_env_get_has_boundary
#    std.env.get/has must work through signed package boundary.
# ---------------------------------------------------------------------------


def test_pkg_env_get_has_boundary(stdlib_package, tmp_path: Path) -> None:
	"""std.env get/has runtime helpers must function across package boundary."""
	source = """\
module m;

import std.env as env;

fn main() nothrow -> Int {
\tmatch env.get("HOME") {
\t\tOptional::Some(v) => {
\t\t\tif v.byte_length() == 0 {
\t\t\t\treturn 1;
\t\t\t}
\t\t},
\t\tOptional::None() => {
\t\t\treturn 2;
\t\t}
\t}
\tmatch env.get("DRIFT_PKG_TEST_UNSET_XYZ_99") {
\t\tOptional::Some(_v) => {
\t\t\treturn 3;
\t\t},
\t\tOptional::None() => { }
\t}
\tif !env.has("HOME") {
\t\treturn 4;
\t}
\tif env.has("DRIFT_PKG_TEST_UNSET_XYZ_99") {
\t\treturn 5;
\t}
\treturn 0;
}
"""
	binary = _compile_consumer(
		source, stdlib_pkg=stdlib_package, tmp_path=tmp_path, entry="m::main",
	)
	res = _run_binary(binary)
	assert res.returncode == 0, (
		f"binary exited with code {res.returncode}\n"
		f"stdout: {res.stdout[:200]}\nstderr: {res.stderr[:200]}"
	)


# ---------------------------------------------------------------------------
# 4. pkg_ext_module_trait_scope
#    K25: external module trait scope + visibility must be populated for
#    generic template re-instantiation (e.g. iter/next in std.log._attrs_json).
# ---------------------------------------------------------------------------


def test_pkg_ext_module_trait_scope(stdlib_package, tmp_path: Path) -> None:
	"""log.create_logger must work across package boundary (K25 trait scope)."""
	source = """\
module m;

import std.log as log;

fn main() nothrow -> Int {
\tvar cfg_builder = log.config_builder();
\tcfg_builder.min_level(log.Level::Debug());
\tcfg_builder.sink(log.stderr_sink());
\tval cfg = cfg_builder.build();
\tval logger = log.create_logger("test", cfg);
\tlogger.info("ev", {"k": 1});
\treturn 0;
}
"""
	binary = _compile_consumer(
		source, stdlib_pkg=stdlib_package, tmp_path=tmp_path, entry="m::main",
	)
	res = _run_binary(binary)
	assert res.returncode == 0, (
		f"binary exited with code {res.returncode}\n"
		f"stdout: {res.stdout[:200]}\nstderr: {res.stderr[:200]}"
	)
	# Verify stderr contains the expected JSON log line.
	assert res.stderr.strip(), "expected JSON log output on stderr"
	log_obj = json.loads(res.stderr.strip().splitlines()[-1])
	assert log_obj["level"] == "info"
	assert log_obj["ev"] == "ev"
	assert log_obj["logger"] == "test"
	assert log_obj["attrs"] == {"k": 1}


# ---------------------------------------------------------------------------
# 5. pkg_iface_impl_vtable
#    K26: interface impl vtable must be populated for external package trait
#    impls (Sink for StdErrSink).
# ---------------------------------------------------------------------------


def test_pkg_iface_impl_vtable(stdlib_package, tmp_path: Path) -> None:
	"""K26 vtable for external trait impls must work across package boundary."""
	source = """\
module m;

import std.log as log;

fn main() nothrow -> Int {
\tvar cfg_builder = log.config_builder();
\tcfg_builder.min_level(log.Level::Debug());
\tcfg_builder.sink(log.stderr_sink());
\tval cfg = cfg_builder.build();
\tval logger = log.create_logger("test", cfg);
\treturn 0;
}
"""
	binary = _compile_consumer(
		source, stdlib_pkg=stdlib_package, tmp_path=tmp_path, entry="m::main",
	)
	res = _run_binary(binary)
	assert res.returncode == 0, (
		f"binary exited with code {res.returncode}\n"
		f"stdout: {res.stdout[:200]}\nstderr: {res.stderr[:200]}"
	)


# ---------------------------------------------------------------------------
# 6. pkg_share_capture_arc
#    Package-mode coverage for `captures(share x)` on `conc.Arc<T>`. The
#    `implement<T> shareable.Share for Arc<T>` impl in std.concurrent
#    must survive the .dmp boundary so that the trait prover proves
#    Arc<App>: Share on the consumer side and the type checker does NOT
#    fire E-CAPTURE-SHARE-NOT-SHARE. Mirrors the source-mode e2e
#    `closures_share_capture_arc_generic` (immediate-lambda +
#    callback-boxed shapes).
# ---------------------------------------------------------------------------


def test_pkg_share_capture_arc(stdlib_package, tmp_path: Path) -> None:
	"""`captures(share arc)` on `conc.Arc<T>` must compile across package boundary.

	Mirrors `lang/tests/codegen/e2e/closures_share_capture_arc_generic`
	but exercises the consumer path (signed std.dmp) instead of
	--stdlib-root, ensuring the Share-for-Arc impl in std.concurrent is
	threaded through impl_headers into the consumer's trait world.
	"""
	source = """\
module m;

import std.core as core;
import std.concurrent as conc;

struct App { v: Int }

implement App {
\tpub fn read(self: &App) nothrow -> Int {
\t\treturn self.v;
\t}
}

fn main() nothrow -> Int {
\tval app = conc.arc(App(v = 42));

\tval direct = (| | captures(share app) => {
\t\tval a = app.get();
\t\treturn a.read();
\t})();

\tval cb: core.Callback0<Int> = core.callback0(| | captures(share app) => {
\t\tval a = app.get();
\t\treturn a.read();
\t});

\tval outer = app.get();
\treturn (direct - 42) + (cb.call() - outer.read());
}
"""
	binary = _compile_consumer(
		source, stdlib_pkg=stdlib_package, tmp_path=tmp_path, entry="m::main",
	)
	res = _run_binary(binary)
	assert res.returncode == 0, (
		f"binary exited with code {res.returncode}\n"
		f"stdout: {res.stdout[:200]}\nstderr: {res.stderr[:200]}"
	)


# ---------------------------------------------------------------------------
# 7. pkg_share_capture_arc_emit_package
#    Regression: `captures(share x)` on `conc.Arc<T>` must compile when the
#    consumer is built with `--emit-package` AND consumes stdlib as a signed
#    .dmp.  This is the load-bearing certified-toolchain producer flow: the
#    certified driftc auto-injects `--package-root <toolchain>/lib/stdlib
#    --dep std@VERSION` and users invoke `--emit-package` to publish a
#    package.
#
#    Bug pinned (2026-04-28): in `--emit-package` mode the impl-merge in
#    `compile_stubbed_funcs` re-encoded `Arc<T>`'s free type-param `T` as
#    a nominal `(std, std.concurrent, "T")` instead of the canonical
#    TypeVar `(None, None, "T")` produced by Pass 1 main's encoding.  Both
#    encodings were appended to `world.impls_by_trait_target[(Share,
#    Arc-head)]` because the dup check compared `existing.target ==
#    target_key` and they differed.  The solver's `_bind_impl_type_params`
#    matches free type-params by NAME, so for `Arc<repro::State>` BOTH
#    impls bound `T → State`, status became `AMBIGUOUS: multiple applicable
#    impls`, `is_share` returned False, and E-CAPTURE-SHARE-NOT-SHARE
#    fired even though the user wrote correct code.  Fix: derive
#    `target_key`/`head_key` from `type_key_from_typeid(shared_type_table,
#    impl.target_type_id)` so both merge sites produce the same key.
# ---------------------------------------------------------------------------


def test_pkg_share_capture_arc_emit_package(stdlib_package, tmp_path: Path) -> None:
	"""`captures(share x)` on Arc<T> must compile under --emit-package + stdlib-as-package.

	Web team carrier (2026-04-28): web-rest's dispatch onion uses a chain of
	Callback2 closures over `Arc<State>` where the loop-built layer
	share-captures `state_arc`.  Source-build mode accepted; `--emit-package`
	rejected with E-CAPTURE-SHARE-NOT-SHARE because the Share-for-Arc impl
	got registered twice with different type-param encodings.
	"""
	source = """\
module repro;

import std.core as core;
import std.concurrent as conc;

struct Resp { pub status: Int }
struct AppErr { pub code: Int }

struct State {
\tpub callbacks: Array<core.Callback2<Int, Int, core.Result<Resp, AppErr>>>
}

fn _term(a: Int, b: Int) nothrow -> core.Result<Resp, AppErr> {
\treturn core.Result::Ok(Resp(status = a + b));
}

fn build_chain(state_arc: conc.Arc<State>) nothrow -> core.Callback2<Int, Int, core.Result<Resp, AppErr>> {
\tvar next: core.Callback2<Int, Int, core.Result<Resp, AppErr>> =
\t\tcore.callback2(|a: Int, b: Int| captures(share state_arc) => {
\t\t\tval n = state_arc.get().callbacks.len;
\t\t\treturn _term(a + n, b);
\t\t});
\tvar i = 0;
\twhile i < 1 {
\t\tnext = core.callback2(|a: Int, b: Int| captures(move next, share state_arc) => {
\t\t\treturn next.call(a, b);
\t\t});
\t\ti = i + 1;
\t}
\treturn move next;
}

fn main() nothrow -> Int {
\tvar cbs: Array<core.Callback2<Int, Int, core.Result<Resp, AppErr>>> = [];
\tcbs.push(core.callback2(_term));
\tval state = State(callbacks = move cbs);
\tval state_arc = conc.arc(move state);
\tval chain = build_chain(move state_arc);
\tmatch chain.call(2, 3) {
\t\tcore.Result::Ok(r) => { if r.status != 6 { return 1; } return 0; },
\t\tcore.Result::Err(_) => { return 2; }
\t}
}
"""
	res = _emit_package_consumer(
		source, stdlib_pkg=stdlib_package, tmp_path=tmp_path, package_id="repro",
	)
	assert res.returncode == 0, (
		f"--emit-package compile must succeed; got returncode={res.returncode}\n"
		f"stderr: {res.stderr[:1500]}"
	)
	assert "E-CAPTURE-SHARE-NOT-SHARE" not in res.stderr, (
		f"E-CAPTURE-SHARE-NOT-SHARE must not fire on Arc<T>; stderr:\n{res.stderr[:1500]}"
	)
	out_dmp = tmp_path / "repro.dmp"
	assert out_dmp.exists() and out_dmp.stat().st_size > 0, "package not emitted"


# ---------------------------------------------------------------------------
# 8. pkg_share_capture_inherent_share_still_rejected
#    Negative control for fix #7: a struct with an INHERENT `.share()`
#    method but no `implement Share` must still be rejected with
#    E-CAPTURE-SHARE-NOT-SHARE in --emit-package mode.  Pins the
#    diagnostic surface and prevents the fix from accidentally accepting
#    the inherent method as a Share impl.
# ---------------------------------------------------------------------------


def test_pkg_share_capture_inherent_share_still_rejected(stdlib_package, tmp_path: Path) -> None:
	"""Inherent `.share()` without `implement Share` must remain rejected.

	The diagnostic text explicitly says "an inherent `.share()` method does
	NOT satisfy `captures(share x)`".  Verify that contract still holds in
	--emit-package mode after the canonical-encoding fix.
	"""
	source = """\
module repro;

import std.core as core;

struct Box { v: Int }

implement Box {
\t// Inherent share() — looks like the trait method but is NOT a trait impl.
\tpub fn share(self: &Box) nothrow -> Box {
\t\treturn Box(v = self.v);
\t}
}

fn run(_cb: core.Callback0<Int>) nothrow -> Int { return 0; }

fn main() nothrow -> Int {
\tval b = Box(v = 42);
\treturn run(|| captures(share b) => { return b.v; });
}
"""
	res = _emit_package_consumer(
		source, stdlib_pkg=stdlib_package, tmp_path=tmp_path, package_id="repro",
	)
	assert res.returncode != 0, (
		"compile should have failed: Box has inherent .share() but no Share impl\n"
		f"stderr: {res.stderr[:500]}"
	)
	assert "E-CAPTURE-SHARE-NOT-SHARE" in res.stderr, (
		"diagnostic must reference E-CAPTURE-SHARE-NOT-SHARE; "
		f"stderr: {res.stderr[:500]}"
	)


# ---------------------------------------------------------------------------
# 9. pkg_share_capture_consumer_re_typecheck
#    Regression for E_INTERNAL_MISSING_CALLSITE_CALLINFO (web-rest 0.31.26
#    carrier, 2026-04-29).  Root cause is in DMIR/HIR decode, not in
#    trait visibility.
#
#    `_to_jsonable` (lang/driftc/packages/provisional_dmir_v0.py) tags
#    serialized dataclasses with `_type: <class.__name__>` — bare class
#    name, no module qualification.  Both `stage0/ast.py` and
#    `parser/ast.py` define a class called `TypeNameRef` with DIFFERENT
#    field sets:
#      - `stage0.ast.TypeNameRef`: name, module_id, loc
#      - `parser/ast.py:TypeNameRef`: loc, name        (no module_id)
#    HIR builders use `stage0.ast.TypeNameRef` (e.g., `ast_to_hir.py`
#    synthesizes `Share::share(&x)` for `captures(share x)` with
#    `module_id="std.core.shareable"`).  The producer serializes that
#    instance with `module_id` intact.  On the consumer side,
#    `from_jsonable` looks up `"TypeNameRef"` in the registry — which
#    only included `parser_ast` (no `stage0_ast`) — and reconstructs
#    using `parser_ast.TypeNameRef`.  That class lacks `module_id`, so
#    the field is silently dropped.  The reconstructed
#    `TypeNameRef(loc=..., name="Share")` then makes `_qual_from_type_expr`
#    return None, `trait_key_from_expr` falls back to the current module
#    (`web.repro`), and the lookup misses the real `(std,
#    std.core.shareable, Share)` trait.  `resolve_qualified_member_ufcs`
#    returns `MethodCallResult(unknown_ty, None)`, `record_call_info` is
#    never called for the synthesized HCall's csid, and the typed-mode
#    guard at `driftc.py:5273` fires.
#
#    Fix: append `stage0.ast` to the dataclass registry in
#    `decode_hir_funcs` and `decode_generic_templates` so its
#    `TypeNameRef` (with `module_id`) wins the bare-name collision.
#    This is an emergency decode-order fix for HIR/stage0-AST payloads;
#    the long-term direction is module-qualified discriminators in
#    `_to_jsonable` to eliminate the collision class.
#
#    The carrier shape mirrors web-rest's dispatch: a library package
#    with a public function whose body share-captures `Arc<State>` and
#    closes the synthesized `Share::share(&state_arc)` calls through
#    `core.callback2(...)`.  The consumer compile imports the library
#    and CALLS that function — that's what forces the cross-process
#    HIR roundtrip and exposes the dropped `module_id`.  Single-module
#    `--emit-package` programs don't reproduce because the in-process
#    snapshot path doesn't go through the same dataclass-name registry
#    lookup that strips the field.
# ---------------------------------------------------------------------------


def test_pkg_share_capture_consumer_re_typecheck(stdlib_package, tmp_path: Path) -> None:
	"""Share-capture in a library fn must re-typecheck cleanly in consumer."""
	library_lib_drift = """\
module web.repro;

import std.core as core;
import std.concurrent as conc;

export { Resp, AppErr, State, build_chain, empty_state };

pub struct Resp { pub status: Int }
pub struct AppErr { pub code: Int }

pub struct State {
\tpub callbacks: Array<core.Callback2<Int, Int, core.Result<Resp, AppErr>>>
}

fn _term(a: Int, b: Int) nothrow -> core.Result<Resp, AppErr> {
\treturn core.Result::Ok(Resp(status = a + b));
}

pub fn build_chain(state_arc: conc.Arc<State>) nothrow -> core.Callback2<Int, Int, core.Result<Resp, AppErr>> {
\tvar next: core.Callback2<Int, Int, core.Result<Resp, AppErr>> =
\t\tcore.callback2(|a: Int, b: Int| captures(share state_arc) => {
\t\t\tval n = state_arc.get().callbacks.len;
\t\t\treturn _term(a + n, b);
\t\t});
\tvar i = 0;
\twhile i < 1 {
\t\tnext = core.callback2(|a: Int, b: Int| captures(move next, share state_arc) => {
\t\t\treturn next.call(a, b);
\t\t});
\t\ti = i + 1;
\t}
\treturn move next;
}

pub fn empty_state() nothrow -> State {
\tvar cbs: Array<core.Callback2<Int, Int, core.Result<Resp, AppErr>>> = [];
\tcbs.push(core.callback2(_term));
\treturn State(callbacks = move cbs);
}
"""
	pkg_root, trust_path = _publish_library_package(
		{"lib.drift": library_lib_drift},
		stdlib_pkg=stdlib_package,
		tmp_path=tmp_path,
		package_id="web-repro",
		package_version="0.0.1",
		module_namespace="web",
	)

	consumer_source = """\
module consumer;

import std.core as core;
import std.concurrent as conc;
import web.repro as repro;

fn main() nothrow -> Int {
\tval state = repro.empty_state();
\tval state_arc = conc.arc(move state);
\tval chain = repro.build_chain(move state_arc);
\tmatch chain.call(2, 3) {
\t\tcore.Result::Ok(r) => { if r.status != 6 { return 1; } return 0; },
\t\tcore.Result::Err(_) => { return 2; }
\t}
}
"""
	src_dir = tmp_path / "consumer_src"
	src_dir.mkdir(parents=True, exist_ok=True)
	(src_dir / "main.drift").write_text(consumer_source)
	out_bin = tmp_path / "consumer_bin"
	empty_stdlib = tmp_path / "_empty_stdlib_consumer"
	empty_stdlib.mkdir(exist_ok=True)
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		str(src_dir / "main.drift"),
		"--stdlib-root", str(empty_stdlib),
		"--target-word-bits", "64",
		"--package-root", str(pkg_root),
		"--dep", f"std@{stdlib_package.version}",
		"--dep", "web-repro@0.0.1",
		"--trust-store", str(trust_path),
		"--dev", "--dev-core-trust-store", str(trust_path),
		"--entry", "consumer::main",
		"-o", str(out_bin),
	]
	res = subprocess.run(
		cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert "E_INTERNAL_MISSING_CALLSITE_CALLINFO" not in res.stderr, (
		"consumer compile of share-capture library must not fire "
		"E_INTERNAL_MISSING_CALLSITE_CALLINFO; stderr:\n"
		+ res.stderr[:2000]
	)
	assert res.returncode == 0, (
		f"consumer compile must succeed; got returncode={res.returncode}\n"
		f"stderr: {res.stderr[:2000]}"
	)


# ---------------------------------------------------------------------------
# Cross-package callback-wrap + OK-wrap thunk preservation
# (0.31.72 fix for the bookkeeper/web-rest report).
#
# 0.31.70 forced `can_throw = True` for every `is_exported_entrypoint`
# fn ref in `_call_sig_for_fn_ref` so the fn-reference TypeId
# reflected the OK-wrap thunk's `FnResult`-based cross-package ABI.
# That kept direct fn-ref call sites (`val fp = pkg.f; try fp(1)
# catch {...}`) working but broke `core.callback{N}(pkg.fn)` over a
# declared-nothrow exported function — the callback intrinsic's
# `fn_throws` check rejected the wrap as "requires a nothrow
# function", cascading into Unknown-typed downstream calls.
#
# 0.31.72 keeps the override (direct-call semantics preserved) and
# fixes the callback path at the resolver: when the side-table entry
# in `fnptr_consts_by_node_id` has `kind=THUNK_OK_WRAP`, the
# `callback{N}` resolver looks up the underlying function via
# `ctx.find_thunk_spec_by_id` and rewrites the entry in place to
# point at the impl with the declared (unwrapped) signature.
# `_apply_fnptr_consts` then materializes an `HFnPtrConst` against
# the original function, so HIR→MIR's `ConstructIface` targets the
# impl rather than `__thunk_ok_wrap::<callee>` (which codegen has
# no rule for).
#
# The same-workspace cross-module shape is pinned by
# `test_cross_module_callback_named_fn.py`; this test pins the
# stricter cross-PACKAGE boundary (signed .dmp consumed via
# --package-root + --dep), where `is_exported_entrypoint` fires
# unconditionally on every published symbol.
# ---------------------------------------------------------------------------


def test_pkg_callback_wrap_named_fn_ref_compiles_and_runs(stdlib_package, tmp_path: Path) -> None:
	"""Cross-package `core.callback{N}(pkg.fn)` over a declared-nothrow
	exported function.  Pre-0.31.72 the consumer compile failed with
	"callback3 requires a nothrow function" because the published-fn's
	`is_exported_entrypoint=True` flipped its fn-reference TypeId-side
	`can_throw` to True (a deliberate choice to reflect the OK-wrap
	thunk's FnResult ABI for direct call sites), and the callback
	intrinsic's `fn_throws` check rejected the wrap.  0.31.72 keeps
	the type-side override and rewrites the wrapped fn_ref to the
	underlying impl at the callback resolver instead, so direct-call
	and callback-wrap paths each see the shape they need.
	"""
	library_lib_drift = """\
module web.cb_wrap_repro;

import std.core as core;

export { passthrough1, passthrough2, passthrough3 };

pub fn passthrough1(a: Int) nothrow -> Int {
\treturn a + 1;
}

pub fn passthrough2(a: Int, b: Int) nothrow -> Int {
\treturn a + b;
}

pub fn passthrough3(a: Int, b: Int, c: Int) nothrow -> Int {
\treturn a + b + c;
}
"""
	pkg_root, trust_path = _publish_library_package(
		{"lib.drift": library_lib_drift},
		stdlib_pkg=stdlib_package,
		tmp_path=tmp_path,
		package_id="web-cb-wrap-repro",
		package_version="0.0.1",
		module_namespace="web",
	)

	consumer_source = """\
module consumer;

import std.core as core;
import web.cb_wrap_repro as cb;

fn main() nothrow -> Int {
\tval cb1 = core.callback1(cb.passthrough1);
\tval r1 = cb1.call(41);
\tif r1 != 42 { return 1; }
\tval cb2 = core.callback2(cb.passthrough2);
\tval r2 = cb2.call(20, 22);
\tif r2 != 42 { return 2; }
\tval cb3 = core.callback3(cb.passthrough3);
\tval r3 = cb3.call(10, 12, 20);
\tif r3 != 42 { return 3; }
\treturn 0;
}
"""
	src_dir = tmp_path / "consumer_src"
	src_dir.mkdir(parents=True, exist_ok=True)
	(src_dir / "main.drift").write_text(consumer_source)
	out_bin = tmp_path / "consumer_bin"
	empty_stdlib = tmp_path / "_empty_stdlib_consumer"
	empty_stdlib.mkdir(exist_ok=True)
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		str(src_dir / "main.drift"),
		"--stdlib-root", str(empty_stdlib),
		"--target-word-bits", "64",
		"--package-root", str(pkg_root),
		"--dep", f"std@{stdlib_package.version}",
		"--dep", "web-cb-wrap-repro@0.0.1",
		"--trust-store", str(trust_path),
		"--dev", "--dev-core-trust-store", str(trust_path),
		"--entry", "consumer::main",
		"-o", str(out_bin),
	]
	res = subprocess.run(
		cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert "callback1 requires a nothrow function" not in res.stderr, (
		"consumer compile must not fire the pre-0.31.72 false-positive; "
		f"stderr:\n{res.stderr[:1500]}"
	)
	assert "callback2 requires a nothrow function" not in res.stderr, res.stderr[:1500]
	assert "callback3 requires a nothrow function" not in res.stderr, res.stderr[:1500]
	assert res.returncode == 0, f"compile failed; stderr:\n{res.stderr[:2000]}"
	assert out_bin.exists()
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(20))
	assert run.returncode == 0, (
		f"binary exit={run.returncode} (expected 0). 1=cb1 mismatch, "
		f"2=cb2 mismatch, 3=cb3 mismatch.  stdout: {run.stdout[:200]}; "
		f"stderr: {run.stderr[:200]}"
	)


def test_pkg_direct_call_ok_wrap_thunk_preserved(stdlib_package, tmp_path: Path) -> None:
	"""OK-wrap thunk preservation: a direct cross-package call to a
	declared-nothrow exported function must still go through the
	FnResult ABI wrapper (`_ensure_ok_wrap_thunk`) so the consumer
	receives the unwrapped bare return value.

	The 0.31.72 patch keeps the `can_throw = True` override on the
	fn-reference TypeId path AND keeps the thunk-installation site
	at type_checker.py:3395-3400 unchanged — the
	`_ensure_ok_wrap_thunk` invocation still reads
	`sig.declared_can_throw` directly.  This test pins that
	direct cross-package calls of `pub fn foo() nothrow -> Int`
	still produce a usable `Int` (not a leaked `FnResult` shape).
	"""
	library_lib_drift = """\
module web.thunk_repro;

export { add_one, mul_two };

pub fn add_one(a: Int) nothrow -> Int {
\treturn a + 1;
}

pub fn mul_two(a: Int) nothrow -> Int {
\treturn a * 2;
}
"""
	pkg_root, trust_path = _publish_library_package(
		{"lib.drift": library_lib_drift},
		stdlib_pkg=stdlib_package,
		tmp_path=tmp_path,
		package_id="web-thunk-repro",
		package_version="0.0.1",
		module_namespace="web",
	)

	consumer_source = """\
module consumer;

import web.thunk_repro as t;

fn main() nothrow -> Int {
\tval a = t.add_one(40);
\tif a != 41 { return 1; }
\tval b = t.mul_two(a);
\tif b != 82 { return 2; }
\treturn 0;
}
"""
	src_dir = tmp_path / "consumer_src"
	src_dir.mkdir(parents=True, exist_ok=True)
	(src_dir / "main.drift").write_text(consumer_source)
	out_bin = tmp_path / "consumer_bin"
	empty_stdlib = tmp_path / "_empty_stdlib_consumer"
	empty_stdlib.mkdir(exist_ok=True)
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		str(src_dir / "main.drift"),
		"--stdlib-root", str(empty_stdlib),
		"--target-word-bits", "64",
		"--package-root", str(pkg_root),
		"--dep", f"std@{stdlib_package.version}",
		"--dep", "web-thunk-repro@0.0.1",
		"--trust-store", str(trust_path),
		"--dev", "--dev-core-trust-store", str(trust_path),
		"--entry", "consumer::main",
		"-o", str(out_bin),
	]
	res = subprocess.run(
		cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert res.returncode == 0, f"compile failed; stderr:\n{res.stderr[:2000]}"
	assert out_bin.exists()
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(20))
	assert run.returncode == 0, (
		f"binary exit={run.returncode} (expected 0). 1=add_one mismatch, "
		f"2=mul_two mismatch.  stdout: {run.stdout[:200]}; "
		f"stderr: {run.stderr[:200]}"
	)
