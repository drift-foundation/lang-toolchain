# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class SourceFile:
	file_id: int
	path: Optional[str]
	text: str


class SourceManager:
	"""
	In-memory source buffer registry for a single compilation.

	Stores the original source text so later phases can slice exact spans
	without re-reading from disk.
	"""

	def __init__(self) -> None:
		self._files: Dict[int, SourceFile] = {}
		self._path_to_id: Dict[str, int] = {}
		self._next_id: int = 1

	def add(self, path: Optional[str], text: str) -> int:
		if path is not None:
			existing = self._path_to_id.get(path)
			if existing is not None:
				return existing
		file_id = self._next_id
		self._next_id += 1
		self._files[file_id] = SourceFile(file_id=file_id, path=path, text=text)
		if path is not None:
			self._path_to_id[path] = file_id
		return file_id

	def get(self, file_id: int) -> Optional[SourceFile]:
		return self._files.get(file_id)

	def file_id_for_path(self, path: Optional[str]) -> Optional[int]:
		if path is None:
			return None
		return self._path_to_id.get(path)

	def slice(self, file_id: int, start_pos: int, end_pos: int) -> Optional[str]:
		source = self._files.get(file_id)
		if source is None:
			return None
		if start_pos < 0 or end_pos < 0:
			return None
		if end_pos < start_pos:
			return None
		return source.text[start_pos:end_pos]


__all__ = ["SourceFile", "SourceManager"]
