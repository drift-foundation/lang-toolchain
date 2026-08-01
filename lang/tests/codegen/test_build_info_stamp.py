# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""drift-build-info/v1 stamp — W1 pins (work/toolchain-meta-stamps).

Covers the pure core (assembly canonicalization, section-payload
validation with the full fail-closed matrix, flag validation helpers)
and the driftc
CLI integration (flag rejection shapes; the emitted IR carries the
section constant whose payload round-trips back to the exact canonical
document).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from lang.build_info import (
	BuildInfoError,
	BUILD_INFO_FORMAT,
	BUILD_INFO_MAX_PAYLOAD,
	BUILD_INFO_SECTION,
	BUILD_INFO_SYMBOL,
	assemble_build_info,
	canonical_json,
	check_payload_size,
	encode_build_info,
	parse_artifact_flags,
	parse_meta_flags,
	validate_build_info_payload,
)

ROOT = Path(__file__).resolve().parents[3]


def _assemble(**over):
	kw = dict(
		git_sha="abc123", word_bits=64, build_profile="optimized",
		build_utc="2026-07-31T00:00:00Z",
		artifact=None, dependencies={}, extra={},
	)
	kw.update(over)
	return assemble_build_info(**kw)


class TestAssembly:
	def test_canonical_and_deterministic(self) -> None:
		a = _assemble(dependencies={"b.pkg": "2.0.0", "a.pkg": "1.0.0"},
		              extra={"zeta": "1", "alpha": ""})
		b = _assemble(dependencies={"a.pkg": "1.0.0", "b.pkg": "2.0.0"},
		              extra={"alpha": "", "zeta": "1"})
		assert a == b, "input ordering must not affect the document"
		# Repo JSON convention: compact separators, sorted keys, no
		# trailing newline.
		assert ": " not in a and ", " not in a
		assert not a.endswith("\n")
		doc = json.loads(a)
		assert list(doc.keys()) == sorted(doc.keys())

	def test_sections_and_types(self) -> None:
		doc = json.loads(_assemble(
			artifact={"name": "app", "version": "0.1.0",
			          "description": "d", "license": "MIT"},
			dependencies={"net-tls": "0.4.1"},
		))
		assert doc["format"] == BUILD_INFO_FORMAT
		assert isinstance(doc["toolchain"]["abi"], int)
		assert isinstance(doc["build"]["word"], int)
		assert doc["build"]["profile"] == "optimized"
		assert doc["build"]["utc"] == "2026-07-31T00:00:00Z"
		assert doc["toolchain"]["git"] == "abc123"
		assert doc["artifact"] == {"name": "app", "version": "0.1.0",
		                           "description": "d", "license": "MIT"}
		assert doc["dependencies"] == [{"name": "net-tls", "version": "0.4.1"}]

	def test_deps_sorted_by_name(self) -> None:
		doc = json.loads(_assemble(
			dependencies={"zzz": "1.0.0", "aaa": "2.0.0", "mmm": "3.0.0"}))
		assert [d["name"] for d in doc["dependencies"]] == ["aaa", "mmm", "zzz"]

	def test_unstamped_shape(self) -> None:
		doc = json.loads(_assemble())
		assert doc["artifact"] is None
		assert doc["dependencies"] == []
		assert doc["extra"] == {}
		# Required keys with "" when unavailable — never omitted.
		doc2 = json.loads(_assemble(git_sha="", build_profile="", build_utc=""))
		assert "git" in doc2["toolchain"]
		assert doc2["build"]["profile"] == "" and doc2["build"]["utc"] == ""

	def test_hostile_value_round_trip(self) -> None:
		"""JSON escaping handles |, commas, @, unicode, newlines — the
		whole reason the pipe grammar died."""
		desc = 'a | b, c@d\n"quoted" ☃ tab\t'
		doc = json.loads(_assemble(
			artifact={"name": "n|1", "version": "0.0.0+x@y",
			          "description": desc, "license": "L,|@"},
			extra={"note": desc},
		))
		assert doc["artifact"]["description"] == desc
		assert doc["extra"]["note"] == desc

	def test_partial_artifact_asserts(self) -> None:
		with pytest.raises(AssertionError):
			_assemble(artifact={"name": "x", "version": "1"})


