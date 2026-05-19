# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Author-key-out-of-orch structural boundary (trust-v1).

The trust-v1 model splits sign authority into two roles:

  - **author** — signs source identity + release intent
    (`tools/drift_author/`).  Author key holder runs author-publish
    locally or on a per-author workstation; never on the deploy /
    orch cluster.

  - **certifier** — signs artifact bytes + toolchain + dep_graph +
    cert-suite result (`tools/drift_deploy/cert_emit.py`).
    Certifier key holder runs the deploy pipeline.

The product invariant: **the deploy / orch pipeline must be able
to certify a release WITHOUT ever holding the author key.**  This
is a hard product boundary the trust-v1 audit pinned -- a single
compromised orch node must not be able to forge BOTH an author
claim and a cert claim, because they live with different key
material.

This test enforces the contract statically:

  - The cert-emit code path (`tools/drift_deploy/cert_emit.py`)
    MUST NOT import anything under `tools/drift_author/`.
  - Nothing else under `tools/drift_deploy/` may either, since the
    deploy pipeline could otherwise stage author keys in a parent
    module that cert_emit reads incidentally.
  - The whole orch surface (`tools/deploy/`, `tools/drift_deploy/`)
    must be free of author-key reads.

A runtime sentinel would only catch the violation at exec time,
after author key bytes had already entered the process.  The
static walk catches a future regression at code-review time.
"""

from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


# Modules whose contents must not be reachable from the orch
# (deploy / cert) pipeline by any import edge.
FORBIDDEN_TARGETS = (
	"tools.drift_author",
)

# Roots scanned for violations.  Add new orch-side directories
# here as the pipeline grows.
ORCH_ROOTS = (
	"tools/drift_deploy",
	"tools/deploy",
)


def _collect_imports(py_path: Path) -> list[tuple[int, str, str]]:
	"""Yield (lineno, target_module, raw_form) for each import in the file."""
	tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
	out: list[tuple[int, str, str]] = []
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			for alias in node.names:
				out.append((node.lineno, alias.name, f"import {alias.name}"))
		elif isinstance(node, ast.ImportFrom):
			if node.module is None or node.level != 0:
				continue
			for alias in node.names:
				out.append((
					node.lineno,
					node.module,
					f"from {node.module} import {alias.name}",
				))
	return out


def _files_under(root_rel: str) -> list[Path]:
	root = REPO_ROOT / root_rel
	if not root.is_dir():
		return []
	return sorted(p for p in root.rglob("*.py") if p.is_file())


def _hits_forbidden(target: str) -> bool:
	"""True if `target` is a forbidden module or a descendant."""
	for forbidden in FORBIDDEN_TARGETS:
		if target == forbidden or target.startswith(forbidden + "."):
			return True
	return False


def test_orch_pipeline_does_not_import_author_module() -> None:
	"""Cert / deploy code MUST NOT reach `tools.drift_author.*`.

	The cert pipeline holds a certifier key, never an author key.
	If a deploy module imports anything from `tools.drift_author`,
	author key material can enter the orch process by side effect
	(e.g. module-level key loading at import time).  Refuse at
	test time.
	"""
	violations: list[str] = []
	for root_rel in ORCH_ROOTS:
		for py_path in _files_under(root_rel):
			for lineno, target, raw in _collect_imports(py_path):
				if _hits_forbidden(target):
					rel = py_path.relative_to(REPO_ROOT)
					violations.append(f"{rel}:{lineno}: forbidden ({raw})")
	assert not violations, (
		"orch pipeline must not import author-key code paths:\n  "
		+ "\n  ".join(violations)
		+ "\n(see lang/tests/packages/test_author_key_boundary.py for "
		"the contract; author claims sign in tools/drift_author/, "
		"cert claims sign in tools/drift_deploy/cert_emit.py with "
		"a separate key role.)"
	)


def test_orch_pipeline_does_not_read_author_seed_files() -> None:
	"""Belt-and-suspenders: even without importing the author
	module, orch code must not directly read an `*author*seed*`
	file (a future regression where someone duplicates the seed
	loader logic inline).

	Scans for string literals in orch sources that name an
	author-seed convention.  Catches the most obvious copy-paste
	regression without false-positiving on the author module's own
	docstrings.
	"""
	suspect_substrings = (
		"author_seed",
		"author-seed",
		"AUTHOR_SEED",
	)
	violations: list[str] = []
	for root_rel in ORCH_ROOTS:
		for py_path in _files_under(root_rel):
			tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
			for node in ast.walk(tree):
				if isinstance(node, ast.Constant) and isinstance(node.value, str):
					for sub in suspect_substrings:
						if sub in node.value:
							rel = py_path.relative_to(REPO_ROOT)
							violations.append(
								f"{rel}:{getattr(node, 'lineno', '?')}: "
								f"orch code references {sub!r} literal "
								f"({node.value!r})"
							)
	assert not violations, (
		"orch pipeline must not reference author-seed literals:\n  "
		+ "\n  ".join(violations)
	)


def test_author_module_does_not_import_orch_pipeline() -> None:
	"""Reverse direction: the author tool MUST NOT import the
	cert / deploy pipeline either.  Author-publish is a leaf tool
	that runs on a workstation; pulling deploy-side modules in
	would entangle the two roles and could surface deploy state
	to an author key holder.

	This direction is a softer policy boundary than the
	primary author-key-out-of-orch invariant, but worth pinning so
	the split stays clean as the trees grow.
	"""
	violations: list[str] = []
	for py_path in _files_under("tools/drift_author"):
		for lineno, target, raw in _collect_imports(py_path):
			if target.startswith("tools.drift_deploy") or target.startswith("tools.deploy"):
				rel = py_path.relative_to(REPO_ROOT)
				violations.append(f"{rel}:{lineno}: forbidden ({raw})")
	assert not violations, (
		"author tool must not import orch / deploy modules:\n  "
		+ "\n  ".join(violations)
	)
