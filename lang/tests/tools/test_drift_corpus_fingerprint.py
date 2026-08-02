# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Hostile pins for the ownership-corpus run fingerprint + compile contract
(tools/drift_corpus_fingerprint.py, tools/corpus_compile_contract.py).

The fingerprint is load-bearing: a miss silently blesses a stale result.  These
pins prove the fingerprint and the compiler consume ONE contract that shares the
production link/native-lib/sanitizer authority (lang.driftc.link_selection),
that COLLECTORS observe real changes, and that read_fingerprint enforces EXACT
shape + recomputed composite.
"""
from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _load(name: str):
	spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	return mod


_fp = _load("drift_corpus_fingerprint")
_cc = _load("corpus_compile_contract")


# ══ item 1: the SINGLE compile contract (parity with driftc) ═════════

def test_contract_env_true_semantics_zero_is_false(monkeypatch):
	from lang.driftc.env_flags import env_true
	monkeypatch.setenv("DRIFT_ASAN", "0")
	assert env_true("DRIFT_ASAN") is False
	assert _cc.runtime_variant() == "default"        # "0" -> not asan
	monkeypatch.setenv("DRIFT_ASAN", "1")
	assert _cc.runtime_variant() == "asan"


def test_contract_alloc_track_pinned_false(monkeypatch):
	monkeypatch.setenv("DRIFT_ALLOC_TRACK", "1")
	assert _cc.runtime_variant() != "alloc_track"


def test_contract_sanitize_arg_overrides_env(monkeypatch):
	"""driftc.py:9557 — an explicit --sanitize is authoritative over the env
	aliases; the contract must model the SAME archive driftc links."""
	monkeypatch.setenv("DRIFT_ASAN", "0")
	monkeypatch.setenv("DRIFT_UBSAN", "0")
	assert _cc.runtime_variant(["--sanitize=address"]) == "asan"
	assert _cc.runtime_variant(["--sanitize", "address,undefined"]) == "asan_ubsan"
	monkeypatch.setenv("DRIFT_ASAN", "1")
	assert _cc.runtime_variant([]) == "asan"          # env applies when arg absent


def test_contract_runtime_variant_parity_with_authority(monkeypatch):
	from lang.language_runtime import runtime_archive_variant
	from lang.driftc.env_flags import env_true
	for asan, ubsan, dbg in [("1", "", ""), ("", "1", ""), ("", "", "1"), ("0", "0", "0")]:
		monkeypatch.setenv("DRIFT_ASAN", asan)
		monkeypatch.setenv("DRIFT_UBSAN", ubsan)
		monkeypatch.setenv("DRIFT_DEBUG", dbg)
		expected = runtime_archive_variant(
			debug_style=env_true("DRIFT_DEBUG"), asan_enabled=env_true("DRIFT_ASAN"),
			ubsan_enabled=env_true("DRIFT_UBSAN"), alloc_track_enabled=False)
		assert _cc.runtime_variant() == expected


def test_contract_clang_matches_driftc_ignores_clang_bin(monkeypatch):
	import shutil
	monkeypatch.setenv("CLANG_BIN", "/nonexistent/clang")
	assert _cc.resolve_clang() == shutil.which("clang")


def test_contract_explicit_linker_via_shared_authority():
	from lang.driftc import link_selection
	assert _cc.resolve_linker(["--linker", "ld"])["selection"] == "ld"
	assert _cc.resolve_linker(["--linker", "gold"])["selection"] == "gold"
	assert _cc.resolve_linker(["--linker=ld"])["selection"] == "ld"
	assert _cc.resolve_linker([])["selection"] == link_selection.select_linker(None)


def test_contract_native_libs_keyed_on_debug_style():
	from lang.driftc import link_selection
	assert _cc.native_link_libs(False) == link_selection.native_link_lib_names(False)
	assert _cc.native_link_libs(True) == link_selection.native_link_lib_names(True)
	assert "z" in _cc.native_link_libs(False) or _cc.native_link_libs(False) == []


def test_contract_normalized_env_clears_unsupported_and_pins():
	ambient = {"PATH": "/x", "DRIFT_DEBUG": "1", "DRIFT_FOO_UNSUPPORTED": "bar",
	           "DRIFTC_WHATEVER": "z", "DRIFT_STRING_ARC_AUDIT_VERBOSE": "1",
	           "PYTHONHASHSEED": "999"}
	env = _cc.normalized_child_env(ambient)
	assert env["DRIFT_DEBUG"] == "1"
	assert env["DRIFT_STRING_ARC_AUDIT_VERBOSE"] == "1"
	assert env["DRIFT_STRING_ARC_AUDIT"] == "1"
	assert "DRIFT_FOO_UNSUPPORTED" not in env
	assert "DRIFTC_WHATEVER" not in env
	assert env["PATH"] == "/x"                             # non-Drift preserved
	assert env["PYTHONHASHSEED"] == "0"                    # PINNED deterministic


def test_contract_fingerprint_env_records_build_relevant(monkeypatch):
	"""F4: PATH/library/locale/PYTHONPATH values are in the identity; the
	per-fixture output path is not."""
	monkeypatch.setenv("PATH", "/a:/b")
	monkeypatch.setenv("LD_LIBRARY_PATH", "/libs")
	monkeypatch.setenv("DRIFT_STRING_ARC_AUDIT_FILE", "/per/fixture/out.jsonl")
	fenv = _cc.fingerprint_env()
	assert fenv["PATH"] == "/a:/b" and fenv["LD_LIBRARY_PATH"] == "/libs"
	assert fenv["PYTHONHASHSEED"] == "0"
	assert _cc.AUDIT_FILE_ENV not in fenv


# ══ collector-level pins (collectors observe REAL changes) ══════════

def _mini_root(tmp_path: Path) -> Path:
	(tmp_path / "lang" / "driftc").mkdir(parents=True)
	(tmp_path / "lang" / "driftc" / "a.py").write_text("real source")
	(tmp_path / "lang" / "tests").mkdir()
	(tmp_path / "lang" / "tests" / "t.py").write_text("test only")
	(tmp_path / "stdlib").mkdir()
	(tmp_path / "stdlib" / "s.drift").write_text("stdlib")
	return tmp_path


def test_compile_source_collector_observes_add_delete_modify(tmp_path):
	root = _mini_root(tmp_path)
	h0 = _fp._compile_source_digest(root)
	(root / "lang" / "driftc" / "b.py").write_text("new")
	h1 = _fp._compile_source_digest(root)
	assert h1 != h0
	(root / "lang" / "driftc" / "a.py").write_text("real source CHANGED")
	h2 = _fp._compile_source_digest(root)
	assert h2 != h1
	(root / "lang" / "driftc" / "b.py").unlink()
	assert _fp._compile_source_digest(root) != h2
	base = _fp._compile_source_digest(root)
	(root / "lang" / "tests" / "t.py").write_text("test only CHANGED")
	assert _fp._compile_source_digest(root) == base   # test file invisible


def test_audit_tool_digest_includes_check_tool_and_record_schema(tmp_path):
	tools = tmp_path / "tools"
	tools.mkdir()
	assert "drift_corpus_check.py" in _fp._AUDIT_TOOL_FILES
	for name in _fp._AUDIT_TOOL_FILES:
		(tools / name).write_text(f"# {name}\nRECORD_SCHEMA_VERSION = 2\n")
	h0 = _fp._audit_tool_digest(tmp_path)
	# a change to the check tool's record schema moves the digest
	(tools / "drift_corpus_check.py").write_text("# changed\nRECORD_SCHEMA_VERSION = 3\n")
	assert _fp._audit_tool_digest(tmp_path) != h0


def _fake_binary(tmp_path: Path, name: str, body: str, version: str) -> str:
	p = tmp_path / name
	p.write_text(f"#!/bin/sh\n# {body}\necho '{version}'\n")
	p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
	return str(p)


def test_bin_identity_exe_bytes_change_with_unchanged_version(tmp_path):
	b1 = _fake_binary(tmp_path, "fakecc", "build-A", "clang 20.0")
	id1 = _fp._bin_identity(b1)
	Path(b1).write_text(f"#!/bin/sh\n# build-B DIFFERENT BYTES\necho 'clang 20.0'\n")
	id2 = _fp._bin_identity(b1)
	assert id2["version_sha256"] == id1["version_sha256"]
	assert id2["bytes_sha256"] != id1["bytes_sha256"]


def test_native_lib_identity_via_shared_authority(monkeypatch):
	from lang.driftc import link_selection
	# unavailable -> unresolved (models a library disappearing)
	monkeypatch.setattr(link_selection, "resolve_native_lib_path", lambda name: None)
	ent = _fp._native_lib_identity("z")
	assert ent["resolved"] is None and ent["bytes_sha256"] is None
	# available -> abs path + bytes, resolved via link_selection (NOT clang)
	monkeypatch.setattr(link_selection, "resolve_native_lib_path",
	                    lambda name: str(tmp := Path(__file__)))
	e = _fp._native_lib_identity("z")
	assert e["resolved"] == str(Path(__file__)) and e["bytes_sha256"]


def test_env_collector_zero_vs_true(monkeypatch):
	monkeypatch.setenv("DRIFT_ASAN", "0")
	e0 = _cc.fingerprint_env()
	monkeypatch.setenv("DRIFT_ASAN", "1")
	e1 = _cc.fingerprint_env()
	assert e0["DRIFT_ASAN"] == "0" and e1["DRIFT_ASAN"] == "1" and e0 != e1


def test_collect_includes_every_collector(monkeypatch):
	monkeypatch.setattr(_fp, "_compile_source_digest", lambda root: "a" * 64)
	monkeypatch.setattr(_fp, "_stdlib_digest", lambda root: "b" * 64)
	monkeypatch.setattr(_fp, "_audit_tool_digest", lambda root: "c" * 64)
	monkeypatch.setattr(_fp, "prebuild_runtime",
	                    lambda root, extra_args=(): {"variant": "default", "RT": "R"})
	monkeypatch.setattr(_fp, "_native_libs_identity", lambda debug_style: {"NL": "N"})
	monkeypatch.setattr(_fp, "_tool_identities", lambda extra: {"T": "T"})
	comp = _fp.collect_toolchain_components(ROOT, extra_args=["ARG"])
	assert comp["compile_source"] == "a" * 64 and comp["stdlib"] == "b" * 64
	assert comp["audit_tool"] == "c" * 64 and comp["runtime"]["RT"] == "R"
	assert comp["native_libs"] == {"NL": "N"} and comp["tools"] == {"T": "T"}
	assert "<FIXTURE>" in comp["driftc_argv_template"] and "env" in comp
	assert set(comp) == set(_fp._COMPONENT_SHAPE)   # exactly the validated shape


# ══ read_fingerprint integrity (EXACT shape + recomputed composite) ══

def _components() -> dict:
	return {
		"contract_schema": _cc.CONTRACT_SCHEMA_VERSION,
		"compile_source": "a" * 64,
		"stdlib": "b" * 64,
		"audit_tool": "c" * 64,
		"runtime": {"variant": "default", "archive_sha256": "d" * 64},
		"native_libs": {"z": {"soname": "libz.so", "resolved": "/x", "bytes_sha256": "e" * 64}},
		"tools": {"clang": {"path": "/usr/bin/clang"}},
		"driftc_argv_template": ["--dev"],
		"env": {"PATH": "/x", "PYTHONHASHSEED": "0"},
	}


def _real_toolchain() -> dict:
	comp = _components()
	return {"schema_version": _fp.FINGERPRINT_SCHEMA_VERSION, "kind": "toolchain",
	        "components": comp, "composite": _fp.composite_hash(comp),
	        "diagnostic": {"git_rev": None}}


def _write(tmp_path, obj):
	p = tmp_path / "fp.json"
	p.write_text(json.dumps(obj))
	return p


def test_read_accepts_well_formed(tmp_path):
	assert _fp.read_fingerprint(_write(tmp_path, _real_toolchain()))["kind"] == "toolchain"


def test_read_rejects_tampered_components(tmp_path):
	t = _real_toolchain()
	t["components"]["compile_source"] = "f" * 64          # valid shape, composite stale
	with pytest.raises(ValueError, match="composite mismatch"):
		_fp.read_fingerprint(_write(tmp_path, t))


def test_read_rejects_tampered_composite(tmp_path):
	t = _real_toolchain()
	t["composite"] = "0" * 64
	with pytest.raises(ValueError, match="composite mismatch"):
		_fp.read_fingerprint(_write(tmp_path, t))


def test_read_rejects_missing_key(tmp_path):
	t = _real_toolchain()
	del t["diagnostic"]                                   # F9: missing key must fail
	with pytest.raises(ValueError, match="key set"):
		_fp.read_fingerprint(_write(tmp_path, t))


def test_read_rejects_unknown_key(tmp_path):
	t = _real_toolchain()
	t["surprise"] = 1
	with pytest.raises(ValueError, match="key set"):
		_fp.read_fingerprint(_write(tmp_path, t))


def test_read_rejects_wrong_kind(tmp_path):
	t = _real_toolchain()
	t["kind"] = "bogus"
	with pytest.raises(ValueError, match="unknown fingerprint kind"):
		_fp.read_fingerprint(_write(tmp_path, t))


def test_read_rejects_missing_component(tmp_path):
	comp = _components()
	del comp["env"]
	t = {"schema_version": _fp.FINGERPRINT_SCHEMA_VERSION, "kind": "toolchain",
	     "components": comp, "composite": _fp.composite_hash(comp),
	     "diagnostic": {"git_rev": None}}
	with pytest.raises(ValueError, match="components shape wrong"):
		_fp.read_fingerprint(_write(tmp_path, t))


def test_read_rejects_nonhex_component(tmp_path):
	comp = _components()
	comp["compile_source"] = "z" * 64                     # right length, not hex
	t = {"schema_version": _fp.FINGERPRINT_SCHEMA_VERSION, "kind": "toolchain",
	     "components": comp, "composite": _fp.composite_hash(comp),
	     "diagnostic": {"git_rev": None}}
	with pytest.raises(ValueError, match="wrong shape/type"):
		_fp.read_fingerprint(_write(tmp_path, t))


def test_snapshot_read_validates_and_rejects_nonhex_universe(tmp_path):
	tc = _real_toolchain()
	snap = _fp.run_snapshot(tc, "a" * 64)
	assert _fp.read_fingerprint(_write(tmp_path, snap))["kind"] == "run_snapshot"
	snap2 = _fp.run_snapshot(tc, "not-hex")
	with pytest.raises(ValueError, match="static_universe_digest not a hex"):
		_fp.read_fingerprint(_write(tmp_path, snap2))


def test_snapshot_read_rejects_tampered_embedded_toolchain(tmp_path):
	tc = _real_toolchain()
	snap = _fp.run_snapshot(tc, "a" * 64)
	snap["toolchain"]["components"]["compile_source"] = "f" * 64
	with pytest.raises(ValueError):
		_fp.read_fingerprint(_write(tmp_path, snap))


# ══ composite basics + atomic write ═════════════════════════════════

def test_composite_deterministic_and_git_rev_diagnostic_only(monkeypatch):
	monkeypatch.setattr(_fp, "collect_toolchain_components", lambda root, extra_args: {"k": "v"})
	a = _fp.toolchain_fingerprint(ROOT, extra_args=[], git_rev="A")
	b = _fp.toolchain_fingerprint(ROOT, extra_args=[], git_rev="B")
	assert a["composite"] == b["composite"]
	assert "git" not in _fp.canonical_json(a["components"]).lower()


def test_run_snapshot_changes_on_universe_and_toolchain():
	tc1 = {"composite": "1" * 64}
	tc2 = {"composite": "2" * 64}
	assert _fp.run_snapshot(tc1, "u")["composite"] != _fp.run_snapshot(tc1, "v")["composite"]
	assert _fp.run_snapshot(tc1, "u")["composite"] != _fp.run_snapshot(tc2, "u")["composite"]


def test_write_atomic_canonical_no_volatile(tmp_path):
	fpath = tmp_path / "sub" / "fingerprint.json"
	body = _real_toolchain()
	_fp.write_atomic(fpath, body)
	text = fpath.read_text()
	assert text.endswith("\n") and json.loads(text) == body
	low = text.lower()
	for bad in ("pid", "timestamp", "scratch", "session-"):
		assert bad not in low
	assert [p.name for p in fpath.parent.iterdir()] == ["fingerprint.json"]