class TestPayloadValidation:
	"""The section contract: EXACTLY the canonical JSON bytes; the
	reader is fail-closed on everything else."""

	def test_round_trip(self) -> None:
		payload = _assemble()
		assert validate_build_info_payload(payload.encode("utf-8")) == payload

	def test_cap_enforced_on_emit_and_read(self) -> None:
		with pytest.raises(BuildInfoError, match="exceeds the"):
			check_payload_size(b"x" * (BUILD_INFO_MAX_PAYLOAD + 1))
		with pytest.raises(BuildInfoError, match="exceeds the"):
			validate_build_info_payload(b"x" * (BUILD_INFO_MAX_PAYLOAD + 1))

	def test_empty_rejected(self) -> None:
		with pytest.raises(BuildInfoError, match="empty"):
			check_payload_size(b"")
		with pytest.raises(BuildInfoError, match="empty"):
			validate_build_info_payload(b"")

	def test_bad_utf8_rejected(self) -> None:
		with pytest.raises(BuildInfoError, match="UTF-8"):
			validate_build_info_payload(b"\xff\xfe\x01")

	def test_bad_json_rejected(self) -> None:
		with pytest.raises(BuildInfoError, match="not valid JSON"):
			validate_build_info_payload(b"{nope")

	def test_trailing_and_leading_bytes_rejected(self) -> None:
		raw = _assemble().encode("utf-8")
		with pytest.raises(BuildInfoError):
			validate_build_info_payload(raw + b"\x00")
		with pytest.raises(BuildInfoError):
			validate_build_info_payload(raw + b"garbage")
		with pytest.raises(BuildInfoError):
			validate_build_info_payload(b" " + raw)


class TestFlagHelpers:
	def test_artifact_all_or_none(self) -> None:
		assert parse_artifact_flags(None, None, None, None) is None
		full = parse_artifact_flags("n", "v", "d", "l")
		assert full == {"name": "n", "version": "v",
		                "description": "d", "license": "l"}
		with pytest.raises(ValueError, match="atomic"):
			parse_artifact_flags("n", None, "d", None)

	def test_artifact_values_non_empty(self) -> None:
		with pytest.raises(ValueError, match="non-empty"):
			parse_artifact_flags("n", "", "d", "l")

	def test_meta_grammar(self) -> None:
		assert parse_meta_flags([]) == {}
		assert parse_meta_flags(["a.b_c1=x", "empty="]) == \
			{"a.b_c1": "x", "empty": ""}
		with pytest.raises(ValueError, match="KEY=VALUE"):
			parse_meta_flags(["noequals"])
		with pytest.raises(ValueError, match=r"\[a-z0-9_.\]"):
			parse_meta_flags(["BadKey=1"])
		with pytest.raises(ValueError, match=r"\[a-z0-9_.\]"):
			parse_meta_flags(["=v"])
		with pytest.raises(ValueError, match="twice"):
			parse_meta_flags(["k=1", "k=2"])


# ── driftc CLI integration ───────────────────────────────────────────

_PROG = "pub fn main() nothrow -> Int {\n\treturn 0;\n}\n"


def _run_driftc(tmp_path: Path, extra_args: list[str], timeout: float):
	src = tmp_path / "main.drift"
	if not src.exists():
		src.write_text(_PROG)
	out_ll = tmp_path / "out.ll"
	cmd = [sys.executable, "-m", "lang.driftc.driftc", str(src),
	       "--emit-ir", str(out_ll)] + extra_args
	res = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT,
	                     timeout=timeout)
	return res, out_ll


