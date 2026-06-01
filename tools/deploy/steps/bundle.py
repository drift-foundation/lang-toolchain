# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Deploy step: bundle compiler sources, runtime archives, docs, and examples.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

COMPILER_PACKAGES = (
	"lang/driftc",
	"lang/drift",
	"lang/codegen",
	"lang/compiler_infra",
	"lang/language_runtime",
)

SOURCE_EXTENSIONS = frozenset({".py", ".lark"})
NATIVE_EXTENSIONS = frozenset({".c", ".h", ".S"})

RUNTIME_VARIANTS = ("default", "debug", "asan", "alloc_track")


def install_flocker(repo_root: Path, dist: Path) -> None:
	"""Copy the flocker host-local slot wrapper into dist/bin.

	flocker is a generic bash utility (no Drift coupling) used by
	certification runners that need a global concurrency cap across
	multiple test lanes. Ships in bin/ so it lands on PATH alongside
	drift/driftc post-deploy.
	"""
	src = repo_root / "bin" / "flocker"
	if not src.exists():
		raise RuntimeError(f"{src} not found in source tree")
	out = dist / "bin" / "flocker"
	out.parent.mkdir(parents=True, exist_ok=True)
	shutil.copy2(str(src), str(out))
	out.chmod(0o755)
	print(f"[deploy] installed: {out}", flush=True)


# CI scripts shipped under `lib/tools/` in the distribution.  Unlike `bin/`
# artifacts (the PEX `driftc`/`drift` compiler, the host-general bash `flocker`),
# these are NOT on PATH and NOT peers of the compiler — they are a library of
# reusable CI components and require a host `python3`.  `lib/tools/` keeps them
# out of the public `bin/` surface and the top-level layout
# (bin/lib/doc/examples) unchanged.  Each entry maps a source path to its
# `drift_`-prefixed dest name under `lib/tools/`.
DEV_TOOLS = (
	("tools/drift_test_run.py", "drift_test_run.py"),
	# Budget protocol that drift_test_run.py sibling-imports
	# (DRIFT_TEST_JOBS / ceil(nproc/2)).  Renamed with the drift_ prefix in the
	# bundle; the runner imports `drift_pytest_jobs` first, falling back to the
	# source-tree name `pytest_jobs`.  Both land in lib/tools/ together.
	("tools/pytest_jobs.py", "drift_pytest_jobs.py"),
)


def bundle_dev_tools(repo_root: Path, dist: Path) -> None:
	"""Copy CI scripts into dist/lib/tools with drift_ names.

	Ships the shared test-runner (`drift_test_run.py`) and its budget helper
	(`drift_pytest_jobs.py`) so teams can consume the runner from a staged
	toolchain at the `lib/tools/` path `doc/test-run.md` references.  These are
	CI machinery, not installed user-facing binaries — kept out of `bin/` and
	off PATH, and (unlike the PEX/bash artifacts in `bin/`) they require a host
	`python3`.  `drift_test_run.py` finds the `bin/` siblings
	(`flocker`/`driftc`/`drift`) by walking up to the distribution root, so this
	`lib/tools/` placement resolves at runtime.
	"""
	out_dir = dist / "lib" / "tools"
	out_dir.mkdir(parents=True, exist_ok=True)
	for rel, dest_name in DEV_TOOLS:
		src = repo_root / rel
		if not src.exists():
			raise RuntimeError(f"{src} not found in source tree")
		out = out_dir / dest_name
		shutil.copy2(str(src), str(out))
		out.chmod(0o755)
		print(f"[deploy] installed: {out}", flush=True)


def bundle_compiler(repo_root: Path, dist: Path) -> None:
	"""Copy compiler Python sources and non-Python assets into dist."""
	compiler_lib = dist / "lib" / "compiler"

	# Verify PEX executables exist.
	for name in ("driftc", "drift"):
		exe = dist / "bin" / name
		if not exe.exists():
			raise RuntimeError(f"{exe} not found; PEX build must run first")

	# Python + grammar files.
	for pkg in COMPILER_PACKAGES:
		src_dir = repo_root / pkg
		if not src_dir.is_dir():
			continue
		for root, _dirs, files in os.walk(str(src_dir)):
			for fname in files:
				src = Path(root) / fname
				if src.suffix not in SOURCE_EXTENSIONS:
					continue
				rel = src.relative_to(repo_root)
				dst = compiler_lib / rel
				dst.parent.mkdir(parents=True, exist_ok=True)
				shutil.copy2(str(src), str(dst))

	# Ensure lang/__init__.py exists and shared top-level modules are bundled.
	(compiler_lib / "lang" / "__init__.py").touch()
	versions_src = repo_root / "lang" / "versions.py"
	if versions_src.exists():
		shutil.copy2(str(versions_src), str(compiler_lib / "lang" / "versions.py"))

	# Stamp the source commit into the bundled versions.py so that deployed
	# toolchains report the exact commit they were built from.
	bundled_versions = compiler_lib / "lang" / "versions.py"
	if bundled_versions.exists():
		import subprocess as _sp
		try:
			res = _sp.run(
				["git", "rev-parse", "--short", "HEAD"],
				capture_output=True, text=True, cwd=str(repo_root), timeout=5,
			)
			if res.returncode == 0:
				sha = res.stdout.strip()
				text = bundled_versions.read_text(encoding="utf-8")
				text = text.replace('DRIFTC_GIT_SHA: str = ""', f'DRIFTC_GIT_SHA: str = "{sha}"')
				bundled_versions.write_text(text, encoding="utf-8")
				print(f"[deploy] stamped source commit {sha} into versions.py", flush=True)
		except Exception:
			pass

	# C/H/S sources for runtime archive rebuilds.
	for pkg in ("lang/language_runtime", "lang/compiler_infra"):
		src_dir = repo_root / pkg
		if not src_dir.is_dir():
			continue
		for root, _dirs, files in os.walk(str(src_dir)):
			for fname in files:
				src = Path(root) / fname
				if src.suffix not in NATIVE_EXTENSIONS:
					continue
				rel = src.relative_to(repo_root)
				dst = compiler_lib / rel
				dst.parent.mkdir(parents=True, exist_ok=True)
				shutil.copy2(str(src), str(dst))


