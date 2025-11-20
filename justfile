set shell := ["bash", "-lc"]

parse-all: parse-playground parse-examples
	@echo "Done."

# Parse every Drift snippet under playground/ to ensure the grammar accepts them.
parse-playground:
	python3 tools/parse_playground.py

# Parse every Drift example under examples/
parse-examples:
	python3 tools/parse_playground.py examples