def _extract_ir_stamp(out_ll: Path) -> str:
	"""Recover the raw JSON section payload from the rendered IR and
	validate it — proves emitted bytes, not internal state."""
	text = out_ll.read_text()
	m = re.search(
		rf'@{BUILD_INFO_SYMBOL} = internal constant \[\d+ x i8\] '
		rf'\[(.*?)\], section "{re.escape(BUILD_INFO_SECTION)}"', text)
	assert m, f"missing {BUILD_INFO_SYMBOL} section constant in IR"
	blob = bytes(int(b) for b in re.findall(r"i8 (\d+)", m.group(1)))
	return validate_build_info_payload(blob)


class TestDriftcCli:
	def test_stamped_compile_emits_raw_json_section(self, tmp_path) -> None:
		from lang.codegen.llvm.test_utils import sanitizer_timeout
		res, out_ll = _run_driftc(tmp_path, [
			"--artifact-name", "app", "--artifact-version", "1.2.3",
			"--artifact-description", "desc |,@ text",
			"--artifact-license", "MIT",
			"--meta", "team=x", "--meta", "note=",
		], sanitizer_timeout(120))
		assert res.returncode == 0, res.stderr[-800:]
		doc = json.loads(_extract_ir_stamp(out_ll))
		assert doc["format"] == BUILD_INFO_FORMAT
		assert doc["artifact"]["name"] == "app"
		assert doc["artifact"]["description"] == "desc |,@ text"
		assert doc["extra"] == {"team": "x", "note": ""}
		assert doc["dependencies"] == []
		assert doc["build"]["utc"]

	def test_unstamped_compile_emits_null_artifact(self, tmp_path) -> None:
		from lang.codegen.llvm.test_utils import sanitizer_timeout
		res, out_ll = _run_driftc(tmp_path, [], sanitizer_timeout(120))
		assert res.returncode == 0, res.stderr[-800:]
		doc = json.loads(_extract_ir_stamp(out_ll))
		assert doc["artifact"] is None
		assert doc["dependencies"] == [] and doc["extra"] == {}

	def test_partial_artifact_flags_rejected(self, tmp_path) -> None:
		from lang.codegen.llvm.test_utils import sanitizer_timeout
		res, _ = _run_driftc(tmp_path, [
			"--artifact-name", "app", "--artifact-version", "1.2.3",
		], sanitizer_timeout(120))
		assert res.returncode == 1
		assert "atomic" in res.stderr

	def test_empty_artifact_value_rejected(self, tmp_path) -> None:
		from lang.codegen.llvm.test_utils import sanitizer_timeout
		res, _ = _run_driftc(tmp_path, [
			"--artifact-name", "app", "--artifact-version", "",
			"--artifact-description", "d", "--artifact-license", "l",
		], sanitizer_timeout(120))
		assert res.returncode == 1
		assert "non-empty" in res.stderr

	def test_duplicate_meta_rejected(self, tmp_path) -> None:
		from lang.codegen.llvm.test_utils import sanitizer_timeout
		res, _ = _run_driftc(tmp_path, ["--meta", "k=1", "--meta", "k=2"],
		                     sanitizer_timeout(120))
		assert res.returncode == 1
		assert "twice" in res.stderr


