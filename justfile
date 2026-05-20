set shell := ["bash", "-lc"]
set quiet
CLANG_BIN := "clang"
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

# Full staged compiler tests + package-consumer smoke + boundary regressions.
# Invariant: test-shard-1 + test-shard-2 + test-shard-3 == just test.
# `ownership-matrix-check` is listed here (and on test-shard-2) as a
# direct top-level dep so the generator-freshness guard runs on the
# full suite even though no top-level target calls a shard target.
test: review-cleanup ownership-matrix-check lang-stage1-test lang-stage2-test lang-stage3-test lang-stage4-test lang-parser-test lang-core-test lang-llvm-test lang-borrow-test lang-type-checker-test lang-method-registry-test lang-packages-test lang-traits-test lang-driver-test lang-codegen-test lang-gdb-test drift-deploy-test ext-e2e-smoke ext-e2e-boundary ownership-matrix-pkgb
	@echo "lang tests: Success."

# Shard 1: everything test runs except codegen.
test-shard-1: review-cleanup lang-stage1-test lang-stage2-test lang-stage3-test lang-stage4-test lang-parser-test lang-core-test lang-llvm-test lang-borrow-test lang-type-checker-test lang-method-registry-test lang-packages-test lang-traits-test lang-driver-test lang-gdb-test
	@echo "lang test-shard-1: Success."

# Shard 2: codegen e2e only.
# The ownership-matrix check runs first so stale/hand-edited generated
# fixtures fail the shard before the long e2e sweep starts (fast, ~1s).
# The generated fixtures themselves are part of the e2e sweep and
# picked up by the normal shallow scan.
test-shard-2: ownership-matrix-check
	PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py --summarize
	@echo "lang test-shard-2: Success."

# Regenerate the ownership-transfer matrix fixtures from the compact
# table in lang/tests/codegen/e2e/__ownership_matrix__/_gen.py.  Run
# this whenever the generator's table changes; commit the produced
# om_* fixture dirs alongside the generator edit.
ownership-matrix-gen:
	PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/__ownership_matrix__/_gen.py

# Fail-fast guard that the on-disk om_* fixtures match what the
# generator would produce today.  Catches hand-edits and stale
# check-ins after a table change.
ownership-matrix-check:
	PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/__ownership_matrix__/_gen.py --check

# Run the full ownership-transfer matrix under ASAN.  Reasonable to
# keep in the inner loop: the matrix is small (~30 fixtures) and
# ASAN overhead is much lower than valgrind.  Honors DRIFT_TEST_JOBS
# via the shared e2e runner.
ownership-matrix-asan:
	@cases=$(ls lang/tests/codegen/e2e/ | grep '^om_' | tr '\n' ' '); \
	DRIFT_ASAN=1 PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py --summarize $cases

# Run the HIGH-RISK subset under DRIFT_MEMCHECK (valgrind).
# "High-risk" = heap-backed String + DiagnosticEntry combos, where
# any missing retain/release shows up as a leak or UAF.  The full
# matrix under memcheck is deferred to a nightly / cert lane; per-
# shard memcheck budget would be too high.
ownership-matrix-memcheck:
	@cases=$(ls lang/tests/codegen/e2e/ | grep -E '^om_.*(string_heap_concat|diag_entry|token)$$' | tr '\n' ' '); \
	DRIFT_MEMCHECK=1 PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py --summarize $cases

# Run the package-boundary ownership matrix (pkgb_* fixtures).
# Each fixture builds a signed producer .dmp from its `producer/`
# subdir plus the signed stdlib, then compiles the consumer
# `main.drift` against both.  Exercises ownership shapes that only
# exist on the boundary: imported copy_status / is_bitcopy,
# struct/variant field metadata reconstruction, generic variant
# tombstone metadata after package linking, Result<T, E> identity /
# visibility across boundary.
ownership-matrix-pkgb:
	PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/__ownership_matrix__/pkgb_runner.py --summarize

ownership-matrix-pkgb-asan:
	DRIFT_ASAN=1 PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/__ownership_matrix__/pkgb_runner.py --summarize

ownership-matrix-pkgb-memcheck:
	DRIFT_MEMCHECK=1 PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/__ownership_matrix__/pkgb_runner.py --summarize

