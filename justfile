set shell := ["bash", "-lc"]
set quiet
CLANG_BIN := "clang-15"

# Default task: run deps check then staged lang compiler tests.
default: deps-check lang-test

deps-check:
	PYTHONPATH=. ./.venv/bin/python3 tools/deps_check.py

review-cleanup:
	rm -f combined_*

# Lang2 staged compiler tests
lang-test: review-cleanup lang-stage1-test lang-stage2-test lang-stage3-test lang-stage4-test lang-parser-test lang-core-test lang-llvm-test lang-borrow-test lang-type-checker-test lang-method-registry-test lang-driver-suite lang-codegen-test lang-gdb-test
	@echo "lang tests: Success."

lang-stage1-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	if ./.venv/bin/python3 -c "import xdist" >/dev/null 2>&1; then \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-auto}" -v lang/tests/stage1; \
	else \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang/tests/stage1; \
	fi

lang-stage2-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	if ./.venv/bin/python3 -c "import xdist" >/dev/null 2>&1; then \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-auto}" -v lang/tests/stage2; \
	else \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang/tests/stage2; \
	fi

lang-stage3-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	if ./.venv/bin/python3 -c "import xdist" >/dev/null 2>&1; then \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-auto}" -v lang/tests/stage3; \
	else \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang/tests/stage3; \
	fi

lang-stage4-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	if ./.venv/bin/python3 -c "import xdist" >/dev/null 2>&1; then \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-auto}" -v lang/tests/stage4; \
	else \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang/tests/stage4; \
	fi

# Parser tests (lang parser copy + adapter).
lang-parser-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	if ./.venv/bin/python3 -c "import xdist" >/dev/null 2>&1; then \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-auto}" -v lang/tests/parser; \
	else \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang/tests/parser; \
	fi

# Core TypeEnv/TypeTable tests.
lang-core-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	if ./.venv/bin/python3 -c "import xdist" >/dev/null 2>&1; then \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-auto}" -v lang/tests/core; \
	else \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang/tests/core; \
	fi

# Type checker tests (typed HIR + resolution).
lang-type-checker-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	if ./.venv/bin/python3 -c "import xdist" >/dev/null 2>&1; then \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-auto}" -v lang/tests/type_checker; \
	else \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang/tests/type_checker; \
	fi

# Method registry/resolver tests.
lang-method-registry-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	if ./.venv/bin/python3 -c "import xdist" >/dev/null 2>&1; then \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-auto}" -v lang/tests/method_registry; \
	else \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang/tests/method_registry; \
	fi

# Driver/integration tests (driftc pipeline, try sugar, declared events).
lang-driver-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	if ./.venv/bin/python3 -c "import xdist" >/dev/null 2>&1; then \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${DRIVER_JOBS:-${PYTEST_JOBS:-auto}}" -v lang/tests/driver; \
	else \
	  echo "pytest-xdist is missing in .venv; running driver tests serially (install: ./.venv/bin/python3 -m pip install pytest-xdist)"; \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang/tests/driver; \
	fi

lang-driver-suite:
	# Full driver suite (lang/tests/driver).
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	if ./.venv/bin/python3 -c "import xdist" >/dev/null 2>&1; then \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${DRIVER_JOBS:-${PYTEST_JOBS:-auto}}" -v lang/tests/driver; \
	else \
	  echo "pytest-xdist is missing in .venv; running driver tests serially (install: ./.venv/bin/python3 -m pip install pytest-xdist)"; \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang/tests/driver; \
	fi

# Basic LLVM codegen smoke test (llvmlite), kept separate from pytest collection.
lang-llvm-test:
	./.venv/bin/python3 tools/test-llvm/test_codegen.py /tmp/lang_test_codegen.o

# LLVM textual codegen tests (SSA→LLVM IR).
lang-codegen-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	# Clean codegen artifacts to keep cases isolated between runs.
	rm -rf build/tests/lang
	if ./.venv/bin/python3 -c "import xdist" >/dev/null 2>&1; then \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-auto}" -v lang/codegen/llvm/tests; \
	else \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang/codegen/llvm/tests; \
	fi
	# Run clang-based IR cases (per-case dirs under lang/codegen/ir_cases).
	PYTHONPATH=. ./.venv/bin/python3 lang/codegen/ir_cases/e2e_runner.py
	# Run Drift-source e2e cases (per-case dirs under lang/tests/codegen/e2e).
	PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py --summarize