class TestSchemaValidation:
	"""validate_build_info_payload is the gate-facing reader: the full v1 schema and the
	canonical encoding are enforced, not just the discriminator."""

	def _payload(self, text: str) -> bytes:
		return text.encode("utf-8")

	def test_discriminator_alone_is_not_enough(self) -> None:
		with pytest.raises(BuildInfoError, match="top-level keys"):
			validate_build_info_payload(self._payload('{"format":"drift-build-info/v1"}'))

	def test_wrong_types_and_extra_keys_rejected(self) -> None:
		import json as _json
		good = _json.loads(_assemble())
		bad_abi = dict(good); bad_abi["toolchain"] = dict(good["toolchain"], abi="22")
		bad_top = dict(good); bad_top["surprise"] = 1
		bad_art = dict(good); bad_art["artifact"] = {"name": "x"}
		bad_dep = dict(good); bad_dep["dependencies"] = [{"name": "b", "version": "1"}, {"name": "a", "version": "1"}]
		for doc, expect in ((bad_abi, "toolchain.abi"),
		                    (bad_top, "top-level keys"),
		                    (bad_art, "artifact must be null or exactly"),
		                    (bad_dep, "sorted by name")):
			with pytest.raises(BuildInfoError, match=expect):
				validate_build_info_payload(self._payload(canonical_json(doc)))

	def test_noncanonical_encoding_rejected(self) -> None:
		import json as _json
		doc = _json.loads(_assemble())
		pretty = _json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False)
		with pytest.raises(BuildInfoError, match="canonically"):
			validate_build_info_payload(self._payload(pretty))

	def test_duplicate_keys_rejected_via_canonical_check(self) -> None:
		text = _assemble()
		# Inject a duplicate of the format key at the front — parses
		# fine (last wins) but can never re-serialize to the same bytes.
		dup = '{"format":"drift-build-info/v1",' + text[1:]
		with pytest.raises(BuildInfoError, match="canonically"):
			validate_build_info_payload(self._payload(dup))


class TestOversizedMetaCli:
	"""P1: oversized user metadata is a normal CLI diagnostic in both
	output modes — never a traceback escaping the codegen boundary."""

	def _oversize_args(self) -> list[str]:
		# 10 values of ~115 KiB stay under the per-arg execve limit
		# (MAX_ARG_STRLEN 128 KiB) while the assembled payload
		# (~1.15 MiB) exceeds the 1 MiB section cap.
		chunk = "x" * (115 * 1024)
		out: list[str] = []
		for i in range(10):
			out += ["--meta", f"pad{i}={chunk}"]
		return out

	def test_plain_mode_diagnostic(self, tmp_path) -> None:
		from lang.codegen.llvm.test_utils import sanitizer_timeout
		res, _ = _run_driftc(tmp_path, self._oversize_args(),
		                     sanitizer_timeout(120))
		assert res.returncode == 1
		assert "exceeds the section cap" in res.stderr
		assert "Traceback" not in res.stderr

	def test_json_mode_diagnostic(self, tmp_path) -> None:
		from lang.codegen.llvm.test_utils import sanitizer_timeout
		res, _ = _run_driftc(tmp_path, self._oversize_args() + ["--json"],
		                     sanitizer_timeout(120))
		assert res.returncode == 1
		assert "Traceback" not in res.stderr
		payload = json.loads(res.stdout)
		assert payload["exit_code"] == 1
		assert any("exceeds the section cap" in d["message"]
		           for d in payload["diagnostics"])


class TestSchemaStrictness:
	"""Documents the compiler cannot produce are rejected even when
	otherwise canonical (review round 3)."""

	def _canon(self, doc) -> bytes:
		return canonical_json(doc).encode("utf-8")

	def test_duplicate_dependency_names_rejected(self) -> None:
		doc = json.loads(_assemble())
		doc["dependencies"] = [{"name": "a", "version": "1.0.0"},
		                       {"name": "a", "version": "2.0.0"}]
		with pytest.raises(BuildInfoError, match="duplicate dependency"):
			validate_build_info_payload(self._canon(doc))

	def test_extra_key_grammar_enforced(self) -> None:
		doc = json.loads(_assemble())
		doc["extra"] = {"Bad-Key": "v"}
		with pytest.raises(BuildInfoError, match=r"\[a-z0-9_.\]"):
			validate_build_info_payload(self._canon(doc))

	def test_deep_nesting_is_a_diagnostic_not_recursionerror(self) -> None:
		hostile = (b"[" * 200000) + (b"]" * 200000)
		assert len(hostile) < BUILD_INFO_MAX_PAYLOAD
		with pytest.raises(BuildInfoError):
			validate_build_info_payload(hostile)

	def test_toolchain_identity_floors_in_build_info(self) -> None:
		"""P2 round-4: empty driftc / non-positive abi rejected in the
		build-info document too."""
		doc = json.loads(_assemble())
		doc["toolchain"]["driftc"] = ""
		with pytest.raises(BuildInfoError, match="non-empty version"):
			validate_build_info_payload(self._canon(doc))
		doc = json.loads(_assemble())
		doc["toolchain"]["abi"] = -1
		with pytest.raises(BuildInfoError, match="must be positive"):
			validate_build_info_payload(self._canon(doc))

	def test_lone_surrogate_encode_is_a_diagnostic(self) -> None:
		with pytest.raises(BuildInfoError, match="not encodable"):
			encode_build_info('{"x":"\udcff"}')


