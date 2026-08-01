# drift-build-info/v1 — document assembly, validation, and the
# executable-section contract.
#
# Single source of truth for the build-info stamp contract
# (work/toolchain-meta-stamps/PLAN.md §2.1/§2.4). Both halves of the
# toolchain import from here: the EMITTER (llvm_codegen.emit_build_info)
# and the READER (`drift inspect build-info`, which parses the binary
# format directly — never readelf/objdump, never executing the target).
#
# ── Document contract (drift-build-info/v1) ──────────────────────────
# One canonical JSON object:
#   format        "drift-build-info/v1" (mandatory discriminator)
#   toolchain     TOOLCHAIN IDENTITY — facts true of the toolchain
#                 independent of any compile: driftc, abi, git, vendor,
#                 license. Compiler-generated; never caller-settable.
#   build         BUILD-INSTANCE facts that exist only during codegen:
#                 word, profile, utc. Compiler-generated.
#   artifact      Manifest identity allowlist (name, version,
#                 description, license) — atomic: all four or the whole
#                 section is JSON null. Values are caller-supplied via
#                 the dedicated `--artifact-*` driftc flags.
#   dependencies  [{name, version}...] sorted by name — derived from
#                 the VALIDATED effective --dep pin map, never raw
#                 argv. Versions are compiler-accepted non-empty exact
#                 identity strings (no semver claim).
#   extra         Generic user metadata from `--meta key=value`; string
#                 values (empty allowed); structurally isolated so it
#                 can never collide with authoritative sections.
# Every key in every present section is REQUIRED; an unavailable fact
# is "" — never null, never omitted. `abi` and `word` are JSON numbers.
#
# Canonicalization: the repository JSON convention — UTF-8,
# sort_keys=True, ensure_ascii=False, compact separators, no trailing
# newline.
#
# ── Executable section contract (`.drift_build_info`) ───────────────
# The section contains EXACTLY the canonical UTF-8 JSON document — no
# magic, no framing version, no length prefix, no NUL terminator. The
# executable's section header already supplies identity, offset, and
# exact byte length; the JSON `format` discriminator supplies the
# schema version. Emitter cap: BUILD_INFO_MAX_PAYLOAD bytes.
#
# Reader (`drift inspect build-info`) requirements — fail-closed, the
# gate-facing contract:
#   * exactly ONE `.drift_build_info` section in the binary;
#   * the 1 MiB limit is enforced BEFORE decoding;
#   * UTF-8, JSON syntax, the complete v1 schema (required keys/types
#     per section), and CANONICAL encoding are all validated — the
#     re-serialization comparison also rejects duplicate keys and any
#     trailing/leading junk;
#   * missing, duplicate, empty, oversized, malformed, noncanonical,
#     or trailing-byte content → exit 1, EMPTY stdout, stderr
#     diagnostic;
#   * the inspected binary is NEVER executed;
#   * `--json` success output is the section's exact canonical bytes
#     plus one CLI newline.

from __future__ import annotations

import json


class BuildInfoError(ValueError):
	"""Build-info input/contract violation.

	Raised by the assembly/validation helpers. The CLI boundary
	catches THIS type specifically and surfaces a normal diagnostic
	(plain and --json) — an oversized `--meta` payload must never
	escape codegen as a traceback."""
	pass


BUILD_INFO_FORMAT = "drift-build-info/v1"
TOOLCHAIN_INFO_FORMAT = "drift-toolchain-info/v1"
BUILD_INFO_SECTION = ".drift_build_info"
BUILD_INFO_SYMBOL = "__drift_build_info"
BUILD_INFO_MAX_PAYLOAD = 1 << 20  # 1 MiB


def canonical_json(obj) -> str:
	"""The repository JSON convention: sort_keys, ensure_ascii=False,
	compact separators, no trailing newline."""
	return json.dumps(obj, sort_keys=True, ensure_ascii=False,
	                  separators=(",", ":"))


