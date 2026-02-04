set shell := ["bash", "-lc"]
set quiet
CLANG_BIN := "clang-15"

# Default task: run the staged lang2 compiler tests.
default: lang2-test

review-cleanup:
	rm -f combined_*

# Lang2 staged compiler tests
lang2-test: review-cleanup lang2-stage1-test lang2-stage2-test lang2-stage3-test lang2-stage4-test lang2-parser-test lang2-core-test lang2-llvm-test lang2-borrow-test lang2-type-checker-test lang2-method-registry-test lang2-driver-suite lang2-codegen-test
	@echo "lang2 tests: Success."

lang2-stage1-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang2/tests/stage1

lang2-stage2-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang2/tests/stage2

lang2-stage3-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang2/tests/stage3

lang2-stage4-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang2/tests/stage4

# Parser tests (lang2 parser copy + adapter).
lang2-parser-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang2/tests/parser

# Core TypeEnv/TypeTable tests.
lang2-core-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang2/tests/core

# Type checker tests (typed HIR + resolution).
lang2-type-checker-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang2/tests/type_checker

# Method registry/resolver tests.
lang2-method-registry-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang2/tests/method_registry

# Driver/integration tests (driftc pipeline, try sugar, declared events).
lang2-driver-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang2/tests/driver

lang2-driver-suite:
	# Full driver suite (lang2/tests/driver).
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang2/tests/driver

# Basic LLVM codegen smoke test (llvmlite), kept separate from pytest collection.
lang2-llvm-test:
	./.venv/bin/python3 tools/test-llvm/test_codegen.py /tmp/lang2_test_codegen.o

# LLVM textual codegen tests (SSA→LLVM IR).
lang2-codegen-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	# Clean codegen artifacts to keep cases isolated between runs.
	rm -rf build/tests/lang2
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang2/codegen/llvm/tests
	# Run clang-based IR cases (per-case dirs under lang2/codegen/ir_cases).
	PYTHONPATH=. ./.venv/bin/python3 lang2/codegen/ir_cases/e2e_runner.py
	# Run Drift-source e2e cases (per-case dirs under lang2/tests/codegen/e2e).
	PYTHONPATH=. ./.venv/bin/python3 lang2/tests/codegen/e2e/runner.py --summarize

# Lang2 e2e runner (lang2.driftc: json + run modes against tests/e2e)
lang2-e2e CASES="":
	PYTHONPATH=. ./.venv/bin/python3 lang2/codegen/codegen_runner.py {{CASES}}

# Borrow checker scaffolding tests.
lang2-borrow-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang2/tests/borrow_checker

# Build examples (lang2.driftc)
make-example EXAMPLE:
	#!/usr/bin/env bash
	set -euo pipefail
	set -x
	example="{{EXAMPLE}}"
	out_dir="build/examples/${example}"
	mkdir -p "${out_dir}"
	if [[ -f "examples/${example}/server.drift" ]] && [[ -f "examples/${example}/client.drift" ]]; then
		PYTHONPATH=. ./.venv/bin/python3 -m lang2.driftc --stdlib-root stdlib "examples/${example}/server.drift" -o "${out_dir}/server"
		PYTHONPATH=. ./.venv/bin/python3 -m lang2.driftc --stdlib-root stdlib "examples/${example}/client.drift" -o "${out_dir}/client"
	else
		out_bin="${out_dir}/example_${example}"
		extra_args=()
		if [[ "${example}" == debug_* ]]; then
			extra_args+=(--debug-info)
		fi
		PYTHONPATH=. ./.venv/bin/python3 -m lang2.driftc "${extra_args[@]}" --stdlib-root stdlib "examples/${example}/main.drift" -o "${out_bin}"
	fi

make-examples:
	#!/usr/bin/env bash
	set -euo pipefail
	set -x
	just make-example tcp_client_server
	just make-example file_io
	just make-example udp_ping
	just make-example tcp_echo
	just make-example debug_1

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
