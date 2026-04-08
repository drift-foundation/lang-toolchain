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

	examples_dir = dist / "examples"
	examples_dir.mkdir(parents=True, exist_ok=True)
	(examples_dir / "hello.drift").write_text(_HELLO_DRIFT, encoding="utf-8")
	(examples_dir / "README.md").write_text(_EXAMPLES_README, encoding="utf-8")

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
the Drift tooling CLI (publishing identity setup, package signing,
trust management, and deploy).  It bundles its own Python interpreter
and runtime dependencies (cryptography, zstandard).

```bash
drift init                        # set up publishing identity + author profile
drift sign my-pkg.dmp --key signing.seed
drift prepare --manifest drift-manifest.json --dest ~/opt/drift/libs
drift deploy --manifest drift-manifest.json --dest ~/opt/drift/libs --driftc driftc
drift trust publisher.author-profile   # consumer: trust an author
drift trust list --trust-store trust.json
```

The compiler sources, runtime archives, and signed stdlib package live in
`lib/` and are resolved relative to the executable's path.  No repo checkout,
ambient `pip install`, or PYTHONPATH setup is needed — only clang.

### Stdlib integrity

The standard library is shipped as a signed DMIR package (`lib/stdlib/std.dmp`)
with a detached signature sidecar (`lib/stdlib/std.sig`).  The compiler
verifies the signature against the bundled core trust store at compile time.
Tampered or unsigned stdlib packages are rejected.

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
2. `steps/bundle.py` — copy compiler sources, runtime archives, docs into staged tree
3. `steps/stdlib.py` — build, sign, and install stdlib package + core trust store
4. `steps/smoke.py` — compile and run smoke test using only deployed paths
5. `steps/publish.py` — atomically publish staged tree to destination

If any step fails, deploy exits non-zero and does not publish a partial install.

## First-run scie extraction

On first invocation, the scie executable extracts its embedded Python
interpreter to a per-user cache (`~/.cache/nce/` by default).  Subsequent
runs reuse the cache.  Override the cache location with `SCIE_BASE`.
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
driftc examples/hello.drift -o /tmp/hello
/tmp/hello
```

Expected output: `hello, drift!` with exit code 0.
"""