def toolchain_identity_section() -> dict:
	"""The `toolchain` section — identity facts independent of any
	compile. Unavailable facts are \"\" (never null/omitted)."""
	from lang.versions import (
		DRIFTC_VERSION, DRIFT_RT_ABI_VERSION,
		DRIFTC_VENDOR, DRIFTC_LICENSE, DRIFTC_GIT_SHA,
	)
	return {
		"driftc": DRIFTC_VERSION,
		"abi": DRIFT_RT_ABI_VERSION,
		"git": DRIFTC_GIT_SHA or "",
		"vendor": DRIFTC_VENDOR or "",
		"license": DRIFTC_LICENSE or "",
	}


def toolchain_info_json(git_sha: str = "") -> str:
	"""Canonical drift-toolchain-info/v1 document — the `--version
	--json` output of BOTH `driftc` and `drift` (truthful without a
	build instance: toolchain identity only, no build/artifact
	sections). `git_sha` overrides the stamp-derived git field when
	non-empty (the CLIs resolve a runtime git probe in dev trees)."""
	toolchain = toolchain_identity_section()
	if git_sha:
		toolchain["git"] = git_sha
	return canonical_json({
		"format": TOOLCHAIN_INFO_FORMAT,
		"toolchain": toolchain,
	})


def parse_toolchain_info(text: str) -> dict:
	"""Fail-closed machine-consumer parse of `--version --json` output.

	Validates the exactly-one-trailing-newline CLI contract, JSON
	syntax, the discriminator, exact keys and types of the toolchain
	section (non-empty driftc version, positive ABI), and canonical
	encoding. Returns the toolchain dict. Raises BuildInfoError —
	machine consumers must treat that as a hard failure, never fall
	back to defaults."""
	if not text.endswith("\n") or text.endswith("\n\n"):
		raise BuildInfoError(
			"toolchain-info output must be the canonical document "
			"followed by exactly one newline")
	text = text[:-1]
	try:
		doc = json.loads(text)
	except json.JSONDecodeError as e:
		raise BuildInfoError(f"toolchain-info output is not valid JSON: {e}")
	except RecursionError:
		raise BuildInfoError(
			"toolchain-info output exceeds the JSON nesting depth limit")
	if not isinstance(doc, dict) or set(doc.keys()) != {"format", "toolchain"}:
		raise BuildInfoError(
			"toolchain-info document must have exactly the keys "
			"{format, toolchain}")
	if doc["format"] != TOOLCHAIN_INFO_FORMAT:
		raise BuildInfoError(
			f"toolchain-info discriminator {doc['format']!r} != "
			f"{TOOLCHAIN_INFO_FORMAT!r}")
	tc = doc["toolchain"]
	if not isinstance(tc, dict) or set(tc.keys()) != set(_TOOLCHAIN_KEYS.keys()):
		raise BuildInfoError(
			f"toolchain section keys must be exactly "
			f"{sorted(_TOOLCHAIN_KEYS.keys())}")
	for k, ty in _TOOLCHAIN_KEYS.items():
		if not isinstance(tc[k], ty) or isinstance(tc[k], bool):
			raise BuildInfoError(f"toolchain.{k} must be {ty.__name__}")
	_check_toolchain_identity_floors(tc, BuildInfoError)
	if canonical_json(doc) != text:
		raise BuildInfoError(
			"toolchain-info output is not canonically encoded")
	return tc


def _check_toolchain_identity_floors(tc: dict, err) -> None:
	"""The "real identity or stop" contract: a toolchain section with
	an empty compiler version or a non-positive ABI is not an
	identity."""
	if not tc["driftc"]:
		raise err("toolchain.driftc must be a non-empty version string")
	if tc["abi"] <= 0:
		raise err(f"toolchain.abi must be positive, got {tc['abi']}")