# Shard 3: deploy tooling + package-consumer e2e (signed package path)
# + ownership-matrix package-boundary (per-fixture signed producer).
test-shard-3: drift-deploy-test ext-e2e-smoke ext-e2e-boundary ownership-matrix-pkgb
	@echo "lang test-shard-3: Success."

# Local build/release prep (no implicit full test run).
#
# trust-v1: the legacy `dist-publish-stdlib` step is gone -- it
# depended on `drift sign` / `drift publish` (deleted v0 CLI
# surface).  Local stdlib distribution returns when the v1 fetch /
# vendor slice lands.  For now `just build` just prebuilds the
# runtime archives; a full self-contained distribution comes from
# `just deploy` (which consumes a pre-signed Foundation author
# claim).
build: runtime-libs

lang-stage1-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	if ./.venv/bin/python3 -c "import xdist" >/dev/null 2>&1; then \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}" --dist=worksteal -v lang/tests/stage1; \
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
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}" --dist=worksteal -v lang/tests/stage2; \
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
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}" --dist=worksteal -v lang/tests/stage3; \
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
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}" --dist=worksteal -v lang/tests/stage4; \
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
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}" --dist=worksteal -v lang/tests/parser; \
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
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}" --dist=worksteal -v lang/tests/core; \
	else \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang/tests/core; \
	fi

# Package linker/consumer unit tests.
lang-packages-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	if ./.venv/bin/python3 -c "import xdist" >/dev/null 2>&1; then \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}" --dist=worksteal -v lang/tests/packages; \
	else \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang/tests/packages; \
	fi

# Trait resolution/overload tests.
lang-traits-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	if ./.venv/bin/python3 -c "import xdist" >/dev/null 2>&1; then \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}" --dist=worksteal -v lang/tests/traits; \
	else \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang/tests/traits; \
	fi

# Type checker tests (typed HIR + resolution).
lang-type-checker-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	if ./.venv/bin/python3 -c "import xdist" >/dev/null 2>&1; then \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}" --dist=worksteal -v lang/tests/type_checker; \
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
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}" --dist=worksteal -v lang/tests/method_registry; \
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
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${DRIVER_JOBS:-${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}}" --dist=worksteal -v lang/tests/driver; \
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
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}" --dist=worksteal -v lang/codegen/llvm/tests; \
	else \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang/codegen/llvm/tests; \
	fi
	# Run clang-based IR cases (per-case dirs under lang/codegen/ir_cases).
	PYTHONPATH=. ./.venv/bin/python3 lang/codegen/ir_cases/e2e_runner.py
	# Run Drift-source e2e cases (per-case dirs under lang/tests/codegen/e2e).
	# Freshness of generated om_* fixtures is guarded by the
	# `ownership-matrix-check` target, listed as a direct dep on both
	# `test` and `test-shard-2` — `lang-codegen-test` itself stays
	# focused on just running codegen tests.
	PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py --summarize

# Run e2e codegen suite through a locally-staged PEX --scie eager artifact.
# Builds the PEX binary + compiler layout in build/pex-staging/, then
# runs each e2e case by invoking the PEX binary via subprocess.
# Requires: pex (in .venv), clang.  Does not require just deploy.
lang-codegen-test-pex:
	#!/usr/bin/env bash
	set -euo pipefail
	STAGING="$PWD/build/pex-staging"
	rm -rf "${STAGING}"
	mkdir -p "${STAGING}/bin"
	CLANG="$(command -v clang 2>/dev/null || true)"
	if [[ -z "${CLANG}" ]]; then
		echo "error: clang not found in PATH" >&2
		exit 1
	fi
	echo "[pex-e2e] building PEX artifact in ${STAGING}..."
	PYTHONPATH=. DEPLOY_DIST="${STAGING}" ./.venv/bin/python3 -c 'import os; from pathlib import Path; from tools.deploy.steps.pex import build_driftc_pex, build_drift_pex; from tools.deploy.steps.bundle import bundle_compiler, bundle_runtime_archives; repo = Path(".").resolve(); dist = Path(os.environ["DEPLOY_DIST"]); build_driftc_pex(repo, dist); build_drift_pex(repo, dist); bundle_compiler(repo, dist); bundle_runtime_archives(repo, dist)'
	# Write a minimal empty v1 core trust store so the PEX binary
	# does not fail on `load_core_trust_store()`.  The local PEX e2e
	# uses `--stdlib-root` (source mode, not signed packages), so no
	# real trust entries are needed.  Filename is `core_trust_v1.json`
	# per the v1 loader (`lang/driftc/packages/trust_v1.py:337`).
	mkdir -p "${STAGING}/lib/compiler/lang/driftc/packages"
	printf '{"format":"drift-trust","version":1,"keys":{},"namespaces":{},"revoked":[]}' \
		> "${STAGING}/lib/compiler/lang/driftc/packages/core_trust_v1.json"
	echo "[pex-e2e] running e2e suite through PEX artifact..."
	rm -rf build/tests/pex_e2e
	PEX_E2E_JOBS="${PEX_E2E_JOBS:-$(( $(nproc 2>/dev/null || echo 4) / 2 ))}"
	PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/pex_e2e_runner.py \
		--driftc "${STAGING}/bin/driftc" --summarize --blocking \
		-j "${PEX_E2E_JOBS}"

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
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}" --dist=worksteal -v lang/tests/borrow_checker; \
	else \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang/tests/borrow_checker; \
	fi

