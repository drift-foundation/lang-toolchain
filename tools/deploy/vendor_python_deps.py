#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.metadata as metadata
import re
import shutil
import sys
from pathlib import Path


_REQ_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+")


def _normalize_name(name: str) -> str:
	return name.lower().replace("-", "_").replace(".", "_")


def _parse_runtime_requirement(raw: str) -> str | None:
	spec, sep, marker = raw.partition(";")
	if sep and "extra ==" in marker:
		return None
	match = _REQ_NAME_RE.match(spec.strip())
	if not match:
		return None
	return match.group(0)


def _collect_distributions(root_requirements: list[str]) -> list[metadata.Distribution]:
	selected: list[metadata.Distribution] = []
	seen: set[str] = set()
	pending = list(root_requirements)
	while pending:
		req_name = pending.pop(0)
		dist = metadata.distribution(req_name)
		dist_name = dist.metadata.get("Name", req_name)
		norm_name = _normalize_name(dist_name)
		if norm_name in seen:
			continue
		seen.add(norm_name)
		selected.append(dist)
		for raw_req in dist.requires or []:
			parsed = _parse_runtime_requirement(raw_req)
			if parsed is not None:
				pending.append(parsed)
	return selected


def _copy_distribution(dist: metadata.Distribution, dest_root: Path) -> None:
	files = dist.files or []
	for rel_path in files:
		parts = rel_path.parts
		if any(part == ".." for part in parts):
			raise RuntimeError(f"refusing to copy parent-relative path from distribution {dist.metadata.get('Name')}: {rel_path}")
		src = Path(dist.locate_file(rel_path))
		if not src.is_file():
			continue
		dest = dest_root / Path(*parts)
		dest.parent.mkdir(parents=True, exist_ok=True)
		shutil.copy2(src, dest)


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Vendor installed Python distributions into a deploy tree.")
	parser.add_argument("--dest", required=True, help="Destination directory inside the staged deploy tree")
	parser.add_argument("requirements", nargs="+", help="Root distribution names to vendor")
	args = parser.parse_args(argv)

	dest_root = Path(args.dest).resolve()
	dest_root.mkdir(parents=True, exist_ok=True)

	for dist in _collect_distributions(args.requirements):
		_copy_distribution(dist, dest_root)
	return 0


if __name__ == "__main__":
	sys.exit(main())
