# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
drift inspect — read-only artifact inspection.

Current subcommand:

    drift inspect build-info <binary> [--json]

Prints the drift-build-info/v1 document embedded in an executable's
`.drift_build_info` section (work/toolchain-meta-stamps PLAN §2.4).
This is the SUPPORTED gate read path: certification gates read stamps
from binaries without executing them and without a host-binutils
dependency (the reader parses the ELF container directly — guardrail
G2). Standard binutils remain a manual convenience
(`objcopy -O binary --only-section=.drift_build_info <bin> /dev/stdout`)
but never the gate contract.

Fail-closed contract (every row → exit 1, EMPTY stdout, stderr
diagnostic): file missing/not ELF/unsupported class; section table
malformed or out of bounds; section MISSING; DUPLICATE sections
(exactly one is the contract); content empty, oversized (1 MiB cap,
enforced before decoding), invalid UTF-8, invalid JSON, schema
violations, or non-canonical encoding. The inspected binary is NEVER
executed.

Success output:
  --json    the section's exact canonical bytes plus one newline —
            machine consumers pipe this into a JSON parser;
  default   a pretty-printed rendering for humans.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lang.build_info import BuildInfoError, extract_build_info


def build_arg_parser() -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(
		prog="drift inspect build-info",
		description=(
			"Print the drift-build-info/v1 document embedded in an "
			"executable's .drift_build_info section. Pure file "
			"reading: the binary is never executed, and no external "
			"tools (readelf/objdump) are involved. Fails closed (exit "
			"1, empty stdout) on a missing or duplicate section and "
			"on any malformed, oversized, or non-canonical content."
		),
	)
	p.add_argument("binary", type=Path,
	               help="Path to the executable to inspect.")
	p.add_argument("--json", action="store_true",
	               help="Emit the section's exact canonical bytes plus one "
	                    "newline (default: pretty-print for humans).")
	return p


def run(argv: list[str] | None = None) -> int:
	parser = build_arg_parser()
	args = parser.parse_args(argv)
	try:
		text = extract_build_info(args.binary)
	except BuildInfoError as e:
		print(f"error: {e}", file=sys.stderr)
		return 1
	except OSError as e:
		print(f"error: cannot read {args.binary}: {e}", file=sys.stderr)
		return 1
	if args.json:
		# The exact canonical UTF-8 bytes + exactly one newline, via the
		# BINARY stdout stream — print() would re-encode through the
		# locale-dependent text stream and can traceback (or mangle)
		# under a hostile PYTHONIOENCODING.
		sys.stdout.buffer.write(text.encode("utf-8") + b"\n")
		sys.stdout.buffer.flush()
	else:
		# Same binary-stream discipline as --json: the pretty rendering
		# contains the document's own Unicode and must not depend on
		# the locale/PYTHONIOENCODING text stream.
		pretty = json.dumps(json.loads(text), indent=2, ensure_ascii=False,
		                    sort_keys=True)
		sys.stdout.buffer.write(pretty.encode("utf-8") + b"\n")
		sys.stdout.buffer.flush()
	return 0


if __name__ == "__main__":
	sys.exit(run())
