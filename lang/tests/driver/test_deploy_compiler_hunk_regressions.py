# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression tests for compiler hunks required by the deploy pipeline.

K9: Call-graph BFS pruning preserves reachable destructor/drop paths.
K4: Template fingerprint mismatch emits observable diagnostic (not silent).
K4: Stdlib template fingerprint stability (self-deploy roundtrip).
K7: Malformed method receiver metadata is still caught by the checker.
K11: Package-consumed variant tombstone metadata preservation.
K12: Package-consumed generic variant constructor inference.
K13: Boundary-call nothrow analysis must not over-approximate.
K14: --entry module::fn must be plumbed to deployed/package compile path.
"""
from __future__ import annotations

import glob as _glob_mod
import json
import sys
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


# ── Helpers ──────────────────────────────────────────────────────────


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _emit_pkg_args(package_id: str) -> list[str]:
	return [
		"--package-id", package_id,
		"--package-version", "0.0.0",
		"--package-target", "test-target",
	]


def _empty_stdlib_root(tmp_path: Path) -> Path:
	d = tmp_path / "_empty_stdlib"
	d.mkdir(parents=True, exist_ok=True)
	return d


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


# ── K9: Call-graph BFS preserves reachable drop paths ────────────────


def _emit_chain_pkg(tmp_path: Path) -> Path:
	"""Emit a package with a function call chain (A calls B)."""
	build = tmp_path / "pkg_build"
	mod_dir = build / "acme" / "chain"
	_write_file(
		mod_dir / "chain.drift",
		"""\
module acme.chain;

export { entry };

fn helper(x: Int) nothrow -> Int {
	return x + 1;
}

