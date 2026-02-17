#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


def _physical_cpu_count_linux() -> int | None:
	cpuinfo = Path("/proc/cpuinfo")
	if not cpuinfo.exists():
		return None
	blocks = [b for b in cpuinfo.read_text(encoding="utf-8", errors="ignore").split("\n\n") if b.strip()]
	cores: set[tuple[str, str]] = set()
	for block in blocks:
		physical_id: str | None = None
		core_id: str | None = None
		for line in block.splitlines():
			if ":" not in line:
				continue
			k, v = line.split(":", 1)
			key = k.strip().lower()
			val = v.strip()
			if key == "physical id":
				physical_id = val
			elif key == "core id":
				core_id = val
		if physical_id is not None and core_id is not None:
			cores.add((physical_id, core_id))
	if cores:
		return len(cores)
	return None


def recommended_workers() -> int:
	physical = _physical_cpu_count_linux()
	if physical is not None and physical > 0:
		return max(1, physical)
	import os
	logical = os.cpu_count() or 1
	return max(1, logical // 2)


def main() -> int:
	print(recommended_workers())
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
