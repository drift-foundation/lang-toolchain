set shell := ["bash", "-lc"]
set quiet
CLANG_BIN := "clang-15"
PYTEST_AUTO_JOBS := `PYTHONPATH=. ./.venv/bin/python3 tools/pytest_jobs.py`

# Default task: run deps check then full staged compiler tests.
default: deps-check test

deps-check:
	PYTHONPATH=. ./.venv/bin/python3 tools/deps_check.py

review-cleanup:
	rm -f combined_*

git-reset BRANCH:
	#!/usr/bin/env bash
	set -euo pipefail
	branch="{{BRANCH}}"
	git fetch origin "${branch}"
	git reset --hard "origin/${branch}"
	git clean -ffd
	echo "HEAD now at:"
	git --no-pager log -1 --pretty='format:%H%n%h %s%nAuthor: %an <%ae>%nDate: %ad'

# Full staged compiler tests
test: review-cleanup lang-stage1-test lang-stage2-test lang-stage3-test lang-stage4-test lang-parser-test lang-core-test lang-llvm-test lang-borrow-test lang-type-checker-test lang-method-registry-test lang-driver-test lang-codegen-test lang-gdb-test
	@echo "lang tests: Success."

# Shard 1: everything test runs except codegen.
test-shard-1: review-cleanup lang-stage1-test lang-stage2-test lang-stage3-test lang-stage4-test lang-parser-test lang-core-test lang-llvm-test lang-borrow-test lang-type-checker-test lang-method-registry-test lang-driver-test lang-gdb-test
	@echo "lang test-shard-1: Success."

# Shard 2: codegen e2e only.
test-shard-2:
	PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py --summarize
	@echo "lang test-shard-2: Success."

# Local build/release prep (no implicit full test run).
build: runtime-libs dist-publish-stdlib

lang-stage1-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	if ./.venv/bin/python3 -c "import xdist" >/dev/null 2>&1; then \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}" -v lang/tests/stage1; \
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
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}" -v lang/tests/stage2; \
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
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}" -v lang/tests/stage3; \
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
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}" -v lang/tests/stage4; \
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
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}" -v lang/tests/parser; \
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
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}" -v lang/tests/core; \
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
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}" -v lang/tests/type_checker; \
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
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}" -v lang/tests/method_registry; \
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
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${DRIVER_JOBS:-${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}}" -v lang/tests/driver; \
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
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}" -v lang/codegen/llvm/tests; \
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
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}" -v lang/tests/borrow_checker; \
	else \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang/tests/borrow_checker; \
	fi

# External consumer fleet (signed package path, K4/K10-K14 guards).
ext-consumer-test:
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang/tests/driver/test_external_consumer.py

# Nightly: consumer fleet + hunk regressions + stdlib package (with ASAN).
ext-consumer-test-nightly:
	PYTHONPATH=. DRIFT_ASAN=1 ./.venv/bin/python3 -m pytest -v \
		lang/tests/driver/test_external_consumer.py \
		lang/tests/driver/test_deploy_compiler_hunk_regressions.py \
		lang/tests/driver/test_deploy_stdlib_package.py

# Package-consumer e2e: report-only (all stdlib-importing tests through signed package path).
ext-e2e-report:
	PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/pkg_consumer_runner.py --summarize

# Package-consumer e2e: blocking smoke subset (CI gate).
ext-e2e-smoke:
	PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/pkg_consumer_runner.py \
		--blocking --only-cases result_ok_array_match_move_no_double_free,array_push_move_non_copy_implicit,array_pop_move_out_non_copy,match_wildcard_owned_payload_drop,abi_entrypoint_cross_module_call,abi_entrypoint_cross_module_struct_ok,std_core_string_from_utf8_bytes_api,std_runtime_scoped_stack_basic,std_io_preamble_installs_stdio,try_wrap_result_err_twice_min

# Package-consumer e2e: ASAN variant (nightly).
ext-e2e-asan:
	DRIFT_ASAN=1 PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/pkg_consumer_runner.py --summarize

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

# Prebuild runtime archives used by driftc/e2e archive-link mode.
runtime-libs CLANG="":
	#!/usr/bin/env bash
	set -euo pipefail
	clang_bin="{{CLANG}}"
	if [[ -z "${clang_bin}" ]]; then
		clang_bin="$(command -v clang-15 || command -v clang || true)"
	fi
	if [[ -z "${clang_bin}" ]]; then
		echo "clang not found (pass CLANG=... or install clang/clang-15)" >&2
		exit 1
	fi
	DRIFT_RUNTIME_CLANG="${clang_bin}" PYTHONPATH=. ./.venv/bin/python3 -c "from pathlib import Path; import os; from lang.language_runtime import build_runtime_archive; root=Path('.').resolve(); clang=os.environ['DRIFT_RUNTIME_CLANG']; [print(build_runtime_archive(root, clang=clang, variant=v)) for v in ('default','debug','asan','alloc_track','optimized')]"

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
	rm -f build/pkg/std.dmp.sig
	PYTHONPATH=. ./.venv/bin/python3 -m lang.drift sign build/pkg/std.dmp --key "${sign_key}" --include-pubkey
	PYTHONPATH=. ./.venv/bin/python3 -m lang.drift publish --force --dest-dir dist/release build/pkg/std.dmp

# Local fallback for early dev only (publishes unsigned std package).
dist-publish-stdlib-unsigned VERSION="0.1.0-dev" TARGET="drift-dev":
	#!/usr/bin/env bash
	set -euo pipefail
	mkdir -p build/pkg dist/release
	PYTHONPATH=. ./.venv/bin/python3 -m lang.driftc -M stdlib $(rg --files stdlib | rg '\.drift$') --package-id std --package-version "{{VERSION}}" --package-target "{{TARGET}}" --emit-package build/pkg/std.dmp --json
	PYTHONPATH=. ./.venv/bin/python3 -m lang.drift publish --dest-dir dist/release --allow-unsigned build/pkg/std.dmp

# Deploy a versioned, self-contained Drift distribution.
# Pass args through to tools/deploy/deploy.sh.
# Examples:
#   just deploy -- --dest "$HOME/opt/drift"
#   just deploy -- "$HOME/opt/drift" --python "$PWD/.venv/bin/python3"
deploy *ARGS:
	tools/deploy/deploy.sh {{ARGS}}

# Print shell env lines for an existing deployment.
deploy-print-env DEST:
	#!/usr/bin/env bash
	set -euo pipefail
	dest="{{DEST}}"
	if [[ ! -L "${dest}/current" ]]; then
		echo "error: no deployment found at ${dest} (missing 'current' symlink)" >&2
		exit 1
	fi
	resolved="$(readlink "${dest}/current")"
	echo "# Drift distribution: ${resolved}"
	echo "export PATH=\"${dest}/current/bin:\$PATH\""