# Stdlib-as-package memcheck: builds stdlib as a signed .dmp and runs
# consumer programs under Valgrind against the package path (not --stdlib-root).
# This exercises the PEX/deploy code path where has_drop/destructor_fns
# timing can cause missing scope drops.
lang-memcheck-stdlib-pkg:
	DRIFT_MEMCHECK=1 PYTHONPATH=. ./.venv/bin/python3 -m pytest -xvs lang/tests/driver/test_stdlib_as_package.py

# Memcheck suite: valgrind-only leak regression tests.
# These are excluded from normal `just test` via pytest.ini norecursedirs.
test-memcheck:
	#!/usr/bin/env bash
	set -euo pipefail
	if ! command -v valgrind >/dev/null 2>&1; then
		echo "error: valgrind not found in PATH" >&2
		exit 1
	fi
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -xvs lang/tests/memcheck

# Drift deploy/build tooling tests (manifest, lockfile, build, prepare, resolver units).
#
# trust-v1: the legacy v0 deploy test files were deleted (their security
# half is now covered by the v1 contract suite in `lang/tests/packages/`
# -- test_author_claim_v1, test_cert_claim_v1, test_verify_v1,
# test_v1_adversarial, test_c3_invariants).  The non-trust unit pieces
# (semver + resolver-conflict) live in `test_resolver_unit.py`.
drift-deploy-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v tools/drift_deploy/test_build.py tools/drift_deploy/test_drift_lock.py tools/drift_deploy/test_manifest.py tools/drift_deploy/test_prepare.py tools/drift_deploy/test_resolver_unit.py

# External consumer fleet (signed package path, K4/K10-K14 guards).
ext-consumer-test:
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang/tests/driver/test_external_consumer.py

# Nightly: full driver suite + complete package-consumer e2e (blocking).
# ASAN: DRIFT_ASAN=1 just ext-consumer-test-nightly
ext-consumer-test-nightly:
	if ./.venv/bin/python3 -c "import xdist" >/dev/null 2>&1; then \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -n "${DRIVER_JOBS:-${PYTEST_JOBS:-{{PYTEST_AUTO_JOBS}}}}" --dist=worksteal -v lang/tests/driver; \
	else \
	  PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang/tests/driver; \
	fi
	PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/pkg_consumer_runner.py --blocking --summarize

# Package-consumer e2e: report-only (all stdlib-importing tests through signed package path).
ext-e2e-report:
	PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/pkg_consumer_runner.py --summarize

# Package-consumer e2e: blocking smoke subset (CI gate).
# ASAN: DRIFT_ASAN=1 just ext-e2e-smoke
ext-e2e-smoke:
	PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/pkg_consumer_runner.py \
		--blocking --only-cases result_ok_array_match_move_no_double_free,array_push_move_non_copy_implicit,array_pop_move_out_non_copy,match_wildcard_owned_payload_drop,abi_entrypoint_cross_module_call,abi_entrypoint_cross_module_struct_ok,std_core_string_from_utf8_bytes_api,std_runtime_scoped_stack_basic,std_io_preamble_installs_stdio,try_wrap_result_err_twice_min

# Package-consumer boundary regressions (CI gate).
# Cases that exercise package-specific codepaths (trait scope through boundary,
# visibility negatives) through the pkg_consumer_runner.
# ASAN: DRIFT_ASAN=1 just ext-e2e-boundary
ext-e2e-boundary:
	PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/pkg_consumer_runner.py \
		--blocking --only-cases trait_iter_next_visibility,vis_source_trait_scope_rejected

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