pub fn entry(x: Int) nothrow -> Int {
	return helper(x);
}
""",
	)
	pkg_path = tmp_path / "pkgs" / "acme.chain.dmp"
	pkg_path.parent.mkdir(parents=True, exist_ok=True)
	rc = driftc_main([
		"--dev",
		"-M", str(build),
		"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
		str(mod_dir / "chain.drift"),
		*_emit_pkg_args("acme.chain"),
		"--emit-package", str(pkg_path),
	])
	assert rc == 0, "failed to build chain package"
	return pkg_path


def test_k9_reachable_drop_path_preserved(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""K9 regression: BFS pruning must preserve transitively-reachable package
	functions.

	User code calls a package function that internally calls another package
	function.  If BFS over-prunes, the internal helper is missing and codegen
	or linking fails.
	"""
	pkg_path = _emit_chain_pkg(tmp_path)
	pkg_root = pkg_path.parent

	consumer = tmp_path / "consumer"
	main_src = consumer / "main.drift"
	_write_file(
		main_src,
		"""\
module main;

import acme.chain as chain;

pub fn main() nothrow -> Int {
	return chain.entry(41);
}
""",
	)

	rc, payload = _run_driftc_json(
		[
			"-M", str(consumer),
			"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
			"--package-root", str(pkg_root),
			"--dep", "acme.chain@0.0.0",
			"--allow-unsigned-from", str(pkg_root),
			"--dev",
			str(main_src),
			"--emit-ir", str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc == 0, f"expected success (call chain reachable); diagnostics: {payload.get('diagnostics', [])}"
	assert (tmp_path / "out.ll").exists()


# ── K4: Template fingerprint mismatch is surfaced ────────────────────


def _emit_generic_pkg(tmp_path: Path) -> Path:
	"""Emit a package containing a generic function."""
	build = tmp_path / "pkg_build"
	mod_dir = build / "acme" / "genlib"
	_write_file(
		mod_dir / "genlib.drift",
		"""\
module acme.genlib;

export { identity };

pub fn identity<T>(x: T) nothrow -> T {
	return x;
}
""",
	)
	pkg_path = tmp_path / "pkgs" / "acme.genlib.dmp"
	pkg_path.parent.mkdir(parents=True, exist_ok=True)
	rc = driftc_main([
		"--dev",
		"-M", str(build),
		"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
		str(mod_dir / "genlib.drift"),
		*_emit_pkg_args("acme.genlib"),
		"--emit-package", str(pkg_path),
	])
	assert rc == 0, "failed to build generic package"
	return pkg_path


def test_k4_fingerprint_mismatch_emits_note(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""K4 regression: when a template's decl fingerprint doesn't match, a note
	must be emitted to stderr so the mismatch is observable.

	We build a package with a generic template, then monkeypatch the fingerprint
	computation to return a different value during consumption, forcing a mismatch.
	"""
	pkg_path = _emit_generic_pkg(tmp_path)
	pkg_root = pkg_path.parent

	consumer = tmp_path / "consumer"
	main_src = consumer / "main.drift"
	_write_file(
		main_src,
		"""\
module main;

import acme.genlib as genlib;

pub fn main() nothrow -> Int {
	return genlib.identity<Int>(42);
}
""",
	)

	# Monkeypatch compute_template_decl_fingerprint in driftc module to return
	# a wrong fingerprint.  This must be done AFTER package emission (above)
	# so the package itself has correct fingerprints, but the consumer's
	# recomputation produces a mismatch.
	import lang.driftc.driftc as _driftc_mod
	_orig = _driftc_mod.compute_template_decl_fingerprint

	def _broken_fingerprint(*args, **kwargs):
		fp, layout = _orig(*args, **kwargs)
		return ("WRONG_" + fp, layout)

	monkeypatch.setattr(_driftc_mod, "compute_template_decl_fingerprint", _broken_fingerprint)

	# Capture stderr where the note is emitted.
	rc = driftc_main([
		"-M", str(consumer),
		"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
		"--package-root", str(pkg_root),
		"--dep", "acme.genlib@0.0.0",
		"--allow-unsigned-from", str(pkg_root),
		"--dev",
		str(main_src),
		"--emit-ir", str(tmp_path / "out.ll"),
		"--json",
	])
	captured = capsys.readouterr()
	# The note should appear on stderr regardless of compilation success/failure.
	assert "fingerprint mismatch" in captured.err, (
		f"expected 'fingerprint mismatch' note on stderr; got:\n{captured.err}"
	)


# ── K7: Malformed method receiver metadata still fails ───────────────


def test_k7_local_bad_receiver_caught(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""K7 regression: method receiver validation is active for locally-compiled code.

	A method declaring `self: Int` instead of `self: &Foo` must be flagged.
	This proves the receiver validation code path is not disabled by the
	package-origin bypass (which only applies to loc=None + module_packages).
	"""
	src = tmp_path / "main.drift"
	_write_file(
		src,
		"""\
module main;

struct Foo { x: Int }

implement Foo {
	pub fn bad(self: Int) nothrow -> Int {
		return 0;
	}
}

pub fn main() nothrow -> Int {
	return 0;
}
""",
	)

	rc, payload = _run_driftc_json(
		[
			"-M", str(tmp_path),
			"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
			"--dev",
			str(src),
			"--emit-ir", str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0, "expected receiver validation error"
	diags = payload.get("diagnostics", [])
	messages = " ".join(d.get("message", "") for d in diags)
	assert "receiver type" in messages.lower() or "must be" in messages.lower(), (
		f"expected receiver type error; got: {messages}"
	)


def test_k7_receiver_bypass_condition_is_narrowed() -> None:
	"""K7 regression: the receiver validation bypass requires BOTH conditions:
	loc is None AND module is in module_packages.

	This is a code-level assertion that the narrowed bypass condition is
	correctly expressed in the checker source.  If someone broadens the
	bypass (e.g., removes the module_packages check), this test fails.
	"""
	import inspect
	from lang.driftc import checker

	# Find the check_by_id method source and verify the bypass condition.
	source = inspect.getsource(checker.Checker.check_by_id)

	# The bypass must check BOTH conditions: loc is None AND module_packages.
	# A broad bypass (just loc is None) would be detected by the absence of
	# module_packages in the condition.
	assert "module_packages" in source, (
		"receiver bypass must check module_packages, not just loc"
	)
	assert "_skip_pkg_receiver" in source, (
		"receiver bypass must use the _skip_pkg_receiver flag"
	)


# ── K7: Behavioral negative test — malformed external receiver ───────


def test_k7_external_bad_receiver_not_bypassed() -> None:
	"""K7 regression (behavioral): a signature with loc=None whose module is
	NOT in module_packages must still fail receiver validation.

	This proves the narrowed bypass actually works at runtime — not just in
	source text.  We construct a Checker directly with a crafted FnSignature
	that has loc=None, is_method=True, wrong receiver type, and a module that
	is not registered in module_packages.  The checker must emit a receiver
	type error.
	"""
	from lang.driftc.checker import Checker, FnSignature
	from lang.driftc.core.function_id import FunctionId
	from lang.driftc.core.types_core import TypeTable
	from lang.driftc.stage1.hir_nodes import HBlock

	tt = TypeTable()
	int_type = tt.ensure_int()
	void_type = tt.ensure_void()
	foo_type = tt.declare_struct("pkg.external", "Foo", ["x"])
	tt.define_struct_fields(foo_type, [int_type])

	# Module NOT in module_packages — bypass must NOT activate.
	fn_id = FunctionId(module="pkg.external", name="bad", ordinal=0)

	sig = FnSignature(
		name="bad",
		loc=None,                    # no source location (package-origin)
		is_method=True,
		self_mode="value",
		param_type_ids=[int_type],   # WRONG — should be foo_type
		param_names=["self"],
		return_type_id=void_type,
		impl_target_type_id=foo_type,
		declared_can_throw=False,
	)

	empty_block = HBlock(statements=[])

	checker = Checker(
		signatures_by_id={fn_id: sig},
		hir_blocks_by_id={fn_id: empty_block},
		type_table=tt,
		call_info_by_callsite_id={fn_id: {}},
	)
	result = checker.check_by_id([fn_id])

	# Must have a receiver type error diagnostic.
	messages = " ".join(d.message for d in result.diagnostics)
	assert "receiver type" in messages.lower(), (
		f"expected receiver type error for non-bypassed external sig; got: {messages}"
	)


def test_k7_external_bad_receiver_bypassed_when_in_module_packages() -> None:
	"""K7 regression (behavioral, positive): a signature with loc=None whose
	module IS in module_packages must be bypassed — no receiver error.

	This confirms the bypass activates correctly when both conditions are met.
	"""
	from lang.driftc.checker import Checker, FnSignature
	from lang.driftc.core.function_id import FunctionId
	from lang.driftc.core.types_core import TypeTable
	from lang.driftc.stage1.hir_nodes import HBlock

	tt = TypeTable()
	int_type = tt.ensure_int()
	void_type = tt.ensure_void()
	foo_type = tt.declare_struct("pkg.external", "Foo", ["x"])
	tt.define_struct_fields(foo_type, [int_type])

	# Register module in module_packages — bypass SHOULD activate.
	tt.module_packages["pkg.external"] = "acme.pkg"

	fn_id = FunctionId(module="pkg.external", name="bad", ordinal=0)

	sig = FnSignature(
		name="bad",
		loc=None,                    # no source location (package-origin)
		is_method=True,
		self_mode="value",
		param_type_ids=[int_type],   # wrong receiver, but bypass should skip check
		param_names=["self"],
		return_type_id=void_type,
		impl_target_type_id=foo_type,
		declared_can_throw=False,
	)

	empty_block = HBlock(statements=[])

	checker = Checker(
		signatures_by_id={fn_id: sig},
		hir_blocks_by_id={fn_id: empty_block},
		type_table=tt,
		call_info_by_callsite_id={fn_id: {}},
	)
	result = checker.check_by_id([fn_id])

	# Must NOT have a receiver type error — bypass is active.
	receiver_errors = [
		d for d in result.diagnostics
		if "receiver type" in d.message.lower()
	]
	assert not receiver_errors, (
		f"expected NO receiver error when module is in module_packages; got: "
		f"{[d.message for d in receiver_errors]}"
	)


# ── K4: Template import failure classification regressions ───────────


def _corrupt_pkg_payload(
	pkg_path: Path,
	module_id: str,
	mutator,
) -> None:
	"""Load a package, corrupt its payload via `mutator`, and rewrite it.

	`mutator(payload_obj)` receives the parsed payload JSON dict and must
	mutate it in-place.  The package is rewritten with updated hashes.
	"""
	from lang.driftc.packages.dmir_pkg_v0 import (
		canonical_json_bytes, sha256_hex, write_dmir_pkg_v0,
	)
	from lang.driftc.packages.provider_v1 import load_package_v1

	pkg = load_package_v1(pkg_path)
	manifest = dict(pkg.manifest)

	# Find the payload blob sha for the target module.
	mod_entry = None
	for m in manifest["modules"]:
		if m["module_id"] == module_id:
			mod_entry = m
			break
	assert mod_entry is not None, f"module {module_id} not found in package"

	old_payload_sha = mod_entry["payload_blob"].split("sha256:", 1)[1]
	raw_payload = pkg.blobs_by_sha256[old_payload_sha]
	payload_obj = json.loads(raw_payload.decode("utf-8"))

	mutator(payload_obj)

	new_payload_bytes = canonical_json_bytes(payload_obj)
	new_payload_sha = sha256_hex(new_payload_bytes)

	# Rebuild blob maps, replacing old payload with corrupted version.
	blobs: dict[str, bytes] = {}
	blob_types: dict[str, int] = {}
	blob_names: dict[str, str] = {}
	for entry in pkg.toc:
		if entry.sha256 == old_payload_sha:
			blobs[new_payload_sha] = new_payload_bytes
			blob_types[new_payload_sha] = entry.type
			blob_names[new_payload_sha] = entry.name
		else:
			blobs[entry.sha256] = pkg.blobs_by_sha256[entry.sha256]
			blob_types[entry.sha256] = entry.type
			blob_names[entry.sha256] = entry.name

	# Update manifest blob references.
	mod_entry["payload_blob"] = f"sha256:{new_payload_sha}"
	old_blobs = manifest["blobs"]
	old_key = f"sha256:{old_payload_sha}"
	new_key = f"sha256:{new_payload_sha}"
	new_blobs = {}
	for k, v in old_blobs.items():
		if k == old_key:
			new_blobs[new_key] = v
		else:
			new_blobs[k] = v
	manifest["blobs"] = new_blobs

	write_dmir_pkg_v0(
		pkg_path,
		manifest_obj=manifest,
		blobs=blobs,
		blob_types=blob_types,
		blob_names=blob_names,
	)


def _consumer_src(tmp_path: Path) -> Path:
	"""Write a minimal consumer that imports the generic package."""
	consumer = tmp_path / "consumer"
	main_src = consumer / "main.drift"
	_write_file(
		main_src,
		"""\
module main;

import acme.genlib as genlib;

pub fn main() nothrow -> Int {
	return genlib.identity<Int>(42);
}
""",
	)
	return main_src


def _consume_pkg(tmp_path: Path, pkg_path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict, str]:
	"""Compile a consumer against the (possibly corrupted) package.

	Returns (rc, json_payload, stderr).
	"""
	main_src = _consumer_src(tmp_path)
	pkg_root = pkg_path.parent
	rc = driftc_main([
		"-M", str(main_src.parent),
		"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
		"--package-root", str(pkg_root),
		"--dep", "acme.genlib@0.0.0",
		"--allow-unsigned-from", str(pkg_root),
		"--dev",
		str(main_src),
		"--emit-ir", str(tmp_path / "out.ll"),
		"--json",
	])
	captured = capsys.readouterr()
	payload = json.loads(captured.out) if captured.out.strip() else {}
	return rc, payload, captured.err


def test_k4_hard_error_non_dict_template_entry(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""K4 regression: a non-dict entry in generic_templates is structural
	corruption and must produce a hard error."""
	pkg_path = _emit_generic_pkg(tmp_path)

	import lang.driftc.driftc as _driftc_mod
	_orig_decode = _driftc_mod.decode_generic_templates

	def _inject_non_dict(templates_obj):
		result = _orig_decode(templates_obj)
		# Inject a non-dict entry to simulate corruption bypassing the decoder.
		result.insert(0, "CORRUPT")
		return result

	monkeypatch.setattr(_driftc_mod, "decode_generic_templates", _inject_non_dict)

	rc, payload, stderr = _consume_pkg(tmp_path, pkg_path, capsys)
	assert rc == 1, f"expected hard error; got rc={rc}"
	diags = payload.get("diagnostics", [])
	messages = " ".join(d.get("message", "") for d in diags)
	assert "not a dict" in messages, f"expected 'not a dict' diagnostic; got: {messages}"


def test_k4_hard_error_missing_fn_id(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""K4 regression: a TemplateHIR-v1 entry missing fn_id is structural
	corruption and must produce a hard error."""
	pkg_path = _emit_generic_pkg(tmp_path)

	def _remove_fn_id(payload_obj):
		templates = payload_obj.get("generic_templates", [])
		for t in templates:
			if isinstance(t, dict) and t.get("ir_kind") == "TemplateHIR-v1":
				t.pop("fn_id", None)
				break

	_corrupt_pkg_payload(pkg_path, "acme.genlib", _remove_fn_id)

	rc, payload, stderr = _consume_pkg(tmp_path, pkg_path, capsys)
	assert rc == 1, f"expected hard error; got rc={rc}"
	diags = payload.get("diagnostics", [])
	messages = " ".join(d.get("message", "") for d in diags)
	assert "missing fn_id" in messages, f"expected 'missing fn_id' diagnostic; got: {messages}"


def test_k4_hard_error_layout_mismatch(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""K4 regression: generic_param_layout that doesn't match type_params
	counts is structural corruption and must produce a hard error."""
	pkg_path = _emit_generic_pkg(tmp_path)

	def _break_layout(payload_obj):
		templates = payload_obj.get("generic_templates", [])
		for t in templates:
			if isinstance(t, dict) and t.get("ir_kind") == "TemplateHIR-v1":
				t["generic_param_layout"] = []
				break

	_corrupt_pkg_payload(pkg_path, "acme.genlib", _break_layout)

	rc, payload, stderr = _consume_pkg(tmp_path, pkg_path, capsys)
	assert rc == 1, f"expected hard error; got rc={rc}"
	diags = payload.get("diagnostics", [])
	messages = " ".join(d.get("message", "") for d in diags)
	assert "layout mismatch" in messages, f"expected 'layout mismatch' diagnostic; got: {messages}"


def test_k4_soft_skip_unknown_ir_kind(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""K4 regression: unknown ir_kind is a soft skip (forward compat) but
	must emit a note to stderr for observability."""
	pkg_path = _emit_generic_pkg(tmp_path)

	def _set_unknown_ir_kind(payload_obj):
		templates = payload_obj.get("generic_templates", [])
		for t in templates:
			if isinstance(t, dict):
				t["ir_kind"] = "TemplateHIR-v99"

	_corrupt_pkg_payload(pkg_path, "acme.genlib", _set_unknown_ir_kind)

	# Consumer must NOT instantiate the generic — the template is skipped.
	# Use a consumer that imports the package but only calls non-generic code.
	consumer = tmp_path / "consumer"
	main_src = consumer / "main.drift"
	_write_file(
		main_src,
		"""\
module main;

pub fn main() nothrow -> Int {
	return 0;
}
""",
	)
	pkg_root = pkg_path.parent
	rc = driftc_main([
		"-M", str(consumer),
		"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
		"--package-root", str(pkg_root),
		"--dep", "acme.genlib@0.0.0",
		"--allow-unsigned-from", str(pkg_root),
		"--dev",
		str(main_src),
		"--emit-ir", str(tmp_path / "out.ll"),
		"--json",
	])
	captured = capsys.readouterr()
	assert rc == 0, f"expected soft skip (success); got rc={rc}, stderr={captured.err}"
	assert "unknown template ir_kind" in captured.err, (
		f"expected 'unknown template ir_kind' note on stderr; got:\n{captured.err}"
	)


# ── K4: Stdlib template fingerprint stability ────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[3]
_STDLIB_DIR = _REPO_ROOT / "stdlib"


def _build_stdlib_package(tmp_path: Path) -> Path:
	"""Compile the full stdlib into a package (unsigned, --dev)."""
	stdlib_files = sorted(_glob_mod.glob(str(_STDLIB_DIR / "**" / "*.drift"), recursive=True))
	assert len(stdlib_files) > 0, f"no stdlib .drift files found under {_STDLIB_DIR}"
	pkg_path = tmp_path / "pkgs" / "std.dmp"
	pkg_path.parent.mkdir(parents=True, exist_ok=True)
	rc = driftc_main([
		"--dev",
		"-M", str(_STDLIB_DIR),
		"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
		*stdlib_files,
		"--package-id", "std",
		"--package-version", "0.0.0-test",
		"--package-target", "test-target",
		"--emit-package", str(pkg_path),
	])
	assert rc == 0, f"failed to build stdlib package (rc={rc})"
	return pkg_path


def test_k4_stdlib_self_deploy_fingerprint_stability(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""K4 regression: every template in the stdlib package must have a decl
	fingerprint that survives the emit→decode→recompute roundtrip.

	This pins the specific bug where HashSetIter<K, B>::…::next had a
	fingerprint mismatch at consume-time.  The test builds the stdlib
	package, loads it, reconstructs signatures from the decoded payload
	(same path as driftc consume-time), recomputes the fingerprint, and
	asserts equality with the stored value.
	"""
	from lang.driftc.checker import FnSignature, TypeParam
	from lang.driftc.core.types_core import TypeParamId
	from lang.driftc.core.function_id import FunctionId
	from lang.driftc.core.function_key import function_key_from_obj
	from lang.driftc.packages.provisional_dmir_v0 import (
		compute_template_decl_fingerprint,
		decode_generic_templates,
		decode_trait_expr,
		decode_type_expr,
	)
	from lang.driftc.packages.provider_v1 import load_package_v1

	pkg_path = _build_stdlib_package(tmp_path)
	_ = capsys.readouterr()  # drain captured output from build
	pkg = load_package_v1(pkg_path)

	assert "std.containers" in pkg.modules_by_id, (
		f"std.containers not found in package; modules: {list(pkg.modules_by_id.keys())}"
	)
	payload = pkg.modules_by_id["std.containers"].payload
	templates_obj = payload.get("generic_templates")
	assert isinstance(templates_obj, list) and len(templates_obj) > 0, (
		"std.containers has no generic_templates in payload"
	)

	templates = decode_generic_templates(templates_obj)
	mismatches: list[str] = []
	hashset_iter_seen = False

	for entry in templates:
		if not isinstance(entry, dict):
			continue
		template_id = entry.get("template_id")
		fn_key = function_key_from_obj(template_id)
		if fn_key is None:
			continue
		stored_fp = fn_key.decl_fingerprint
		name = fn_key.name

		if "HashSetIter" in name and "next" in name:
			hashset_iter_seen = True

		# Reconstruct a minimal FnSignature from the decoded signature entry,
		# mirroring the consume-time path in driftc.py.
		sig_entry = entry.get("signature")
		if not isinstance(sig_entry, dict):
			continue

		# Decode param_types.
		param_types_raw = sig_entry.get("param_types")
		param_types = None
		if isinstance(param_types_raw, list):
			decoded_pts = []
			ok = True
			for pt in param_types_raw:
				te = decode_type_expr(pt)
				if te is None:
					ok = False
					break
				decoded_pts.append(te)
			if ok:
				param_types = decoded_pts

		return_type = None
		return_raw = sig_entry.get("return_type")
		if return_raw is not None:
			return_type = decode_type_expr(return_raw)

		if param_types is None or return_type is None:
			continue

		# Build type_params / impl_type_params from the signature entry.
		dummy_fn_id = FunctionId(module=fn_key.module_path, name=fn_key.name, ordinal=0)
		type_params = []
		for idx, tp_name in enumerate(sig_entry.get("type_params") or []):
			tp_id = TypeParamId(owner=dummy_fn_id, index=idx)
			type_params.append(TypeParam(id=tp_id, name=tp_name))
		impl_type_params = []
		for idx, tp_name in enumerate(sig_entry.get("impl_type_params") or []):
			tp_id = TypeParamId(owner=dummy_fn_id, index=idx)
			impl_type_params.append(TypeParam(id=tp_id, name=tp_name))

		sig = FnSignature(
			name=name,
			is_method=bool(sig_entry.get("is_method", False)),
			self_mode=sig_entry.get("self_mode"),
			param_types=param_types,
			return_type=return_type,
			type_params=type_params,
			impl_type_params=impl_type_params,
		)

		req = entry.get("require")
		decl_fp, _layout = compute_template_decl_fingerprint(
			sig,
			declared_name=name,
			module_id=fn_key.module_path,
			require_expr=req if req is not None else None,
			default_package="std",
			module_packages=None,
		)
		if decl_fp != stored_fp:
			mismatches.append(
				f"  {name}: stored={stored_fp[:16]}… computed={decl_fp[:16]}…"
			)

	assert hashset_iter_seen, (
		"HashSetIter…next template not found in std.containers templates"
	)
	assert not mismatches, (
		f"fingerprint roundtrip mismatches in std.containers ({len(mismatches)}):\n"
		+ "\n".join(mismatches)
	)


def test_k4_stdlib_deploy_consume_no_fingerprint_mismatch(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""K4 regression (deploy-path): consuming the stdlib package must not
	emit any fingerprint mismatch notes.

	This directly pins the user-visible bug: when a consumer imports
	std.containers and uses HashSet, the template import path must not
	skip any templates due to fingerprint mismatch.

	The test has two layers:
	1. Direct fingerprint verification for the specific HashSetIter…next
	   template key (bypasses display-name dedup masking).
	2. Full compile of a consumer that uses HashSet, asserting no
	   fingerprint-mismatch notes on stderr.
	"""
	from lang.driftc.checker import FnSignature, TypeParam
	from lang.driftc.core.types_core import TypeParamId
	from lang.driftc.core.function_id import FunctionId
	from lang.driftc.core.function_key import function_key_from_obj
	from lang.driftc.packages.provisional_dmir_v0 import (
		compute_template_decl_fingerprint,
		decode_generic_templates,
		decode_type_expr,
	)
	from lang.driftc.packages.provider_v1 import load_package_v1

	pkg_path = _build_stdlib_package(tmp_path)
	_ = capsys.readouterr()  # drain build output

	# Layer 1: directly verify HashSetIter…next fingerprint roundtrips.
	pkg = load_package_v1(pkg_path)
	payload = pkg.modules_by_id["std.containers"].payload
	templates = decode_generic_templates(payload.get("generic_templates"))
	target_entry = None
	for entry in templates:
		if not isinstance(entry, dict):
			continue
		fk = function_key_from_obj(entry.get("template_id"))
		if fk is not None and "HashSetIter" in fk.name and "next" in fk.name:
			target_entry = entry
			break
	assert target_entry is not None, (
		"HashSetIter…next template not found in std.containers payload"
	)
	fk = function_key_from_obj(target_entry["template_id"])
	sig_entry = target_entry["signature"]
	pts = [decode_type_expr(p) for p in sig_entry.get("param_types", [])]
	ret = decode_type_expr(sig_entry.get("return_type"))
	dummy_fn_id = FunctionId(module=fk.module_path, name=fk.name, ordinal=0)
	tps = [TypeParam(id=TypeParamId(owner=dummy_fn_id, index=i), name=n) for i, n in enumerate(sig_entry.get("type_params") or [])]
	itps = [TypeParam(id=TypeParamId(owner=dummy_fn_id, index=i), name=n) for i, n in enumerate(sig_entry.get("impl_type_params") or [])]
	sig = FnSignature(
		name=fk.name,
		is_method=bool(sig_entry.get("is_method", False)),
		self_mode=sig_entry.get("self_mode"),
		param_types=pts,
		return_type=ret,
		type_params=tps,
		impl_type_params=itps,
	)
	req = target_entry.get("require")
	decl_fp, _ = compute_template_decl_fingerprint(
		sig,
		declared_name=fk.name,
		module_id=fk.module_path,
		require_expr=req if req is not None else None,
		default_package="std",
		module_packages=None,
	)
	assert decl_fp == fk.decl_fingerprint, (
		f"HashSetIter…next fingerprint mismatch at consume-time: "
		f"stored={fk.decl_fingerprint[:16]}… computed={decl_fp[:16]}…"
	)

	# Layer 2: full consumer compile — no fingerprint mismatch notes on stderr.
	pkg_root = pkg_path.parent
	consumer = tmp_path / "consumer"
	main_src = consumer / "main.drift"
	_write_file(
		main_src,
		"""\
module main;

import std.containers as containers;

pub fn main() nothrow -> Int {
	var s = containers.HashSet<Int>.new();
	s.insert(1);
	return 0;
}
""",
	)

	rc = driftc_main([
		"-M", str(consumer),
		"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
		"--package-root", str(pkg_root),
		"--dep", "std@0.0.0-test",
		"--allow-unsigned-from", str(pkg_root),
		"--dev",
		str(main_src),
		"--emit-ir", str(tmp_path / "out.ll"),
	])
	captured = capsys.readouterr()
	assert "fingerprint mismatch" not in captured.err, (
		f"unexpected fingerprint mismatch note in stderr:\n{captured.err}"
	)


def test_k10_module_qualified_struct_ctor_from_package(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Regression: module-qualified struct ctor calls must work for external
	modules loaded from package-root.

	This pins the mariadb reported failure:
	  conc.Duration(millis = ...)
	which was incorrectly rejected in parser as non-struct when stdlib was
	consumed from package artifacts. The core bug is in package-root
	module-qualified constructor resolution, so this test uses a non-reserved
	external package fixture.
	"""
	build = tmp_path / "pkg_build"
	mod_dir = build / "acme" / "concurrent"
	_write_file(
		mod_dir / "concurrent.drift",
		"""\
module acme.concurrent;

export { Duration };

pub struct Duration {
	pub millis: Int
}
""",
	)
	pkg_path = tmp_path / "pkgs" / "acme.concurrent.dmp"
	pkg_path.parent.mkdir(parents=True, exist_ok=True)
	rc = driftc_main([
		"--dev",
		"-M", str(build),
		"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
		str(mod_dir / "concurrent.drift"),
		*_emit_pkg_args("acme.concurrent"),
		"--emit-package", str(pkg_path),
	])
	assert rc == 0, "failed to build acme.concurrent package fixture"
	pkg_root = pkg_path.parent
	consumer = tmp_path / "consumer"
	main_src = consumer / "main.drift"
	_write_file(
		main_src,
		"""\
module main;

import acme.concurrent as conc;

pub fn main() nothrow -> Int {
	val d1 = conc.Duration(millis = 1);
	val d2 = conc.Duration(millis = d1.millis + 1);
	return d2.millis;
}
""",
	)

	rc = driftc_main([
		"-M", str(consumer),
		"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
		"--package-root", str(pkg_root),
		"--dep", "acme.concurrent@0.0.0",
		"--allow-unsigned-from", str(pkg_root),
		"--dev",
		str(main_src),
		"--emit-ir", str(tmp_path / "out.ll"),
	])
	assert rc == 0, "module-qualified external struct ctor should compile from package-root"
	captured = capsys.readouterr()
	assert "module-qualified constructor call 'conc.Duration(...)' is only supported for structs in v1" not in captured.err


# ── K11: Package-consumed variant tombstone metadata preservation ─────


def _emit_tombstone_variant_pkg(tmp_path: Path) -> Path:
	"""Emit a package containing a variant with a @tombstone arm."""
	build = tmp_path / "pkg_build"
	mod_dir = build / "acme" / "result"
	_write_file(
		mod_dir / "result.drift",
		"""\
module acme.result;

export { Outcome };

pub variant Outcome<T, E> {
	Ok(value: T),
	Err(err: E),
	@tombstone Tombstone
}
""",
	)
	pkg_path = tmp_path / "pkgs" / "acme.result.dmp"
	pkg_path.parent.mkdir(parents=True, exist_ok=True)
	rc = driftc_main([
		"--dev",
		"-M", str(build),
		"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
		str(mod_dir / "result.drift"),
		*_emit_pkg_args("acme.result"),
		"--emit-package", str(pkg_path),
	])
	assert rc == 0, "failed to build tombstone variant package"
	return pkg_path


def test_k11_tombstone_match_exhaustiveness(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""K11 regression: matching Ok/Err on a variant with @tombstone must be
	exhaustive — tombstone is internal and pruned from the required set.

	Without tombstone metadata, the type checker sees a 3-arm variant and
	reports E-MATCH-NONEXHAUSTIVE (missing: Tombstone).
	"""
	src = tmp_path / "src"
	mod_dir = src / "acme" / "result"
	_write_file(
		mod_dir / "result.drift",
		"""\
module acme.result;

export { Outcome };

pub variant Outcome<T, E> {
	Ok(value: T),
	Err(err: E),
	@tombstone Tombstone
}
""",
	)
	main_src = src / "main.drift"
	_write_file(
		main_src,
		"""\
module main;

import acme.result as result;

fn check(o: result.Outcome<Int, Int>) nothrow -> Int {
	return match o {
		result.Outcome::Ok(value) => { value },
		result.Outcome::Err(err) => { err },
	};
}

pub fn main() nothrow -> Int {
	val o: result.Outcome<Int, Int> = result.Outcome::Ok(42);
	return check(move o);
}
""",
	)

	rc, payload = _run_driftc_json(
		[
			"-M", str(src),
			"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
			"--dev",
			str(main_src),
			str(mod_dir / "result.drift"),
			"--emit-ir", str(tmp_path / "out.ll"),
		],
		capsys,
	)
	diags = payload.get("diagnostics", [])
	messages = " ".join(d.get("message", "") for d in diags)
	assert "NONEXHAUSTIVE" not in messages, (
		f"expected exhaustive match (tombstone pruned); got: {messages}"
	)
	assert "Tombstone" not in messages, (
		f"tombstone leaked into diagnostics: {messages}"
	)
	assert rc == 0, f"expected successful compilation; diagnostics: {messages}"


def test_k11_tombstone_result_inference(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""K11 regression: constructing and using a Result-like variant with
	@tombstone must not fail type argument inference.

	Without tombstone metadata, constructor analysis may be destabilised
	and produce 'cannot infer type arguments' errors.
	"""
	src = tmp_path / "src"
	mod_dir = src / "acme" / "result"
	_write_file(
		mod_dir / "result.drift",
		"""\
module acme.result;

export { Outcome };

pub variant Outcome<T, E> {
	Ok(value: T),
	Err(err: E),
	@tombstone Tombstone
}
""",
	)
	main_src = src / "main.drift"
	_write_file(
		main_src,
		"""\
module main;

import acme.result as result;

fn get_ok() nothrow -> result.Outcome<Int, Int> {
	return result.Outcome::Ok(42);
}

fn get_err() nothrow -> result.Outcome<Int, Int> {
	return result.Outcome::Err(1);
}

pub fn main() nothrow -> Int {
	val ok = get_ok();
	val err = get_err();
	val v1 = match ok {
		result.Outcome::Ok(value) => { value },
		result.Outcome::Err(err) => { err },
	};
	val v2 = match err {
		result.Outcome::Ok(value) => { value },
		result.Outcome::Err(err) => { err },
	};
	return v1 + v2;
}
""",
	)

	rc, payload = _run_driftc_json(
		[
			"-M", str(src),
			"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
			"--dev",
			str(main_src),
			str(mod_dir / "result.drift"),
			"--emit-ir", str(tmp_path / "out.ll"),
		],
		capsys,
	)
	diags = payload.get("diagnostics", [])
	messages = " ".join(d.get("message", "") for d in diags)
	assert "cannot infer" not in messages.lower(), (
		f"type inference failed for variant with tombstone: {messages}"
	)
	assert rc == 0, f"expected successful compilation; diagnostics: {messages}"


def test_k11_tombstone_schema_preserved_after_link(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""K11 regression (unit): after type-table link, the host variant schema
	for a package-consumed variant must preserve tombstone_ctor metadata.

	This directly asserts the linker fix — without it, tombstone_ctor is
	None on the host side even though the package schema has it.
	"""
	from lang.driftc.core.types_core import TypeTable
	from lang.driftc.packages.provider_v1 import load_package_v1
	from lang.driftc.packages.type_table_link_v0 import import_type_tables_and_build_typeid_maps

	pkg_path = _emit_tombstone_variant_pkg(tmp_path)
	_ = capsys.readouterr()
	pkg = load_package_v1(pkg_path)

	# Extract the raw type_table dict from the package payload
	# (same as driftc.py does when collecting pkg_tt_objs).
	pkg_tt_obj = None
	for _mid, mod in pkg.modules_by_id.items():
		tt = mod.payload.get("type_table")
		if isinstance(tt, dict):
			pkg_tt_obj = tt
			break
	assert pkg_tt_obj is not None, "no type_table in package payload"

	# Verify the package payload encodes tombstone_ctor.
	raw_variant_schemas = pkg_tt_obj.get("variant_schemas", {})
	pkg_has_tombstone = False
	for _base_id_str, schema_obj in raw_variant_schemas.items():
		if schema_obj.get("name") == "Outcome":
			assert schema_obj.get("tombstone_ctor") == "Tombstone", (
				f"package payload variant schema missing tombstone_ctor: {schema_obj}"
			)
			pkg_has_tombstone = True
			break
	assert pkg_has_tombstone, "Outcome variant_schema not found in package payload"

	# Link into a fresh host type table.
	host = TypeTable()
	_tid_maps = import_type_tables_and_build_typeid_maps(
		[pkg_tt_obj],
		host=host,
	)

	# Find the linked Outcome variant in the host and assert tombstone_ctor.
	host_variant_found = False
	for _base_id, schema in host.variant_schemas.items():
		if schema.name == "Outcome" and schema.module_id == "acme.result":
			assert schema.tombstone_ctor == "Tombstone", (
				f"host schema lost tombstone_ctor after link: {schema}"
			)
			host_variant_found = True
			break
	assert host_variant_found, "Outcome variant not found in host type table after link"


# ── K12: Package-consumed generic variant constructor inference ───────


def _emit_generic_variant_pkg(tmp_path: Path) -> Path:
	"""Emit a package containing a generic variant (no tombstone)."""
	build = tmp_path / "pkg_build"
	mod_dir = build / "acme" / "result"
	_write_file(
		mod_dir / "result.drift",
		"""\
module acme.result;

export { Outcome };

pub variant Outcome<T, E> {
	Ok(value: T),
	Err(err: E)
}
""",
	)
	pkg_path = tmp_path / "pkgs" / "acme.result.dmp"
	pkg_path.parent.mkdir(parents=True, exist_ok=True)
	rc = driftc_main([
		"--dev",
		"-M", str(build),
		"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
		str(mod_dir / "result.drift"),
		*_emit_pkg_args("acme.result"),
		"--emit-package", str(pkg_path),
	])
	assert rc == 0, "failed to build generic variant package"
	return pkg_path


def test_k12_package_variant_ctor_inference(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""K12 regression: constructing a generic variant from a package-consumed
	module must infer type arguments from the function return type.

	Root cause: at parse-time the type table has no variant schema for
	package-consumed modules, so resolve_opaque_type returned Unknown for
	parameterised types like Outcome<Int, Int>.  The return type collapsed
	to Unknown, making variant-constructor inference fail with:
	  'cannot infer type arguments for variant Result'

	Fix: resolve_opaque_type now emits a parameterised FORWARD_NOMINAL
	for unknown generic nominals with a known origin module, and
	_canonicalize_signature_type_ids resolves them after type-table
	linking.
	"""
	pkg_path = _emit_generic_variant_pkg(tmp_path)
	pkg_root = pkg_path.parent

	consumer = tmp_path / "consumer"
	main_src = consumer / "main.drift"
	_write_file(
		main_src,
		"""\
module main;

import acme.result as result;

fn get_ok() nothrow -> result.Outcome<Int, Int> {
	return result.Outcome::Ok(42);
}

fn get_err() nothrow -> result.Outcome<Int, Int> {
	return result.Outcome::Err(1);
}

pub fn main() nothrow -> Int {
	val o: result.Outcome<Int, Int> = get_ok();
	return 0;
}
""",
	)

	rc, payload = _run_driftc_json(
		[
			"-M", str(consumer),
			"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
			"--package-root", str(pkg_root),
			"--dep", "acme.result@0.0.0",
			"--allow-unsigned-from", str(pkg_root),
			"--dev",
			str(main_src),
			"--emit-ir", str(tmp_path / "out.ll"),
		],
		capsys,
	)
	diags = payload.get("diagnostics", [])
	messages = " ".join(d.get("message", "") for d in diags)
	assert "cannot infer" not in messages.lower(), (
		f"type inference failed for package-consumed variant ctor: {messages}"
	)
	assert rc == 0, f"expected successful compilation; diagnostics: {messages}"


def test_k12_unresolvable_generic_nominal_errors(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""K12 negative: referencing a generic type that does not exist in a
	package-consumed module must produce a clear error — not silently pass
	or crash due to an unresolved forward nominal.
	"""
	pkg_path = _emit_generic_variant_pkg(tmp_path)
	pkg_root = pkg_path.parent

	consumer = tmp_path / "consumer"
	main_src = consumer / "main.drift"
	_write_file(
		main_src,
		"""\
module main;

import acme.result as result;

fn get() nothrow -> result.Widget<Int> {
	return result.Widget::Make(42);
}

pub fn main() nothrow -> Int {
	return 0;
}
""",
	)

	rc, payload = _run_driftc_json(
		[
			"-M", str(consumer),
			"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
			"--package-root", str(pkg_root),
			"--dep", "acme.result@0.0.0",
			"--allow-unsigned-from", str(pkg_root),
			"--dev",
			str(main_src),
			"--emit-ir", str(tmp_path / "out.ll"),
		],
		capsys,
	)
	assert rc != 0, "expected compilation to fail for non-existent generic type"
	diags = payload.get("diagnostics", [])
	messages = " ".join(d.get("message", "") for d in diags)
	assert "Widget" in messages, (
		f"error should mention the unresolved type name 'Widget'; got: {messages}"
	)


# ── K13: Boundary-call nothrow analysis ──────────────────────────────


def _emit_nothrow_method_pkg(tmp_path: Path) -> Path:
	"""Emit a package with a struct + nothrow method."""
	build = tmp_path / "pkg_build"
	mod_dir = build / "acme" / "util"
	_write_file(
		mod_dir / "util.drift",
		"""\
module acme.util;

export { Counter, make_counter };

pub struct Counter {
	pub value: Int
}

implement Counter {
	pub fn increment(self: &mut Counter) nothrow -> Void {
		self.value = self.value + 1;
	}

	pub fn get(self: &Counter) nothrow -> Int {
		return self.value;
	}
}

pub fn make_counter(start: Int) nothrow -> Counter {
	return Counter(value = start);
}
""",
	)
	pkg_path = tmp_path / "pkgs" / "acme.util.dmp"
	pkg_path.parent.mkdir(parents=True, exist_ok=True)
	rc = driftc_main([
		"--dev",
		"-M", str(build),
		"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
		str(mod_dir / "util.drift"),
		*_emit_pkg_args("acme.util"),
		"--emit-package", str(pkg_path),
	])
	assert rc == 0, "failed to build acme.util package"
	return pkg_path


def test_k13_boundary_nothrow_direct_call(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""K13 regression: a nothrow function calling a nothrow package function
	must not be flagged as 'may throw'.

	The boundary-call path in the checker must not unconditionally force
	call_can_throw=True for exported boundary calls — it should respect
	the callee's declared nothrow semantics.
	"""
	pkg_path = _emit_nothrow_method_pkg(tmp_path)
	pkg_root = pkg_path.parent

	consumer = tmp_path / "consumer"
	main_src = consumer / "main.drift"
	_write_file(
		main_src,
		"""\
module main;

import acme.util as util;

pub fn main() nothrow -> Int {
	var c = util.make_counter(0);
	c.increment();
	return c.get();
}
""",
	)

	rc, payload = _run_driftc_json(
		[
			"-M", str(consumer),
			"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
			"--package-root", str(pkg_root),
			"--dep", "acme.util@0.0.0",
			"--allow-unsigned-from", str(pkg_root),
			"--dev",
			str(main_src),
			"--emit-ir", str(tmp_path / "out.ll"),
		],
		capsys,
	)
	diags = payload.get("diagnostics", [])
	messages = " ".join(d.get("message", "") for d in diags)
	assert "may throw" not in messages, (
		f"nothrow-to-nothrow boundary call should not be flagged: {messages}"
	)
	assert rc == 0, f"expected successful compilation; diagnostics: {messages}"


def test_k13_boundary_nothrow_direct_call_does_not_poison(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""K13-a: a nothrow caller invoking ONLY a nothrow free function across
	a package boundary must compile without 'may throw'.

	This isolates the HCall boundary path in the checker — no method
	wrappers involved.
	"""
	pkg_path = _emit_nothrow_method_pkg(tmp_path)
	pkg_root = pkg_path.parent

	consumer = tmp_path / "consumer"
	main_src = consumer / "main.drift"
	_write_file(
		main_src,
		"""\
module main;

import acme.util as util;

pub fn main() nothrow -> Int {
	val c = util.make_counter(42);
	return c.value;
}
""",
	)

	rc, payload = _run_driftc_json(
		[
			"-M", str(consumer),
			"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
			"--package-root", str(pkg_root),
			"--dep", "acme.util@0.0.0",
			"--allow-unsigned-from", str(pkg_root),
			"--dev",
			str(main_src),
			"--emit-ir", str(tmp_path / "out.ll"),
		],
		capsys,
	)
	diags = payload.get("diagnostics", [])
	messages = " ".join(d.get("message", "") for d in diags)
	assert "may throw" not in messages, (
		f"nothrow direct call to nothrow free function should not poison caller: {messages}"
	)
	assert rc == 0, f"expected successful compilation; diagnostics: {messages}"


def test_k13_wrapper_path_preserves_nothrow(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""K13-b: a nothrow caller invoking ONLY nothrow methods across a
	package boundary must compile without 'may throw'.

	This isolates the HMethodCall wrapper path — the checker must look
	through wraps_target_fn_id to the wrapped method's declared_can_throw.
	"""
	pkg_path = _emit_nothrow_method_pkg(tmp_path)
	pkg_root = pkg_path.parent

	consumer = tmp_path / "consumer"
	main_src = consumer / "main.drift"
	_write_file(
		main_src,
		"""\
module main;

import acme.util as util;

pub fn main() nothrow -> Int {
	var c = util.Counter(value = 0);
	c.increment();
	c.increment();
	return c.get();
}
""",
	)

	rc, payload = _run_driftc_json(
		[
			"-M", str(consumer),
			"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
			"--package-root", str(pkg_root),
			"--dep", "acme.util@0.0.0",
			"--allow-unsigned-from", str(pkg_root),
			"--dev",
			str(main_src),
			"--emit-ir", str(tmp_path / "out.ll"),
		],
		capsys,
	)
	diags = payload.get("diagnostics", [])
	messages = " ".join(d.get("message", "") for d in diags)
	assert "may throw" not in messages, (
		f"nothrow method call via wrapper path should not poison caller: {messages}"
	)
	assert rc == 0, f"expected successful compilation; diagnostics: {messages}"


# ── K14: --entry plumbing in deployed path ─────────────────────────────


def test_k14_entry_flag_honored_with_packages(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""K14 regression: --entry pkg.mod::main must be forwarded to the
	deployed compile path (loaded_pkgs branch) so that validate_entrypoint
	finds the correct entry function instead of defaulting to main::main.
	"""
	pkg_path = _emit_nothrow_method_pkg(tmp_path)
	pkg_root = pkg_path.parent

	consumer = tmp_path / "consumer"
	main_src = consumer / "runner.drift"
	_write_file(
		main_src,
		"""\
module runner;

import acme.util as util;

pub fn main() nothrow -> Int {
	val c = util.make_counter(7);
	return c.value;
}
""",
	)

	rc, payload = _run_driftc_json(
		[
			"-M", str(consumer),
			"--stdlib-root", str(_empty_stdlib_root(tmp_path)),
			"--package-root", str(pkg_root),
			"--dep", "acme.util@0.0.0",
			"--allow-unsigned-from", str(pkg_root),
			"--dev",
			"--entry", "runner::main",
			str(main_src),
			"--emit-ir", str(tmp_path / "out.ll"),
		],
		capsys,
	)
	diags = payload.get("diagnostics", [])
	messages = " ".join(d.get("message", "") for d in diags)
	assert "missing entry point" not in messages, (
		f"--entry runner::main should be honored in deployed path: {messages}"
	)
	assert rc == 0, f"expected successful compilation; diagnostics: {messages}"
