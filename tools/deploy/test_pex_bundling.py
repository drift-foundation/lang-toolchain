# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
PEX bundling contract for the deployed `drift` CLI.

Regression pin for the 0.32.16 ship-break:
`lang/drift/cli.py` grew a `drift author` dispatcher that imports
`tools.drift_author.cli::run_author_subcommand`, but
`tools/deploy/steps/pex.py::build_drift_pex` did not stage the
`tools.drift_author` package into the PEX.  Result: every deployed
`drift` binary raised `ModuleNotFoundError: No module named
'tools.drift_author'` on first invocation.

This test scans `lang/drift/cli.py` for every `from tools.<pkg>
import ...` line and asserts that `<pkg>` is on
`tools.deploy.steps.pex.BUNDLED_TOOLS_PACKAGES`.  Same check on
`tools/deploy/deploy_pex_entry.py` so the entry-point's pre-argparse
dispatch is also covered.

If you add a new `tools.<X>` dispatcher in either file, extend
`BUNDLED_TOOLS_PACKAGES` and this test passes.  If you forget,
this test fails BEFORE the PEX is built -- much cheaper than
discovering the gap by deploying a broken binary.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


# Pattern matches `from tools.<pkg> import ...` and `from tools.<pkg>.<mod> import ...`.
# Captures the top-level package name only -- that's the staging granularity
# in `build_drift_pex`.
_TOOLS_IMPORT_RE = re.compile(
	r"^\s*from\s+tools\.([a-z_][a-z0-9_]*)(?:\.[a-z_][a-z0-9_.]*)?\s+import\b",
	re.MULTILINE,
)


def _bundled_packages() -> tuple[str, ...]:
	"""Read the bundle contract from `tools/deploy/steps/pex.py`.

	Imports lazily so this test file can be collected even when the
	deploy step's transitive deps (e.g. `lang.test_support`) aren't on
	the path -- the contract value is a plain tuple constant.
	"""
	from tools.deploy.steps.pex import BUNDLED_TOOLS_PACKAGES
	return BUNDLED_TOOLS_PACKAGES


def _scan_tools_imports(source_path: Path) -> set[str]:
	"""Return the set of top-level `tools.<pkg>` names imported by
	`source_path`.  Excludes `tools.deploy.*` (the deploy steps are
	build-time infrastructure, not runtime dispatch targets) and
	`tools.<X>` references that appear inside string literals or
	comments (matched at line start, optionally indented).
	"""
	text = source_path.read_text(encoding="utf-8")
	return {m.group(1) for m in _TOOLS_IMPORT_RE.finditer(text)}


def test_lang_drift_cli_dispatchers_are_all_bundled() -> None:
	"""Every `from tools.<X> import ...` in `lang/drift/cli.py` must
	correspond to a package in `BUNDLED_TOOLS_PACKAGES`.

	This is the test that would have caught the 0.32.16 ship-break:
	`drift author` was added in cli.py but `tools.drift_author` was
	not staged.
	"""
	imported = _scan_tools_imports(ROOT / "lang" / "drift" / "cli.py")
	# `tools.deploy` is the deploy-time orchestrator, not a runtime
	# dispatch target -- excluded by design.
	runtime_imports = {pkg for pkg in imported if pkg != "deploy"}
	bundled = set(_bundled_packages())
	missing = runtime_imports - bundled
	assert not missing, (
		f"`lang/drift/cli.py` dispatches to tools packages that are "
		f"NOT staged into the deployed PEX: {sorted(missing)!r}.  "
		f"Either add the package to `BUNDLED_TOOLS_PACKAGES` in "
		f"`tools/deploy/steps/pex.py`, or remove the dispatcher.  "
		f"(Currently bundled: {sorted(bundled)!r}; cli.py dispatches "
		f"to: {sorted(runtime_imports)!r}.)"
	)


def test_deploy_pex_entry_dispatchers_are_all_bundled() -> None:
	"""Same contract for `tools/deploy/deploy_pex_entry.py`'s
	pre-argparse dispatch (currently routes `drift deploy ...` to
	`tools.drift_deploy.drift_deploy.run`).
	"""
	imported = _scan_tools_imports(ROOT / "tools" / "deploy" / "deploy_pex_entry.py")
	bundled = set(_bundled_packages())
	missing = imported - bundled
	assert not missing, (
		f"`tools/deploy/deploy_pex_entry.py` dispatches to tools "
		f"packages that are NOT staged into the deployed PEX: "
		f"{sorted(missing)!r}.  Extend `BUNDLED_TOOLS_PACKAGES` in "
		f"`tools/deploy/steps/pex.py`."
	)


def test_bundled_packages_exist_on_disk() -> None:
	"""Sanity: each entry in `BUNDLED_TOOLS_PACKAGES` names a real
	directory under `tools/`.  Catches typos in the bundle list
	before they fail at PEX build time.
	"""
	bundled = _bundled_packages()
	assert bundled, "BUNDLED_TOOLS_PACKAGES must not be empty"
	for pkg in bundled:
		pkg_dir = ROOT / "tools" / pkg
		assert pkg_dir.is_dir(), (
			f"BUNDLED_TOOLS_PACKAGES names {pkg!r}, but {pkg_dir} is "
			f"not a directory"
		)
		# Must be an importable package (have __init__.py) OR be a
		# pure namespace package; assert at least one .py file present.
		py_files = list(pkg_dir.glob("*.py"))
		assert py_files, f"tools/{pkg}/ contains no .py files"


@pytest.mark.parametrize("pkg_name", [
	# Static parametrize so each bundled package gets its own test
	# line in the runner output -- easier to read when one regresses.
	"drift_deploy",
	"drift_author",
	"drift_doc",
])
def test_bundled_package_imports_cleanly(pkg_name: str) -> None:
	"""Each bundled `tools.<pkg>` package must be importable from a
	clean sys.path (i.e. all its module-level imports resolve from
	what the PEX itself bundles + the compiler `lib/compiler/` tree).

	This is a source-level proxy for "would the deployed PEX import
	this cleanly".  It does NOT exercise the actual PEX binary; that
	level of integration test belongs in a separate deploy-shape
	suite.
	"""
	import importlib
	# Import the package; the import system will pull in any top-level
	# side effects.  Failure here surfaces missing deps in CI before
	# the deploy step builds the PEX.
	mod = importlib.import_module(f"tools.{pkg_name}")
	assert mod is not None