# `dist-init` / `dist-publish` / `dist-index` recipes deleted in the
# trust-v1 cutover: they drove the v0 local-repo distribution flow
# (`dist/release/index.json` + `drift sign` + `drift publish`),
# all of which depended on now-deleted CLI surface.  Local stdlib
# distribution returns when the v1 fetch / vendor slice lands;
# until then, use `just deploy --dest <path>` (which produces a
# self-contained Drift distribution with v1 trust artifacts).

# Prebuild runtime archives used by driftc/e2e archive-link mode.
runtime-libs CLANG="":
	#!/usr/bin/env bash
	set -euo pipefail
	clang_bin="{{CLANG}}"
	if [[ -z "${clang_bin}" ]]; then
		clang_bin="$(command -v clang || true)"
	fi
	if [[ -z "${clang_bin}" ]]; then
		echo "clang not found (pass CLANG=... or install clang)" >&2
		exit 1
	fi
	DRIFT_RUNTIME_CLANG="${clang_bin}" PYTHONPATH=. ./.venv/bin/python3 -c "from pathlib import Path; import os; from lang.language_runtime import build_runtime_archive; root=Path('.').resolve(); clang=os.environ['DRIFT_RUNTIME_CLANG']; [print(build_runtime_archive(root, clang=clang, variant=v)) for v in ('default','debug','asan','alloc_track','optimized')]"

# `dist-publish-stdlib` / `dist-publish-stdlib-unsigned` recipes
# deleted in the trust-v1 cutover: both invoked `drift sign` /
# `drift publish` (gone v0 CLI).  Stdlib distribution is produced
# by `just deploy --dest <path>`, which consumes a Foundation
# author claim as input and emits v1 sidecars on the deploy side.

