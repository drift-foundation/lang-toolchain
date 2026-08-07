"""Deterministic stdlib-only zipapp builder for the baton tool.

Produces bin/baton — a self-contained executable Python archive whose bytes
depend only on the packaged sources (fixed timestamps, sorted entries, no
compiled artifacts), so rebuilding from the same inputs yields the same
sha256. Writes DISTRIBUTION.json beside it with the version/floor manifest.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
FIXED_DATE = (2020, 1, 1, 0, 0, 0)  # deterministic member timestamp

BOOTSTRAP = '''\
# Zipapp bootstrap. This file must PARSE on old interpreters (syntax kept to
# ~Python 3.6) so the floor diagnostics below are reachable instead of a
# SyntaxError. Exit code 2 is the documented environment-floor result.
import sys

if sys.version_info < (3, 11):
    sys.stderr.write(
        "baton: Python %d.%d is below the required 3.11 floor\\n"
        % (sys.version_info[0], sys.version_info[1]))
    sys.exit(2)

try:
    import sqlite3  # noqa: F401
except ImportError:
    sys.stderr.write("baton: the Python sqlite3 module is unavailable\\n")
    sys.exit(2)

from baton_v6 import main

sys.exit(main())
'''


def build(root: str) -> dict:
	"""ONE distribution-root contract: `root` receives DISTRIBUTION.json at
	its top and the executable at `bin/baton`; the manifest's `artifact`
	field is that root-relative path. The schema/example/docs assets live
	beside the manifest in a checked-in root (the package directory itself
	is the canonical root)."""
	source_path = os.path.join(HERE, "baton_v6.py")
	with open(source_path, "rb") as handle:
		source = handle.read()
	members = [
		("__main__.py", BOOTSTRAP.encode("utf-8")),
		("baton_v6.py", source),
	]
	buffer = io.BytesIO()
	buffer.write(b"#!/usr/bin/env python3\n")
	with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
		for name, data in sorted(members):
			info = zipfile.ZipInfo(name, date_time=FIXED_DATE)
			info.external_attr = 0o644 << 16
			archive.writestr(info, data)
	payload = buffer.getvalue()
	bin_dir = os.path.join(root, "bin")
	os.makedirs(bin_dir, exist_ok=True)
	target = os.path.join(bin_dir, "baton")
	with open(target, "wb") as handle:
		handle.write(payload)
	os.chmod(target, os.stat(target).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

	# A complete deployment ships the generic protocol document beside the
	# executable; the manifest records and pins it.
	proto_name = "AGENTS-MAILBOX-PROTO.md"
	with open(os.path.join(HERE, proto_name), "rb") as handle:
		proto = handle.read()
	proto_target = os.path.join(root, proto_name)
	if os.path.abspath(proto_target) != os.path.join(HERE, proto_name):
		with open(proto_target, "wb") as handle:
			handle.write(proto)

	sys.path.insert(0, HERE)
	import baton_v6
	manifest = {
		"tool": "baton",
		"tool_version": baton_v6.TOOL_VERSION,
		"protocol_version": baton_v6.PROTOCOL_VERSION,
		"python_min": "3.11",
		"sqlite_min": ".".join(map(str, baton_v6.SQLITE_MIN)),
		"artifact": "bin/baton",
		"artifact_sha256": hashlib.sha256(payload).hexdigest(),
		"source_sha256": hashlib.sha256(source).hexdigest(),
		"protocol_doc": proto_name,
		"protocol_doc_sha256": hashlib.sha256(proto).hexdigest(),
	}
	manifest_path = os.path.join(root, "DISTRIBUTION.json")
	with open(manifest_path, "w") as handle:
		json.dump(manifest, handle, indent=2, sort_keys=True)
		handle.write("\n")
	return manifest


if __name__ == "__main__":
	out = sys.argv[1] if len(sys.argv) > 1 else HERE
	print(json.dumps(build(out), indent=2, sort_keys=True))