def bundle_runtime_archives(repo_root: Path, dist: Path) -> list[str]:
	"""Copy pre-built runtime archives. Returns list of bundled variants."""
	from lang.language_runtime import runtime_archive_name
	bundled: list[str] = []
	for variant in RUNTIME_VARIANTS:
		# Variant-aware filename: the dual-runtime contract requires the
		# debug variant to carry the explicit `_debug` infix on disk so
		# production releases can spot it by inspection.
		ar_name = runtime_archive_name(variant)
		src = repo_root / "build" / "runtime_libs" / variant / ar_name
		if src.exists():
			dst_dir = dist / "lib" / "runtime" / variant
			dst_dir.mkdir(parents=True, exist_ok=True)
			shutil.copy2(str(src), str(dst_dir / ar_name))
			bundled.append(variant)
		else:
			import sys
			print(f"warning: runtime archive for variant '{variant}' not found, skipping", file=sys.stderr)

	# Remove leaked build artifacts.
	for lock in dist.rglob(".build.lock"):
		lock.unlink()

	return bundled


def bundle_docs_and_examples(dist: Path) -> None:
	"""Generate docs and example files."""
	doc_dir = dist / "doc"
	doc_dir.mkdir(parents=True, exist_ok=True)
	(doc_dir / "README.md").write_text(_README_TEXT, encoding="utf-8")

	# Ship the checked-in official docs into the toolchain distribution so
	# consumers can read the language guide, design/spec docs, and toolchain
	# workflow docs after deployment.
	_ROOT = Path(__file__).resolve().parents[3]
	docs_src = _ROOT / "docs"
	if docs_src.is_dir():
		for src in sorted(docs_src.rglob("*")):
			if not src.is_file():
				continue
			dst = doc_dir / src.relative_to(docs_src)
			dst.parent.mkdir(parents=True, exist_ok=True)
			shutil.copy2(src, dst)

	examples_dir = dist / "examples"
	examples_dir.mkdir(parents=True, exist_ok=True)
	(examples_dir / "hello.drift").write_text(_HELLO_DRIFT, encoding="utf-8")
	(examples_dir / "README.md").write_text(_EXAMPLES_README, encoding="utf-8")

	# Generate stdlib API reference from source.
	stdlib_src = _ROOT / "stdlib" / "std"
	if stdlib_src.is_dir():
		from tools.drift_doc.drift_doc import generate_docs
		stdlib_doc_dir = doc_dir / "stdlib"
		generate_docs(source_root=stdlib_src, output_dir=stdlib_doc_dir)

	print("[deploy] bundle complete", flush=True)