class TestHostileArgvBytesCli:
	def test_invalid_utf8_meta_value_no_traceback(self, tmp_path) -> None:
		"""Raw invalid-UTF-8 argv bytes surface as a normal diagnostic
		(surrogateescape round-trip must not traceback at encode)."""
		from lang.codegen.llvm.test_utils import sanitizer_timeout
		src = tmp_path / "main.drift"
		src.write_text(_PROG)
		out_ll = tmp_path / "out.ll"
		cmd = [sys.executable.encode(), b"-m", b"lang.driftc.driftc",
		       str(src).encode(), b"--emit-ir", str(out_ll).encode(),
		       b"--meta", b"k=\xff\xfe"]
		res = subprocess.run(cmd, capture_output=True, cwd=ROOT,
		                     timeout=sanitizer_timeout(120))
		assert res.returncode == 1, res.stderr[-400:]
		assert b"Traceback" not in res.stderr
		assert b"not encodable" in res.stderr or b"UTF-8" in res.stderr


class TestDepPinsReachStamp:
	"""G1 end to end (review round 3, pulled forward from W3): a REAL
	package consume — emit a .dmp, consume it via --package-root/--dep,
	and the VALIDATED pin appears in the emitted stamp."""

	def test_package_consume_stamps_validated_pin(self, tmp_path) -> None:
		from lang.codegen.llvm.test_utils import sanitizer_timeout
		lib_dir = tmp_path / "lib"
		lib_dir.mkdir()
		(lib_dir / "stamplib.drift").write_text(
			"module stamplib;\n"
			"export { answer };\n"
			"pub fn answer() nothrow -> Int { return 41; }\n")
		dmp = tmp_path / "stamplib.dmp"
		res = subprocess.run(
			[sys.executable, "-m", "lang.driftc.driftc",
			 "--target-word-bits", "64",
			 "-M", str(tmp_path), str(lib_dir / "stamplib.drift"),
			 "--emit-package", str(dmp), "--package-id", "stamplib",
			 "--package-version", "0.1.0", "--package-target", "test-target"],
			cwd=ROOT, capture_output=True, text=True,
			timeout=sanitizer_timeout(150))
		assert res.returncode == 0 and dmp.exists(), res.stderr[-800:]

		(tmp_path / "main.drift").write_text(
			"module main;\n"
			"import stamplib as stamplib;\n"
			"pub fn main() nothrow -> Int { return stamplib.answer(); }\n")
		out_ll = tmp_path / "consumer.ll"
		res = subprocess.run(
			[sys.executable, "-m", "lang.driftc.driftc",
			 "--target-word-bits", "64",
			 "-M", str(tmp_path), "--package-root", str(tmp_path),
			 "--dep", "stamplib@0.1.0",
			 "--allow-unsigned-from", str(tmp_path),
			 str(tmp_path / "main.drift"), "--entry", "main::main",
			 "--emit-ir", str(out_ll)],
			cwd=ROOT, capture_output=True, text=True,
			timeout=sanitizer_timeout(150))
		assert res.returncode == 0, res.stderr[-800:]
		doc = json.loads(_extract_ir_stamp(out_ll))
		assert doc["dependencies"] == [
			{"name": "stamplib", "version": "0.1.0"}
		], "the validated --dep pin must appear in the emitted stamp"


