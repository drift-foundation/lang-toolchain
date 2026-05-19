# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""`python -m tools.drift_author` entry point."""

from __future__ import annotations

import sys

from tools.drift_author.cli import main


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))