_README_TEXT = """\
# Drift Distribution

## Prerequisites

The Drift compiler requires:

| Dependency | Version | Purpose |
|------------|---------|---------|
| clang | 15+ | Linker / native codegen |

No host Python installation is required — the deployed `bin/driftc` is a
self-contained PEX --scie eager executable that embeds its own Python
interpreter and all third-party dependencies.

Verify your environment:

```bash
clang --version
```

## Quick start

```bash
export PATH="<deploy-root>/bin:$PATH"
driftc my_program.drift -o my_program
./my_program
```

## Using the compiler

`bin/driftc` is a PEX --scie eager executable that bundles:
- An embedded CPython interpreter
- All third-party Python dependencies (lark, llvmlite, cryptography, zstandard)

## Drift CLI tool

`bin/drift` is a self-contained PEX --scie eager executable providing
the Drift consumer-side tooling CLI (publishing identity setup,
trust management, build, prepare, deploy).  It bundles its own
Python interpreter and runtime dependencies (cryptography, zstandard).

Author-side signing has its own subcommand, `drift author`
(the surface inside `bin/drift`), backed by a separately-loaded
key-handling module so author key material stays out of the
consumer-side code paths (see `docs/design/trust-v1.md`).

```bash
drift init                        # set up publishing identity + author profile
drift author --manifest drift/manifest.json --key-file <author.seed>  # mint/refresh author claim
drift trust <publisher>.author-profile   # consumer: trust an author kid
drift trust add --namespace acme.crypto.* \
    --pubkey-b64 <base64> --kid <kid> --role both
drift trust import <pkg>.author-claim    # bulk-import kids from a v1 sidecar
drift trust revoke --kid <kid> --reason "compromised CI host"
drift prepare --manifest drift/manifest.json --dest ~/opt/drift/lib
drift deploy --manifest drift/manifest.json --dest ~/opt/drift/lib --driftc driftc
```

The compiler sources, runtime archives, and signed stdlib package live in
`lib/` and are resolved relative to the executable's path.  No repo checkout,
ambient `pip install`, or PYTHONPATH setup is needed — only clang.

### Stdlib API reference

The `doc/stdlib/` directory contains auto-generated API reference for every
standard library module.  Open `doc/stdlib/index.md` for a module listing,
or browse individual files (e.g. `doc/stdlib/std_text.md`) for function
signatures, types, and doc comments.

To regenerate docs or generate docs for your own modules:

```bash
drift doc <source-dir-or-file> -o <output-dir>
```

See `doc/stdlib/authoring.md` for how to write doc comments that
`drift doc` picks up.

### Stdlib integrity

The standard library is shipped as a DMIR package
(`lib/stdlib/std.dmp`) plus the trust-v1 sidecar pair:
`lib/stdlib/std.author-claim` (Foundation's author kid) and
`lib/stdlib/std.cert-claim.<kid>.json` (Foundation's certifier
kid).  The compiler verifies both claims against the bundled
core trust store at compile time (see
`docs/design/trust-v1.md`).  Tampered or unsigned stdlib
packages are rejected.

### Flags

| Flag | Purpose |
|------|---------|
| `-o <path>` | Output binary path |
| `-g` / `--debug-info` | Emit DWARF debug info (orthogonal to runtime variant selection) |
| `--entry <mod>::<fn>` | Custom entry point (default: `main::main`) |
| `--json` | Machine-readable diagnostics |

### Environment

| Variable | Purpose |
|----------|---------|
| `DRIFT_DEBUG` | Set to `1` to link the debug-style runtime variant (`_debug` archive); equivalent to `drift build --debug` |
| `DRIFT_ASAN` | Set to `1` to link with AddressSanitizer runtime |
| `DRIFT_TRUST_STORE` | Path to trust store JSON for user/third-party packages |
| `SCIE_BASE` | Override scie cache directory (default: `~/.cache/nce`) |

## ABI compatibility

Each distribution is built against a specific runtime ABI version
(see `lib/manifest.json`).  Binaries compiled with one ABI version
cannot link against a runtime built for a different version — the
linker will fail with an unresolved `__drift_rt_abi_version_N` symbol.

Exact toolchain identity comes from `driftc --version`, `lib/manifest.json`,
provenance metadata, and certification records.

## Deploy

`deploy.py` orchestrates the build in Python step modules:

1. `steps/pex.py` — build PEX --scie eager executables (bin/driftc, bin/drift)
2. `steps/bundle.py` — copy compiler sources, runtime archives, CI tools
   (lib/tools/), and docs into staged tree
3. `steps/stdlib.py` — build, sign, and install stdlib package + core trust store
4. `steps/smoke.py` — compile and run smoke test using only deployed paths
5. `steps/publish.py` — atomically publish staged tree to destination

If any step fails, deploy exits non-zero and does not publish a partial install.

## First-run scie extraction

On first invocation, the scie executable extracts its embedded Python
interpreter to a per-user cache (`~/.cache/nce/` by default).  Subsequent
runs reuse the cache.  Override the cache location with `SCIE_BASE`.

## See also

- `doc/effective-drift.md` — idiom guide covering common Drift
  patterns (Arc+Mutex shared state, runtime registry, JSON API,
  graceful shutdown, MPSC queues, atomic ordering, method overload
  resolution, call-site auto-borrow, `String.clone()`, and more).
- `doc/design/` — language spec, grammar, ABI, stdlib, package, and
  runtime design docs.
- `doc/articles/` — architecture notes and deeper design articles.
- `doc/toolchain-build-workflow.md` — certified toolchain build and
  deployment workflow.
- `doc/test-run.md` — the shared parallel job executor for test/perf/stress
  gates.  The tool itself ships under `lib/tools/` (CI machinery, host-`python3`,
  not on PATH): `python3 lib/tools/drift_test_run.py …`.
- `doc/history.md` — development history for this toolchain.
- `doc/stdlib/index.md` — generated stdlib API reference.
- `doc/stdlib/authoring.md` — how to write `///` doc comments that
  the `drift doc` tool picks up.
"""

_HELLO_DRIFT = """\
module main;

import std.console as console;

pub fn main() nothrow -> Int {
\tconsole.println("hello, drift!");
\treturn 0;
}
"""

_EXAMPLES_README = """\
# Examples

## hello.drift

Compile and run:

```bash
driftc examples/hello.drift -o /tmp/hello   # drift-tmp-root-audit: allow docstring example
/tmp/hello                                   # drift-tmp-root-audit: allow docstring example
```

Expected output: `hello, drift!` with exit code 0.
"""