def assemble_build_info(
	*,
	git_sha: str,
	word_bits: int,
	build_profile: str,
	build_utc: str,
	artifact: dict | None,
	dependencies: dict[str, str],
	extra: dict[str, str],
) -> str:
	"""Assemble the canonical drift-build-info/v1 document.

	`git_sha` overrides the identity section's git field when non-empty
	(the CLI resolves a runtime git probe when the build stamp is
	absent; both callers of the provenance channel pass the resolved
	value). `dependencies` is the VALIDATED pin map {name: version}.
	`artifact` is either a complete 4-key dict or None (atomicity is
	the CLI's contract; this function trusts its caller and asserts).
	"""
	toolchain = toolchain_identity_section()
	if git_sha:
		toolchain["git"] = git_sha
	if artifact is not None:
		assert set(artifact.keys()) == {"name", "version", "description", "license"}, (
			"artifact section must be atomic (exactly name/version/description/license)"
		)
		assert all(isinstance(v, str) and v for v in artifact.values()), (
			"artifact values must be non-empty strings"
		)
	doc = {
		"format": BUILD_INFO_FORMAT,
		"toolchain": toolchain,
		"build": {
			"word": int(word_bits),
			"profile": build_profile or "",
			"utc": build_utc or "",
		},
		"artifact": dict(artifact) if artifact is not None else None,
		"dependencies": [
			{"name": name, "version": version}
			for name, version in sorted(dependencies.items())
		],
		"extra": {str(k): str(v) for k, v in extra.items()},
	}
	return canonical_json(doc)


def encode_build_info(payload: str) -> bytes:
	"""Encode the canonical document for section emission.

	Hostile argv bytes reach Python as lone surrogates
	(surrogateescape); a bare .encode would traceback. Wrap as the
	normal BuildInfoError diagnostic, then enforce the size cap."""
	try:
		raw = payload.encode("utf-8")
	except UnicodeEncodeError as e:
		raise BuildInfoError(
			f"build-info payload is not encodable as UTF-8 ({e}); "
			f"check --meta / --artifact-* values for invalid bytes"
		)
	return check_payload_size(raw)


def check_payload_size(payload: bytes) -> bytes:
	"""Emitter-side cap enforcement (the reader independently enforces
	the same cap before decoding)."""
	if not payload:
		raise BuildInfoError("build-info payload must not be empty")
	if len(payload) > BUILD_INFO_MAX_PAYLOAD:
		raise BuildInfoError(
			f"build-info payload {len(payload)} bytes exceeds the "
			f"section cap of {BUILD_INFO_MAX_PAYLOAD} bytes "
			f"(shrink --meta values)"
		)
	return payload


_TOOLCHAIN_KEYS = {"driftc": str, "abi": int, "git": str,
                   "vendor": str, "license": str}
_BUILD_KEYS = {"word": int, "profile": str, "utc": str}
_ARTIFACT_KEYS = ("name", "version", "description", "license")


def validate_build_info_doc(doc) -> None:
	"""Full drift-build-info/v1 schema validation (fail-closed).

	The reader contract: required keys with exact types in every
	section; artifact is null or the complete non-empty 4-key
	identity; dependencies is a name-sorted array of {name, version}
	non-empty-string records; extra maps string keys to string
	values. Raises BuildInfoError."""
	def bad(msg: str) -> None:
		raise BuildInfoError(f"build-info document invalid: {msg}")
	if not isinstance(doc, dict):
		bad(f"top level must be an object, got {type(doc).__name__}")
	expected_top = {"format", "toolchain", "build", "artifact",
	                "dependencies", "extra"}
	if set(doc.keys()) != expected_top:
		bad(f"top-level keys {sorted(doc.keys())} != {sorted(expected_top)}")
	if doc["format"] != BUILD_INFO_FORMAT:
		bad(f"format discriminator {doc['format']!r} != {BUILD_INFO_FORMAT!r}")
	for section, spec in (("toolchain", _TOOLCHAIN_KEYS), ("build", _BUILD_KEYS)):
		val = doc[section]
		if not isinstance(val, dict) or set(val.keys()) != set(spec.keys()):
			bad(f"{section} keys must be exactly {sorted(spec.keys())}")
		for k, ty in spec.items():
			if not isinstance(val[k], ty) or isinstance(val[k], bool):
				bad(f"{section}.{k} must be {ty.__name__}")
	_check_toolchain_identity_floors(
		doc["toolchain"],
		lambda m: BuildInfoError(f"build-info document invalid: {m}"))
	art = doc["artifact"]
	if art is not None:
		if not isinstance(art, dict) or set(art.keys()) != set(_ARTIFACT_KEYS):
			bad(f"artifact must be null or exactly keys {sorted(_ARTIFACT_KEYS)}")
		for k in _ARTIFACT_KEYS:
			if not isinstance(art[k], str) or not art[k]:
				bad(f"artifact.{k} must be a non-empty string")
	deps = doc["dependencies"]
	if not isinstance(deps, list):
		bad("dependencies must be an array")
	names = []
	for i, d in enumerate(deps):
		if (not isinstance(d, dict) or set(d.keys()) != {"name", "version"}
				or not isinstance(d.get("name"), str) or not d["name"]
				or not isinstance(d.get("version"), str) or not d["version"]):
			bad(f"dependencies[{i}] must be {{name, version}} with "
			    f"non-empty strings")
		names.append(d["name"])
	if names != sorted(names):
		bad("dependencies must be sorted by name")
	if len(set(names)) != len(names):
		from collections import Counter
		dupes = sorted(n for n, c in Counter(names).items() if c > 1)
		bad(f"duplicate dependency name(s): {', '.join(dupes)}")
	extra = doc["extra"]
	if not isinstance(extra, dict) or not all(
			isinstance(k, str) and isinstance(v, str)
			for k, v in extra.items()):
		bad("extra must map string keys to string values")
	import re as _re
	for k in extra:
		if not _re.fullmatch(r"[a-z0-9_.]+", k):
			bad(f"extra key {k!r} must match [a-z0-9_.]+ "
			    f"(the --meta key grammar — the compiler cannot "
			    f"produce any other key)")


