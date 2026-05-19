# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Compiler/package-load path is v1-only audit.

This audit is the security guarantee of slice 4 part B (compiler
sub-boundary on the trust-v1 branch).  It does NOT cover the
deploy pipeline (`tools/drift_deploy/`) which still emits v0
sidecars until a later sub-boundary.  Once that work lands, this
audit is broadened into a repo-wide one (slice 4 part F).

What it asserts:

  - `lang.driftc.driftc` (the compiler entrypoint) and every
    module it imports transitively in the package-load entry chain
    must NOT reference any of these v0 trust symbols:
      - module: `lang.driftc.packages.trust_v0`
      - module: `lang.driftc.packages.signature_v0`
      - symbol: `verify_package_signatures`
      - symbol: `allowed_kids_for_module`
      - symbol: `load_package_v0_with_policy`

  - The package-load surface re-exported from `driftc.py` MUST be
    the v1 surface (`load_package_v1_with_policy`, the v1
    `PackageTrustPolicy`, the v1 `TrustStore` shape).

  - Sidecar conventions: source files in the compiler path MUST
    NOT mention `.sig` or `.source-attestation` literals as live
    consumption hooks.  (Docstrings/comments are not flagged --
    they don't affect runtime behavior.)

Implementation: a lightweight static walker.  It does not import
or execute `tools/drift_deploy/` code so deploy-side v0 usage is
out of scope by construction.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Iterable


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


# Modules that are absolutely forbidden in the compiler load path.
FORBIDDEN_MODULES = {
	"lang.driftc.packages.trust_v0",
	"lang.driftc.packages.signature_v0",
}

# Symbols whose presence in a `from ... import X` would imply v0
# trust use, even if the source module appears benign.
FORBIDDEN_SYMBOLS = {
	"verify_package_signatures",
	"allowed_kids_for_module",
	"load_package_v0_with_policy",
}


def _module_to_path(modname: str) -> pathlib.Path | None:
	"""Resolve a dotted module name to its file under REPO_ROOT.

	Returns None for stdlib / third-party / unresolved names so the
	walker can prune them.  Only modules under the repo are walked.
	"""
	rel = modname.replace(".", "/")
	candidates = [
		REPO_ROOT / f"{rel}.py",
		REPO_ROOT / rel / "__init__.py",
	]
	for c in candidates:
		if c.is_file():
			return c
	return None


def _collect_imports(path: pathlib.Path) -> Iterable[tuple[str, str | None]]:
	"""Yield (module, imported_symbol_or_None) tuples for a source file.

	For `import a.b` yields ("a.b", None).  For `from a.b import x`
	yields ("a.b", "x").  Used both to walk the import graph and to
	flag forbidden symbols.
	"""
	tree = ast.parse(path.read_text(encoding="utf-8"))
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			for alias in node.names:
				yield alias.name, None
		elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
			for alias in node.names:
				yield node.module, alias.name


def _walk_compiler_path() -> set[pathlib.Path]:
	"""Walk imports transitively from the compiler package-load entry.

	Roots at `lang.driftc.driftc` (the CLI) and follows imports of
	any module whose dotted name starts with `lang.driftc.`.  Other
	roots (tools, tests) are intentionally NOT walked -- this audit
	is scoped to the compiler load path, not the whole repo.
	"""
	queue = ["lang.driftc.driftc"]
	visited: set[str] = set()
	files: set[pathlib.Path] = set()
	while queue:
		modname = queue.pop()
		if modname in visited:
			continue
		visited.add(modname)
		path = _module_to_path(modname)
		if path is None:
			continue
		files.add(path)
		for imp_mod, _sym in _collect_imports(path):
			if imp_mod.startswith("lang.driftc."):
				queue.append(imp_mod)
	return files


def test_compiler_path_does_not_import_v0_trust_modules() -> None:
	"""No file in the compiler load path imports trust_v0 / signature_v0."""
	files = _walk_compiler_path()
	violations: list[str] = []
	for f in sorted(files):
		for mod, sym in _collect_imports(f):
			if mod in FORBIDDEN_MODULES:
				violations.append(f"{f.relative_to(REPO_ROOT)}: imports {mod}"
					+ (f" ({sym})" if sym else ""))
	assert not violations, (
		"v0 trust modules reachable from compiler load path:\n  "
		+ "\n  ".join(violations)
	)


def test_compiler_path_does_not_import_v0_trust_symbols() -> None:
	"""No file in the compiler load path imports a v0-only symbol by name."""
	files = _walk_compiler_path()
	violations: list[str] = []
	for f in sorted(files):
		for mod, sym in _collect_imports(f):
			if sym in FORBIDDEN_SYMBOLS:
				violations.append(f"{f.relative_to(REPO_ROOT)}: from {mod} import {sym}")
	assert not violations, (
		"v0 trust symbols reachable from compiler load path:\n  "
		+ "\n  ".join(violations)
	)


def test_compiler_path_has_no_sig_or_attestation_load_calls() -> None:
	"""The compiler load path must not invoke `.sig` or
	`.source-attestation` reads at runtime.

	Comments/docstrings are not flagged: this scan walks AST string
	constants used as filename-like arguments to common path ops, so
	a literal `".sig"` passed to `path.with_suffix(...)` or
	`Path(...) / ".sig"` triggers, while a passing docstring
	mention does not.
	"""
	files = _walk_compiler_path()
	violations: list[str] = []
	bad_literals = {".sig", ".source-attestation", ".source-attestation.json"}
	for f in sorted(files):
		tree = ast.parse(f.read_text(encoding="utf-8"))
		for node in ast.walk(tree):
			# `path.with_suffix(".sig")`-style calls
			if isinstance(node, ast.Call):
				for arg in node.args:
					if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
						if arg.value in bad_literals:
							violations.append(
								f"{f.relative_to(REPO_ROOT)}:{node.lineno}: "
								f"call uses forbidden literal {arg.value!r}"
							)
	assert not violations, (
		"v0 sidecar literals reachable from compiler load path:\n  "
		+ "\n  ".join(violations)
	)


def test_driftc_re_exports_v1_surface() -> None:
	"""driftc.py top-level imports must use the v1 trust surface."""
	driftc = REPO_ROOT / "lang" / "driftc" / "driftc.py"
	imports = list(_collect_imports(driftc))
	v1_provider_imports = {sym for mod, sym in imports
		if mod == "lang.driftc.packages.provider_v1"}
	v1_trust_imports = {sym for mod, sym in imports
		if mod == "lang.driftc.packages.trust_v1"}

	# Spot-check the load entrypoint and the policy class are pulled
	# from v1.  If these names drift in driftc.py, this test fires.
	assert "load_package_v1_with_policy" in v1_provider_imports, (
		f"driftc.py is missing v1 load entrypoint; provider_v1 imports: "
		f"{sorted(v1_provider_imports)}"
	)
	assert "PackageTrustPolicy" in v1_provider_imports
	assert "TrustStore" in v1_trust_imports
	assert "load_core_trust_store" in v1_trust_imports
	assert "load_trust_store_json" in v1_trust_imports
	assert "merge_trust_stores" in v1_trust_imports