lang-gdb-test:
	DRIFT_GDB_TEST=1 PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang/tests/gdb/test_gdb_runner.py

# Lang2 e2e runner (lang.driftc: json + run modes against tests/e2e)
lang-e2e CASES="":
	PYTHONPATH=. ./.venv/bin/python3 lang/codegen/codegen_runner.py {{CASES}}

# Borrow checker scaffolding tests.
lang-borrow-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	if ./.venv/bin/python3 -c "import xdist" >/dev/null 2>&1; then \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-auto}" -v lang/tests/borrow_checker; \
	else \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang/tests/borrow_checker; \
	fi

# Build examples (lang.driftc)
make-example EXAMPLE:
	#!/usr/bin/env bash
	set -euo pipefail
	set -x
	example="{{EXAMPLE}}"
	example_dir="examples/${example}"
	out_dir="build/examples/${example}"
	mkdir -p "${out_dir}"
	if [[ -f "${example_dir}/server.drift" ]] && [[ -f "${example_dir}/client.drift" ]]; then
		PYTHONPATH=. ./.venv/bin/python3 -m lang.driftc --stdlib-root stdlib "${example_dir}/server.drift" -o "${out_dir}/server"
		PYTHONPATH=. ./.venv/bin/python3 -m lang.driftc --stdlib-root stdlib "${example_dir}/client.drift" -o "${out_dir}/client"
	elif [[ -f "${example_dir}/main.drift" ]]; then
		out_bin="${out_dir}/example_${example}"
		extra_args=()
		if [[ "${example}" == debug_* ]]; then
			extra_args+=(--debug-info)
		fi
		PYTHONPATH=. ./.venv/bin/python3 -m lang.driftc "${extra_args[@]}" --stdlib-root stdlib "${example_dir}/main.drift" -o "${out_bin}"
	else
		shopt -s nullglob
		files=("${example_dir}"/*.drift)
		if [[ ${#files[@]} -eq 0 ]]; then
			echo "no drift sources found in ${example_dir}" >&2
			exit 1
		fi
		for src in "${files[@]}"; do
			name="$(basename "${src}" .drift)"
			PYTHONPATH=. ./.venv/bin/python3 -m lang.driftc --stdlib-root stdlib "${src}" -o "${out_dir}/${name}"
		done
	fi

make-examples:
	#!/usr/bin/env bash
	set -euo pipefail
	set -x
	shopt -s nullglob
	for d in examples/*; do
		[[ -d "${d}" ]] || continue
		just make-example "$(basename "${d}")"
	done

# Local package distribution repo scaffold (dev convenience).
dist-init:
	#!/usr/bin/env bash
	set -euo pipefail
	mkdir -p dist/release
	echo "initialized local repo: dist/release"

dist-publish PKG:
	#!/usr/bin/env bash
	set -euo pipefail
	pkg="{{PKG}}"
	if [[ ! -f "${pkg}" ]]; then
		echo "missing package file: ${pkg}" >&2
		exit 1
	fi
	mkdir -p dist/release
	PYTHONPATH=. ./.venv/bin/python3 -m lang.drift publish --dest-dir dist/release --allow-unsigned "${pkg}"

dist-index:
	#!/usr/bin/env bash
	set -euo pipefail
	if [[ ! -f dist/release/index.json ]]; then
		echo "dist/release/index.json not found (publish at least one package first)" >&2
		exit 1
	fi
	cat dist/release/index.json

# Build stdlib package and publish into local dist/release repo (signed by default).
# Key resolution priority: explicit SIGN_KEY arg, then DRIFT_SIGN_KEY_FILE.
dist-publish-stdlib SIGN_KEY="" VERSION="0.1.0-dev" TARGET="drift-dev":
	#!/usr/bin/env bash
	set -euo pipefail
	sign_key="{{SIGN_KEY}}"
	if [[ -z "${sign_key}" ]]; then
		sign_key="${DRIFT_SIGN_KEY_FILE:-}"
	fi
	if [[ -z "${sign_key}" ]]; then
		echo "missing signing key: pass SIGN_KEY or set DRIFT_SIGN_KEY_FILE" >&2
		exit 1
	fi
	if [[ ! -f "${sign_key}" ]]; then
		echo "missing signing key: ${sign_key}" >&2
		exit 1
	fi
	mkdir -p build/pkg dist/release
	PYTHONPATH=. ./.venv/bin/python3 -m lang.driftc -M stdlib $(rg --files stdlib | rg '\.drift$') --package-id std --package-version "{{VERSION}}" --package-target "{{TARGET}}" --emit-package build/pkg/std.dmp --json
	PYTHONPATH=. ./.venv/bin/python3 -m lang.drift sign build/pkg/std.dmp --key "${sign_key}" --include-pubkey
	PYTHONPATH=. ./.venv/bin/python3 -m lang.drift publish --dest-dir dist/release build/pkg/std.dmp

# Local fallback for early dev only (publishes unsigned std package).
dist-publish-stdlib-unsigned VERSION="0.1.0-dev" TARGET="drift-dev":
	#!/usr/bin/env bash
	set -euo pipefail
	mkdir -p build/pkg dist/release
	PYTHONPATH=. ./.venv/bin/python3 -m lang.driftc -M stdlib $(rg --files stdlib | rg '\.drift$') --package-id std --package-version "{{VERSION}}" --package-target "{{TARGET}}" --emit-package build/pkg/std.dmp --json
	PYTHONPATH=. ./.venv/bin/python3 -m lang.drift publish --dest-dir dist/release --allow-unsigned build/pkg/std.dmp

stage-for-review:
	#!/usr/bin/env bash
	staged_dir=staged
	TODAY=$(date +'%Y-%m-%dT%H-%M-%S%Z')
	COMBINED_NAME="combined_${TODAY}.txt"
	rm -rf "$staged_dir"
	mkdir -p "$staged_dir"
	rm -f combined_*
	git ls-files -m -o --exclude-standard | while IFS= read -r f; do
		[ -f "$f" ] || continue
		mkdir -p "$staged_dir/$(dirname "$f")"
		cp -- "$f" "$staged_dir/$f"
	done
	mapfile -d '' files < <(find "$staged_dir/" -type f -print0 | sort -z)
	{
		echo "[==== AGENT INSTRUCTIONS ====]"
		echo "Role: Act as a production-compiler reviewer for Drift."
		echo "Primary constraints (in order): (1) semantic correctness + soundness, (2) adherence to and inspiration from: the Drift language spec (if you don't have it ask for it), modern languages like Rust, Java's Project Loom and POSIX C whenever our lang-spec is undrespecified, (3) determinism + reproducibility, (4) diagnostics quality/stability, (5) maintainability/extensibility, (6) performance (no hidden big-O or allocation cliffs)."
		echo "Review requirements: For each change/claim, verify against invariants, spec rules, and edge cases; when unsure (missing spec context), say exactly what can’t be verified and what assumption you’re making."
		echo "Output: Report only issues, risks, or better long-term alternatives. You can include Drift code snippets or pseudo code to illustrate solutions. If no issues, output “Reviewed and found no material issues to resolve.” and (optionally) a one-line checklist of what you verified. "
		echo "Decision rule: If multiple solutions exist, recommend the cleanest long-term design even if it’s more work; avoid speculative refactors unless requested. Don't say: If you want a “clean long-term alternative” - we positively want only "clean long-term", provide the plan for executing it. "
		echo "Style: concise, technical, no fluff."
		echo "Date: ${TODAY}"
		echo "[==== FILE LIST ====]"
		printf '%s\n' "${files[@]}"
		echo

		# feed awk a NUL-separated list so xargs -0 is happy
		printf '%s\0' "${files[@]}" |
			xargs -0 awk '
				FNR==1 { print "\n[==== File: " FILENAME " =====]" }
				{ print }
			'
	} > $COMBINED_NAME

	# rm -f staged.zip && zip -r staged.zip "$staged_dir"