# Deploy a versioned, self-contained Drift distribution.
#
# trust-v1: deploy plays the **certifier role only**.  Required inputs:
#
#   --stdlib-author-claim    <path>     # externally-produced std.author-claim
#                                       # (the deploy host never holds the
#                                       # author private key)
#   --stdlib-author-pubkey-b64 <base64>  # matching Foundation author pubkey
#   --certifier-key-file     <path>     # OPTIONAL: certifier seed used to
#                                       # sign std.cert-claim; falls back to
#                                       # $DRIFT_SIGN_KEY_FILE when omitted
#
# The same on-disk seed MAY be used for both `drift-author publish` and the
# certifier step here -- v1's role split is about which claim body is
# signed at which step, not about forcing two distinct keys (see
# docs/design/trust-v1.md §7.5).
#
# Examples:
#   # Production: Foundation-released author artifacts + a release-host
#   # certifier seed configured via env.
#   export DRIFT_SIGN_KEY_FILE=/var/lib/drift-deploy/certifier.seed
#   just deploy --dest ~/opt/drift \
#       --stdlib-author-claim ~/.config/drift/foundation/std.author-claim \
#       --stdlib-author-pubkey-b64 <base64-32-bytes>
#
#   # Dev: pre-publish locally via `just deploy-prepublish-stdlib-author`
#   # (see below), then run `just deploy --dest ...` without re-typing
#   # the long flags (the helper exports the paths for you).  The certifier
#   # key can either point at DRIFT_SIGN_KEY_FILE or share the same seed
#   # the helper used for `drift-author publish` -- both are policy-allowed.
deploy *ARGS:
	#!/usr/bin/env bash
	set -euo pipefail
	args=({{ARGS}})
	# Strip leading '--' so "just deploy -- --dest ..." works.
	if [[ ${#args[@]} -gt 0 && "${args[0]}" == "--" ]]; then
		args=("${args[@]:1}")
	fi
	# Pick up dev-published author identity from env ONLY when the
	# caller didn't pass the corresponding flag explicitly.  Explicit
	# CLI args always win -- argparse uses the LAST occurrence of a
	# repeated flag, so appending env values unconditionally would
	# silently override the user's explicit choice.
	have_claim_flag=0
	have_pubkey_flag=0
	for a in "${args[@]}"; do
		case "${a}" in
			--stdlib-author-claim|--stdlib-author-claim=*)
				have_claim_flag=1 ;;
			--stdlib-author-pubkey-b64|--stdlib-author-pubkey-b64=*)
				have_pubkey_flag=1 ;;
		esac
	done
	if [[ ${have_claim_flag} -eq 0 && -n "${DRIFT_STDLIB_AUTHOR_CLAIM:-}" ]]; then
		args+=( --stdlib-author-claim "${DRIFT_STDLIB_AUTHOR_CLAIM}" )
	fi
	if [[ ${have_pubkey_flag} -eq 0 && -n "${DRIFT_STDLIB_AUTHOR_PUBKEY_B64:-}" ]]; then
		args+=( --stdlib-author-pubkey-b64 "${DRIFT_STDLIB_AUTHOR_PUBKEY_B64}" )
	fi
	PYTHONPATH=. exec ./.venv/bin/python3 tools/deploy/deploy.py "${args[@]}"


# Dev helper: pre-publish a stdlib author claim using an *ephemeral
# local* Foundation-stand-in key.  Writes the claim + pubkey to
# `build/foundation-stub/`, prints export lines for the env vars
# `just deploy` consumes.  The seed is discarded after signing.
#
# This recipe is for DEV ONLY.  Production releases must use a real
# Foundation author identity produced outside this checkout (offline
# signing, signing service, or pre-committed release artifact).
deploy-prepublish-stdlib-author:
	#!/usr/bin/env bash
	set -euo pipefail
	mkdir -p build/foundation-stub
	PYTHONPATH=. ./.venv/bin/python3 - <<'PY'
	import base64, os
	from pathlib import Path
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
	from cryptography.hazmat.primitives import serialization
	from lang.driftc.packages.source_content_id import compute_artifact_source_content_id
	from lang.driftc.packages.author_claim_v1 import AuthorClaimBody
	from tools.drift_author.author_publish import SignAuthorClaimOptions, sign_and_write_author_claim
	from lang.versions import DRIFTC_VERSION

	root = Path.cwd()
	stdlib = root / "stdlib"
	module_paths_rel = sorted(str(p.relative_to(root)) for p in stdlib.rglob("*.drift"))
	sci = compute_artifact_source_content_id(
		kind="library", package_id="std", version=DRIFTC_VERSION,
		module_namespace="std", entry_module="std",
		module_paths=module_paths_rel,
		package_deps=[], native_deps=[], unsafe=False, asset_paths=[],
		target_class="drift-dev", source_root=root,
	)
	priv = Ed25519PrivateKey.generate()
	seed = priv.private_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PrivateFormat.Raw,
		encryption_algorithm=serialization.NoEncryption(),
	)
	pub_raw = priv.public_key().public_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PublicFormat.Raw,
	)
	pub_b64 = base64.b64encode(pub_raw).decode("ascii")
	out_dir = root / "build" / "foundation-stub"
	out_dir.mkdir(parents=True, exist_ok=True)
	claim_path = sign_and_write_author_claim(SignAuthorClaimOptions(
		body=AuthorClaimBody(
			schema_version=1, package_id="std", version=DRIFTC_VERSION,
			namespaces=("std.*", "lang.*", "drift.*"),
			source_content_id=sci, required_deps=(),
			target_class="library", release_utc="2026-05-19T00:00:00Z",
		),
		seed32=seed, sidecar_dir=out_dir,
	))
	(out_dir / "author.pubkey.b64").write_text(pub_b64 + "\n", encoding="utf-8")
	del priv, seed
	print(f"# Foundation author claim (DEV STUB) written to: {claim_path}")
	print(f"# Pubkey saved to: {out_dir / 'author.pubkey.b64'}")
	print(f"export DRIFT_STDLIB_AUTHOR_CLAIM={claim_path}")
	print(f"export DRIFT_STDLIB_AUTHOR_PUBKEY_B64={pub_b64}")
	PY

# Print shell env lines for an existing deployment.
deploy-print-env DEST:
	#!/usr/bin/env bash
	set -euo pipefail
	dest="{{DEST}}"
	if [[ ! -x "${dest}/bin/driftc" ]]; then
		echo "error: no deployment found at ${dest} (missing bin/driftc)" >&2
		exit 1
	fi
	version="$("${dest}/bin/driftc" --version 2>/dev/null || echo unknown)"
	echo "# Drift distribution: ${version}"
	echo "export PATH=\"${dest}/bin:\$PATH\""
