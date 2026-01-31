# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Import boundary enforcement tests.

Pinned boundary (enforced by this test):

- `lang2.driftc.*` MUST NOT import `lang2.drift.*` (package manager layer).
- `lang2.drift.*` MUST NOT import `lang2.driftc.*` (compiler internals).
- If a shared layer is introduced later (`lang2.drift_common.*` / `lang2.pkg_common.*`),
  it MUST NOT import either `lang2.driftc.*` or `lang2.drift.*`.

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
	Map a file path like `lang2/driftc/packages/foo.py` to module parts:
	["lang2", "driftc", "packages"].
	"""
	parts = list(py_path.parts)
	try:
		i = parts.index("lang2")
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
	# also treat `from lang2 import drift` as importing `lang2.drift`.
	if level == 0:
		if not module:
			return None
		if module == "lang2" and name in {"drift", "driftc", "drift_common", "pkg_common", "language_runtime", "compiler_infra"}:
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
	violations: list[ImportRef] = []
	for py_path in _collect_py_files(Path("lang2/driftc")):
		for imp in _collect_imports(py_path):
			if imp.target == "lang2.drift" or imp.target.startswith("lang2.drift."):
				violations.append(imp)
	assert not violations, "\n".join(
		f"{v.path}:{v.lineno}: forbidden import {v.target!r} ({v.raw})" for v in violations
	)


def test_drift_layer_does_not_import_driftc_internals() -> None:
	# `lang2/drift/` does not exist yet in the repo; once it does, this test
	# starts enforcing the pinned boundary automatically.
	violations: list[ImportRef] = []
	for py_path in _collect_py_files(Path("lang2/drift")):
		for imp in _collect_imports(py_path):
			if imp.target == "lang2.driftc" or imp.target.startswith("lang2.driftc."):
				violations.append(imp)
	assert not violations, "\n".join(
		f"{v.path}:{v.lineno}: forbidden import {v.target!r} ({v.raw})" for v in violations
	)


def test_shared_layer_is_dependency_leaf_if_present() -> None:
	# Optional future shared packages.
	shared_roots = [Path("lang2/drift_common"), Path("lang2/pkg_common")]
	violations: list[ImportRef] = []
	for root in shared_roots:
		for py_path in _collect_py_files(root):
			for imp in _collect_imports(py_path):
				if imp.target.startswith("lang2.driftc") or imp.target.startswith("lang2.drift"):
					violations.append(imp)
	assert not violations, "\n".join(
		f"{v.path}:{v.lineno}: forbidden import {v.target!r} ({v.raw})" for v in violations
	)
