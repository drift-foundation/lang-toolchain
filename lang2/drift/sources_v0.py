# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Drift sources file (v0).

This is a local, offline-friendly description of where `drift fetch` should look
for package repositories. MVP supports directory sources only; network sources
are handled later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DirSource:
	path: Path


@dataclass(frozen=True)
class SourcesV0:
	sources: list[DirSource]


def load_sources_v0(path: Path) -> SourcesV0:
	obj = json.loads(path.read_text(encoding="utf-8"))
	if not isinstance(obj, dict):
		raise ValueError("sources file must be a JSON object")
	if obj.get("format") != "drift-sources" or obj.get("version") != 0:
		raise ValueError("unsupported sources format/version")
	raw_sources = obj.get("sources")
	if not isinstance(raw_sources, list):
		raise ValueError("sources must be a list")

	out: list[DirSource] = []
	for s in raw_sources:
		if not isinstance(s, dict):
			raise ValueError("source entry must be an object")
		kind = s.get("kind")
		if kind != "dir":
			raise ValueError("unsupported source kind (MVP supports dir only)")
		p = s.get("path")
		if not isinstance(p, str) or not p:
			raise ValueError("dir source must have a path string")
		out.append(DirSource(path=Path(p)))
	return SourcesV0(sources=out)

