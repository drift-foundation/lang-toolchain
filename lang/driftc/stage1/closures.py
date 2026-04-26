# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Iterable

from lang.driftc.core.span import Span
from lang.driftc.stage1.hir_nodes import BindingId


class HCaptureKind(Enum):
	"""How a value is captured.

	`SHARE` (added 0.31.14): the closure construction emits
	`Call(Share::share, [&local])` and move-captures the returned
	owned value.  Outer binding remains usable.  Type must implement
	`std.core.shareable.Share`.  See `docs/design/drift-lang-spec.md` and
	`docs/effective-drift.md`.
	"""

	REF = auto()
	REF_MUT = auto()
	COPY = auto()
	MOVE = auto()
	SHARE = auto()


@dataclass(frozen=True)
class HCaptureProj:
	"""Projection segment inside a captured place (v0: fields only)."""

	field: str


@dataclass(frozen=True)
class HCaptureKey:
	"""
	Stable capture identity: root local id + normalized projections.

	`proj` ordering is significant and normalized (outermost→innermost).
	"""

	root_local: BindingId
	proj: tuple[HCaptureProj, ...] = ()

	def sort_tuple(self) -> tuple:
		"""Deterministic tuple used for sorting captures."""
		return (
			self.root_local,
			len(self.proj),
			tuple(p.field for p in self.proj),
		)


@dataclass
class HCapture:
	"""Concrete capture with kind + span for diagnostics."""

	kind: HCaptureKind
	key: HCaptureKey
	span: Span = Span()


def sort_captures(captures: Iterable[HCapture]) -> list[HCapture]:
	"""Return captures sorted by their canonical key ordering."""
	return sorted(captures, key=lambda c: c.key.sort_tuple())