class TestRunLevelAccessors:
	"""W2 coverage bar: full compile/link/RUN for the std.meta surface
	(the e2e in-process runner cannot pass stamp flags, so stamped-run
	coverage lives here against the real driftc CLI)."""

	_SHOW_PROG = (
		"import std.meta as meta;\n"
		"import std.console as cons;\n\n"
		"fn show(v: Optional<String>) nothrow -> Void {\n"
		"\tmatch v {\n"
		"\t\tOptional::Some(s) => { cons.println(s); },\n"
		"\t\tOptional::None() => { cons.println(\"<none>\"); }\n"
		"\t}\n"
		"}\n\n"
		"pub fn main() nothrow -> Int {\n"
		"\tshow(meta.artifact_name());\n"
		"\tshow(meta.artifact_version());\n"
		"\tshow(meta.artifact_description());\n"
		"\tshow(meta.artifact_license());\n"
		"\tcons.println(meta.toolchain_version());\n"
		"\tif meta.runtime_abi() <= 0 { return 1; }\n"
		"\treturn 0;\n"
		"}\n")

	def _compile_and_run(self, tmp_path: Path, flags: list[str]):
		from lang.codegen.llvm.test_utils import sanitizer_timeout
		src = tmp_path / "main.drift"
		src.write_text(self._SHOW_PROG)
		out = tmp_path / "app"
		cc = subprocess.run(
			[sys.executable, "-m", "lang.driftc.driftc", str(src),
			 "-o", str(out)] + flags,
			capture_output=True, text=True, cwd=ROOT,
			timeout=sanitizer_timeout(180))
		assert cc.returncode == 0, cc.stderr[-800:]
		run = subprocess.run([str(out)], capture_output=True, text=True,
		                     timeout=sanitizer_timeout(30))
		return run

	def test_stamped_binary_run(self, tmp_path) -> None:
		from lang.versions import DRIFTC_VERSION
		run = self._compile_and_run(tmp_path, [
			"--artifact-name", "runapp", "--artifact-version", "9.9.9",
			"--artifact-description", "hostile |,@ desc",
			"--artifact-license", "MIT", "--meta", "k=v",
		])
		assert run.returncode == 0, run.stderr[-400:]
		assert run.stdout == (
			f"runapp\n9.9.9\nhostile |,@ desc\nMIT\n{DRIFTC_VERSION}\n")

	def test_unstamped_binary_run(self, tmp_path) -> None:
		from lang.versions import DRIFTC_VERSION
		run = self._compile_and_run(tmp_path, [])
		assert run.returncode == 0, run.stderr[-400:]
		assert run.stdout == (
			f"<none>\n<none>\n<none>\n<none>\n{DRIFTC_VERSION}\n")