def validate_build_info_payload(blob: bytes) -> str:
	"""Validate raw `.drift_build_info` section content → canonical text.

	`blob` is the COMPLETE section content (the reader slices it from
	the section header's exact size). Fail-closed, in order: size cap
	BEFORE decoding, non-empty, UTF-8, JSON syntax, the complete v1
	schema, and canonical encoding (the re-serialization comparison
	rejects duplicate keys, whitespace, reordering, and any trailing
	or leading bytes). Raises BuildInfoError; callers map that to
	exit 1 + empty stdout + a stderr diagnostic.
	"""
	if len(blob) > BUILD_INFO_MAX_PAYLOAD:
		raise BuildInfoError(
			f"build-info section {len(blob)} bytes exceeds the cap of "
			f"{BUILD_INFO_MAX_PAYLOAD} bytes"
		)
	if not blob:
		raise BuildInfoError("build-info section is empty")
	try:
		text = blob.decode("utf-8")
	except UnicodeDecodeError as e:
		raise BuildInfoError(f"build-info payload is not valid UTF-8: {e}")
	try:
		doc = json.loads(text)
	except json.JSONDecodeError as e:
		raise BuildInfoError(f"build-info payload is not valid JSON: {e}")
	except RecursionError:
		raise BuildInfoError(
			"build-info payload exceeds the JSON nesting depth limit"
		)
	validate_build_info_doc(doc)
	if canonical_json(doc) != text:
		raise BuildInfoError(
			"build-info payload is not canonically encoded (key order, "
			"separators, escapes, duplicate keys, or surrounding bytes "
			"deviate from the exact canonical document)"
		)
	return text


