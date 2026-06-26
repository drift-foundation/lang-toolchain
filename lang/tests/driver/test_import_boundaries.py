# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Import boundary enforcement tests.

Pinned boundary (enforced by this test):

- `lang.driftc.*` MUST NOT import `lang.drift.*` (package manager layer).
- `lang.drift.*` MUST NOT import `lang.driftc.*` (compiler internals).
- If a shared layer is introduced later (`lang.drift_common.*` / `lang.pkg_common.*`),
  it MUST NOT import either `lang.driftc.*` or `lang.drift.*`.

We enforce this statically using Python AST parsing (not regex) to avoid false
positives from comments/strings and to catch relative imports like `from ... import drift`.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ImportRef:
	path: Path
	lineno: int
	target: str
	raw: str


def _module_path_for_file(py_path: Path) -> list[str]:
	"""
	Map a file path like `lang/driftc/packages/foo.py` to module parts:
	["lang", "driftc", "packages"].
	"""
	parts = list(py_path.parts)
	try:
		i = parts.index("lang")
	except ValueError:
		return []
	mod_parts = parts[i:]
	if mod_parts and mod_parts[-1].endswith(".py"):
		mod_parts = mod_parts[:-1]
	leaf = mod_parts[-1] if mod_parts else ""
	if leaf == "__init__":
		mod_parts = mod_parts[:-1]
	return mod_parts


def _resolve_importfrom(
	*,
	file_module_parts: list[str],
	level: int,
	module: str | None,
	name: str,
) -> str | None:
	"""
	Resolve a `from ...module import name` into a best-effort absolute target.

	This intentionally does not try to emulate every corner of Python import rules;
	it is only used to detect illegal cross-package edges in a stable way.
	"""
	if not file_module_parts:
		return None
	if level < 0:
		level = 0
	# Absolute import: `from pkg.sub import name`.
	# For boundary enforcement we care about the imported module namespace, and we
	# also treat `from lang import drift` as importing `lang.drift`.
	if level == 0:
		if not module:
			return None
		if module == "lang" and name in {"drift", "driftc", "drift_common", "pkg_common", "language_runtime", "compiler_infra"}:
			return f"{module}.{name}"
		return module
	# In Python, level=1 means "from .", level=2 means "from ..", etc.
	up = max(level - 1, 0)
	if up > len(file_module_parts):
		return None
	base = file_module_parts[: len(file_module_parts) - up]

	if module:
		return ".".join(base + module.split("."))
	# `from .. import drift` form: treat imported names as submodules.
	return ".".join(base + [name])


def _collect_imports(py_path: Path) -> list[ImportRef]:
	tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
	file_module_parts = _module_path_for_file(py_path)
	out: list[ImportRef] = []
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			for a in node.names:
				out.append(
					ImportRef(
						path=py_path,
						lineno=getattr(node, "lineno", 1),
						target=a.name,
						raw=f"import {a.name}",
					)
				)
		elif isinstance(node, ast.ImportFrom):
			for a in node.names:
				target = _resolve_importfrom(
					file_module_parts=file_module_parts,
					level=int(getattr(node, "level", 0) or 0),
					module=getattr(node, "module", None),
					name=a.name,
				)
				if target is None:
					continue
				raw_mod = getattr(node, "module", "")
				raw = f"from {'.' * int(getattr(node, 'level', 0) or 0)}{raw_mod} import {a.name}"
				out.append(
					ImportRef(
						path=py_path,
						lineno=getattr(node, "lineno", 1),
						target=target,
						raw=raw,
					)
				)
	return out


def _collect_py_files(root: Path) -> list[Path]:
	if not root.exists():
		return []
	return sorted([p for p in root.rglob("*.py") if p.is_file()])


