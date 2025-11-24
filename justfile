set shell := ["bash", "-lc"]

test: parse-all
	python3 tools/draft_linter.py tests/programs
	python3 tests/run_tests.py

parse-all: parse-playground parse-examples
	@echo "Parsing successful."

# Parse every Drift snippet under playground/ to ensure the grammar accepts them.
parse-playground:
	python3 tools/draft_linter.py playground
	python3 tools/parse_playground.py

# Parse every Drift example under examples/
parse-examples:
	python3 tools/draft_linter.py examples
	python3 tools/parse_playground.py examples

test-codegen:
	./.venv/bin/python3 tools/test-llvm/test_codegen.py tools/test-llvm/out/add.o
	clang-15 -c tools/test-llvm/main.c -o tools/test-llvm/out/main.o
	clang-15 tools/test-llvm/out/main.o tools/test-llvm/out/add.o -o tools/test-llvm/out/add_test
	./tools/test-llvm/out/add_test