def read_build_info_section(path) -> bytes:
	"""Read the raw `.drift_build_info` section from an executable.

	SELF-CONTAINED (guardrail G2): parses the ELF container directly —
	never readelf/objdump, and the target is NEVER executed; this is
	pure file reading. BOUNDED reads throughout: the whole file is
	never loaded (a huge or sparse hostile binary cannot exhaust
	memory), every table/string/content read is size-capped and
	verified against the file size, and the 1 MiB payload cap is
	enforced from the section HEADER before any payload byte is
	copied. Fail-closed on every malformed shape: not a regular file,
	not ELF, unsupported class/endianness/version, truncated or
	out-of-bounds section table, wrong section types
	(string table must be SHT_STRTAB, the stamp must be SHT_PROGBITS,
	never compressed), MISSING section, and DUPLICATE sections
	(exactly one `.drift_build_info` is the contract). Raises
	BuildInfoError only.
	"""
	import os
	import struct
	from pathlib import Path as _Path
	p = _Path(path)
	if not p.is_file():
		raise BuildInfoError(f"not a regular file: {p}")

	_SHT_PROGBITS, _SHT_STRTAB = 1, 3
	_SHF_COMPRESSED = 0x800
	_MAX_SHENTSIZE = 1024          # sanity cap; real ELF64 uses 0x40
	_MAX_SHSTR = 1 << 20           # section-NAME table cap (names are tiny)

	try:
		f = open(p, "rb")
	except OSError as e:
		raise BuildInfoError(f"cannot read {p}: {e}")
	try:
		try:
			file_size = os.fstat(f.fileno()).st_size
		except OSError as e:
			raise BuildInfoError(f"cannot stat {p}: {e}")

		def _read_at(off: int, n: int, what: str) -> bytes:
			if off < 0 or n < 0 or off + n > file_size:
				raise BuildInfoError(f"{p}: {what} out of bounds")
			try:
				f.seek(off)
				buf = f.read(n)
			except OSError as e:
				raise BuildInfoError(f"cannot read {p}: {e}")
			if len(buf) != n:
				raise BuildInfoError(f"{p}: truncated read for {what}")
			return buf

		if file_size < 0x40:
			raise BuildInfoError(f"{p}: too small to be an ELF binary")
		hdr = _read_at(0, 0x40, "ELF header")
		if hdr[:4] != b"\x7fELF":
			raise BuildInfoError(f"{p}: not an ELF binary")
		if hdr[4] != 2:
			raise BuildInfoError(f"{p}: unsupported ELF class {hdr[4]} (reader supports ELF64)")
		if hdr[5] != 1:
			raise BuildInfoError(f"{p}: unsupported ELF byte order {hdr[5]} (reader supports little-endian)")
		if hdr[6] != 1:
			raise BuildInfoError(f"{p}: unsupported ELF identification version {hdr[6]}")
		e_shoff, = struct.unpack_from("<Q", hdr, 0x28)
		e_ehsize, = struct.unpack_from("<H", hdr, 0x34)
		e_shentsize, = struct.unpack_from("<H", hdr, 0x3A)
		e_shnum, = struct.unpack_from("<H", hdr, 0x3C)
		e_shstrndx, = struct.unpack_from("<H", hdr, 0x3E)
		if e_ehsize < 0x40:
			raise BuildInfoError(f"{p}: malformed ELF header size {e_ehsize}")
		if (e_shentsize < 0x40 or e_shentsize > _MAX_SHENTSIZE
				or e_shnum == 0 or e_shstrndx >= e_shnum):
			raise BuildInfoError(f"{p}: malformed ELF section table header")
		if e_shoff == 0 or e_shoff + e_shnum * e_shentsize > file_size:
			raise BuildInfoError(f"{p}: section table out of bounds")
		# One bounded read for the whole table (u16 count × capped
		# entry size ≤ 64 MiB worst case; real tables are tiny).
		table = _read_at(e_shoff, e_shnum * e_shentsize, "section table")

		def _header(i: int) -> tuple[int, int, int, int, int]:
			off = i * e_shentsize
			sh_name, sh_type = struct.unpack_from("<II", table, off)
			sh_flags, = struct.unpack_from("<Q", table, off + 0x08)
			sh_offset, = struct.unpack_from("<Q", table, off + 0x18)
			sh_size, = struct.unpack_from("<Q", table, off + 0x20)
			return sh_name, sh_type, sh_flags, sh_offset, sh_size

		_, str_type, _, str_off, str_size = _header(e_shstrndx)
		if str_type != _SHT_STRTAB:
			raise BuildInfoError(
				f"{p}: section string table has type {str_type}, expected "
				f"SHT_STRTAB")
		if str_size > _MAX_SHSTR:
			raise BuildInfoError(
				f"{p}: section string table {str_size} bytes exceeds the "
				f"{_MAX_SHSTR}-byte cap")
		if str_off + str_size > file_size:
			raise BuildInfoError(f"{p}: string table out of bounds")
		shstr = _read_at(str_off, str_size, "section string table")

		found: list[tuple[int, int]] = []
		for i in range(e_shnum):
			sh_name, sh_type, sh_flags, sh_offset, sh_size = _header(i)
			if sh_name >= len(shstr):
				raise BuildInfoError(f"{p}: section {i} name offset out of bounds")
			nul = shstr.find(b"\x00", sh_name)
			if nul < 0:
				raise BuildInfoError(f"{p}: unterminated section name for section {i}")
			if shstr[sh_name:nul].decode("utf-8", "replace") != BUILD_INFO_SECTION:
				continue
			# Type discipline: the stamp is emitted as PROGBITS file
			# content. A hostile SHT_NOBITS (or any other type) section
			# merely NAMED .drift_build_info has no real file content
			# at its claimed offset — never serve bytes from one.
			if sh_type != _SHT_PROGBITS:
				raise BuildInfoError(
					f"{p}: {BUILD_INFO_SECTION} section has type {sh_type}, "
					f"expected SHT_PROGBITS")
			if sh_flags & _SHF_COMPRESSED:
				raise BuildInfoError(
					f"{p}: {BUILD_INFO_SECTION} section is compressed "
					f"(SHF_COMPRESSED); the contract is raw canonical JSON")
			# Cap enforced from the HEADER, before any payload copy.
			if sh_size > BUILD_INFO_MAX_PAYLOAD:
				raise BuildInfoError(
					f"{p}: {BUILD_INFO_SECTION} section {sh_size} bytes "
					f"exceeds the cap of {BUILD_INFO_MAX_PAYLOAD} bytes")
			if sh_offset + sh_size > file_size:
				raise BuildInfoError(
					f"{p}: {BUILD_INFO_SECTION} section content out of bounds")
			found.append((sh_offset, sh_size))
		if not found:
			raise BuildInfoError(f"{p}: no {BUILD_INFO_SECTION} section")
		if len(found) > 1:
			raise BuildInfoError(
				f"{p}: {len(found)} {BUILD_INFO_SECTION} sections (exactly "
				f"one is the contract)")
		off, size = found[0]
		return _read_at(off, size, f"{BUILD_INFO_SECTION} content")
	finally:
		f.close()