class TestDepVersionsRun:
	"""Dependency records at RUN level: a real package consume whose
	binary parses meta.build_info() for the dependency."""

	def test_consumed_pin_visible_at_runtime(self, tmp_path) -> None:
		from lang.codegen.llvm.test_utils import sanitizer_timeout
		lib_dir = tmp_path / "lib"
		lib_dir.mkdir()
		(lib_dir / "runlib.drift").write_text(
			"module runlib;\n"
			"export { answer };\n"
			"pub fn answer() nothrow -> Int { return 0; }\n")
		dmp = tmp_path / "runlib.dmp"
		res = subprocess.run(
			[sys.executable, "-m", "lang.driftc.driftc",
			 "--target-word-bits", "64",
			 "-M", str(tmp_path), str(lib_dir / "runlib.drift"),
			 "--emit-package", str(dmp), "--package-id", "runlib",
			 "--package-version", "0.2.5", "--package-target", "test-target"],
			cwd=ROOT, capture_output=True, text=True,
			timeout=sanitizer_timeout(150))
		assert res.returncode == 0 and dmp.exists(), res.stderr[-800:]
		# std.meta no longer typed-parses deps (that std.json edge was
		# removed); the consumer parses the raw build_info() document,
		# the same layered pattern std.cli / examples/build_info use.
		(tmp_path / "main.drift").write_text(
			"module main;\n"
			"import runlib as runlib;\n"
			"import std.meta as meta;\n"
			"import std.json as json;\n"
			"import std.core as core;\n"
			"import std.console as cons;\n"
			"pub fn main() nothrow -> Int {\n"
			"\tmatch json.parse(meta.build_info()) {\n"
			"\t\tcore.Result::Ok(root) => {\n"
			"\t\t\tmatch root.get(\"dependencies\") {\n"
			"\t\t\t\tOptional::Some(dn) => {\n"
			"\t\t\t\t\tmatch dn.as_array() {\n"
			"\t\t\t\t\t\tOptional::Some(es) => {\n"
			"\t\t\t\t\t\t\tif es.len != 1 { return 1; }\n"
			"\t\t\t\t\t\t\tmatch es[0].get(\"name\") {\n"
			"\t\t\t\t\t\t\t\tOptional::Some(nn) => { match nn.as_string() { Optional::Some(s) => { cons.println(s); }, Optional::None() => { return 2; } } },\n"
			"\t\t\t\t\t\t\t\tOptional::None() => { return 3; }\n"
			"\t\t\t\t\t\t\t}\n"
			"\t\t\t\t\t\t\tmatch es[0].get(\"version\") {\n"
			"\t\t\t\t\t\t\t\tOptional::Some(vn) => { match vn.as_string() { Optional::Some(s) => { cons.println(s); }, Optional::None() => { return 4; } } },\n"
			"\t\t\t\t\t\t\t\tOptional::None() => { return 5; }\n"
			"\t\t\t\t\t\t\t}\n"
			"\t\t\t\t\t\t},\n"
			"\t\t\t\t\t\tOptional::None() => { return 6; }\n"
			"\t\t\t\t\t}\n"
			"\t\t\t\t},\n"
			"\t\t\t\tOptional::None() => { return 7; }\n"
			"\t\t\t}\n"
			"\t\t},\n"
			"\t\tcore.Result::Err(_) => { return 8; }\n"
			"\t}\n"
			"\treturn runlib.answer();\n"
			"}\n")
		out = tmp_path / "consumer"
		res = subprocess.run(
			[sys.executable, "-m", "lang.driftc.driftc",
			 "--target-word-bits", "64",
			 "-M", str(tmp_path), "--package-root", str(tmp_path),
			 "--dep", "runlib@0.2.5",
			 "--allow-unsigned-from", str(tmp_path),
			 str(tmp_path / "main.drift"), "--entry", "main::main",
			 "-o", str(out)],
			cwd=ROOT, capture_output=True, text=True,
			timeout=sanitizer_timeout(150))
		assert res.returncode == 0, res.stderr[-800:]
		run = subprocess.run([str(out)], capture_output=True, text=True,
		                     timeout=sanitizer_timeout(30))
		assert run.returncode == 0, run.stderr[-400:]
		assert run.stdout == "runlib\n0.2.5\n"