def test_driftc_does_not_import_drift_layer() -> None:
	# `lang.drift.crypto` is a neutral, dependency-free wrapper around
	# `cryptography.hazmat.primitives.ed25519` + base64.  Both layers
	# need it: the compiler consumes it from `lang.driftc.packages.*_v1`
	# for trust-claim verification, and the package-manager layer
	# consumes it from `lang.drift.{sign,trust,envelope,...}`.  It has
	# zero internal deps on other `lang.drift.*` modules, so it cannot
	# pull the rest of the package-manager layer into the compiler.
	#
	# The architectural ideal is a third package (e.g.
	# `lang.drift_common.crypto`); moving incurs ~20 importer updates
	# across compiler, package-manager, and drift_deploy, and is
	# tracked separately.  Until then this exception is documented and
	# scoped to the crypto module alone.
	allowed_targets = {"lang.drift.crypto"}

	violations: list[ImportRef] = []
	for py_path in _collect_py_files(Path("lang/driftc")):
		for imp in _collect_imports(py_path):
			if imp.target in allowed_targets:
				continue
			if imp.target == "lang.drift" or imp.target.startswith("lang.drift."):
				violations.append(imp)
	assert not violations, "\n".join(
		f"{v.path}:{v.lineno}: forbidden import {v.target!r} ({v.raw})" for v in violations
	)


def test_drift_layer_does_not_import_driftc_internals() -> None:
	# `lang/drift/` is the user-facing CLI layer.  It cannot reach
	# into compiler internals (parser/IR/codegen).
	#
	# Exception list: the v1 trust contract intentionally couples
	# the CLI to a small, stable set of v1 sidecar parsers / naming
	# helpers in `lang.driftc.packages`.  These modules carry the
	# on-disk format of v1 author / cert claims (and the neutral
	# manifest + SCI helper extracted in the deploy/author boundary
	# work) -- explicit integration points, NOT compiler internals,
	# used by `provider_v1` on the consumer side, by
	# `drift-author publish` on the producer side, and by
	# `drift trust bootstrap` / `drift trust check` on the
	# project-preflight side.
	v1_sidecar_format_allow = frozenset({
		"lang.driftc.packages.author_claim_v1",
		"lang.driftc.packages.cert_claim_v1",
		"lang.driftc.packages.sidecar_naming",
		# Neutral manifest parser + Artifact→SCI helper.  Lives in
		# `lang.driftc.packages` so both `tools/drift_deploy` (orch)
		# and `tools/drift_author` (author tool) AND `lang/drift`
		# (CLI / preflight) can share the same definition without
		# crossing the author/deploy boundary.  See
		# `test_author_key_boundary.py` for the symmetric guard.
		"lang.driftc.packages.manifest",
		# NOTE: artifact verification (`drift verify-package` /
		# `drift verify-app`) is NOT in the `lang/drift` CLI layer — it lives
		# in `tools/drift_deploy/verify_{package,app}_cli.py`, which wrap the
		# `verify_deployed_v1` facade.  So `lang.driftc.packages.verify_deployed_v1`
		# is intentionally NOT allowlisted here: the CLI layer must not import
		# the verifier facade directly.
	})
	violations: list[ImportRef] = []
	for py_path in _collect_py_files(Path("lang/drift")):
		for imp in _collect_imports(py_path):
			if imp.target in v1_sidecar_format_allow:
				continue
			if imp.target == "lang.driftc" or imp.target.startswith("lang.driftc."):
				violations.append(imp)
	assert not violations, "\n".join(
		f"{v.path}:{v.lineno}: forbidden import {v.target!r} ({v.raw})" for v in violations
	)


def test_shared_layer_is_dependency_leaf_if_present() -> None:
	# Optional future shared packages.
	shared_roots = [Path("lang/drift_common"), Path("lang/pkg_common")]
	violations: list[ImportRef] = []
	for root in shared_roots:
		for py_path in _collect_py_files(root):
			for imp in _collect_imports(py_path):
				if imp.target.startswith("lang.driftc") or imp.target.startswith("lang.drift"):
					violations.append(imp)
	assert not violations, "\n".join(
		f"{v.path}:{v.lineno}: forbidden import {v.target!r} ({v.raw})" for v in violations
	)