def extract_build_info(path) -> str:
	"""The production extraction path (`drift inspect build-info`):
	read the single named section, then run the FULL gate-facing
	payload validation (cap before decode, UTF-8, JSON, v1 schema,
	canonical encoding). Raises BuildInfoError."""
	return validate_build_info_payload(read_build_info_section(path))


def parse_meta_flags(pairs: list[str]) -> dict[str, str]:
	"""Validate `--meta key=value` occurrences (PLAN §2.2).

	Key grammar `[a-z0-9_.]+` (non-empty); duplicate key = error; value
	is any string including empty. Raises ValueError with a diagnostic.
	"""
	import re
	out: dict[str, str] = {}
	for spec in pairs:
		if "=" not in spec:
			raise ValueError(f"--meta requires KEY=VALUE format, got: {spec}")
		key, value = spec.split("=", 1)
		if not re.fullmatch(r"[a-z0-9_.]+", key):
			raise ValueError(
				f"--meta key must match [a-z0-9_.]+ (non-empty), got: {key!r}"
			)
		if key in out:
			raise ValueError(f"--meta specified twice for key {key!r}")
		out[key] = value
	return out


def parse_artifact_flags(
	name: str | None,
	version: str | None,
	description: str | None,
	license_: str | None,
) -> dict | None:
	"""Validate the atomic `--artifact-*` set (PLAN §2.1/§2.2).

	All four present with NON-EMPTY values → the artifact dict; all
	four absent → None; anything else (partial set, or any empty
	value) → ValueError. Version is an arbitrary non-empty string —
	no shape validation here by design.
	"""
	values = {
		"name": name, "version": version,
		"description": description, "license": license_,
	}
	given = {k: v for k, v in values.items() if v is not None}
	if not given:
		return None
	missing = sorted(set(values) - set(given))
	if missing:
		raise ValueError(
			f"--artifact-* flags are atomic: all four or none; missing "
			f"--artifact-{', --artifact-'.join(missing)}"
		)
	empty = sorted(k for k, v in given.items() if v == "")
	if empty:
		raise ValueError(
			f"--artifact-* values must be non-empty; empty: "
			f"--artifact-{', --artifact-'.join(empty)}"
		)
	return dict(values)
