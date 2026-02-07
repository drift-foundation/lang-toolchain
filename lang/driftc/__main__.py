# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""
CLI entrypoint for `python -m lang.driftc`.
"""

from .driftc import main

if __name__ == "__main__":
	import sys
	sys.exit(main())