class TestIntrinsicTotality:
	"""Every @intrinsic std.meta declares has lowering support, and the
	retired compiler_info arm is gone — new intrinsics cannot silently
	land without codegen, and the pipe arm cannot silently return."""

	def test_every_meta_intrinsic_has_a_lowering_arm(self) -> None:
		meta_src = (ROOT / "stdlib" / "std" / "meta" / "meta.drift").read_text()
		declared = re.findall(r"@intrinsic\s+(?:pub\s+)?fn\s+([a-zA-Z0-9_]+)\s*\(", meta_src)
		assert declared, "no @intrinsic declarations found in std.meta"
		codegen_src = (ROOT / "lang" / "codegen" / "llvm" / "llvm_codegen.py").read_text()
		for name in declared:
			assert (f'fn_id.name == "{name}"' in codegen_src
			        or f'"{name}":' in codegen_src), (
				f"std.meta intrinsic {name!r} has no lowering arm in "
				f"llvm_codegen.py")

	def test_retired_compiler_info_arm_is_gone(self) -> None:
		codegen_src = (ROOT / "lang" / "codegen" / "llvm" / "llvm_codegen.py").read_text()
		assert 'fn_id.name == "compiler_info"' not in codegen_src
		assert "emit_compiler_provenance" not in codegen_src
		assert "__drift_compiler_build" not in codegen_src
		meta_src = (ROOT / "stdlib" / "std" / "meta" / "meta.drift").read_text()
		assert "compiler_info" not in meta_src
		assert "CompilerTag" not in meta_src


class TestRawIntrinsicRuntime:
	"""P1 (W2 review round 2): the RAW build_info() intrinsic — which
	has its own lowering arm, so accessor coverage proves nothing about
	it — must run in stamped AND unstamped binaries, and the stamped
	runtime string must equal the externally extracted section
	byte-for-byte."""

	_PRINT_PROG = (
		"import std.meta as meta;\n"
		"import std.console as cons;\n\n"
		"pub fn main() nothrow -> Int {\n"
		"\tcons.println(meta.build_info());\n"
		"\treturn 0;\n"
		"}\n")

	def _build_and_run(self, tmp_path: Path, flags: list[str]):
		from lang.codegen.llvm.test_utils import sanitizer_timeout
		src = tmp_path / "main.drift"
		src.write_text(self._PRINT_PROG)
		out = tmp_path / "app"
		cc = subprocess.run(
			[sys.executable, "-m", "lang.driftc.driftc", str(src),
			 "-o", str(out)] + flags,
			capture_output=True, text=True, cwd=ROOT,
			timeout=sanitizer_timeout(180))
		assert cc.returncode == 0, cc.stderr[-800:]
		run = subprocess.run([str(out)], capture_output=True, text=True,
		                     timeout=sanitizer_timeout(30))
		assert run.returncode == 0, run.stderr[-400:]
		return out, run.stdout

	def test_stamped_runtime_matches_extracted_section(self, tmp_path) -> None:
		out, stdout = self._build_and_run(tmp_path, [
			"--artifact-name", "rawapp", "--artifact-version", "3.2.1",
			"--artifact-description", "raw |,@ desc",
			"--artifact-license", "MIT",
			"--meta", "team=raw", "--meta", "empty=",
		])
		runtime_doc = stdout.rstrip("\n")
		# The runtime string is the validated canonical document...
		assert validate_build_info_payload(runtime_doc.encode("utf-8")) == runtime_doc
		# ...and is byte-identical to the SHARED PRODUCTION reader's
		# extraction from the SAME binary (exactly-one-section enforced
		# inside read_build_info_section).
		from lang.build_info import extract_build_info
		assert extract_build_info(out) == runtime_doc
		doc = json.loads(runtime_doc)
		assert doc["artifact"] == {
			"name": "rawapp", "version": "3.2.1",
			"description": "raw |,@ desc", "license": "MIT"}
		assert doc["extra"] == {"team": "raw", "empty": ""}

	def test_unstamped_runtime_document(self, tmp_path) -> None:
		out, stdout = self._build_and_run(tmp_path, [])
		runtime_doc = stdout.rstrip("\n")
		assert validate_build_info_payload(runtime_doc.encode("utf-8")) == runtime_doc
		from lang.build_info import extract_build_info
		assert extract_build_info(out) == runtime_doc
		doc = json.loads(runtime_doc)
		assert doc["artifact"] is None and doc["extra"] == {}
