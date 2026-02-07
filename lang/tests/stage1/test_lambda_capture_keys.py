# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.stage1.closures import (
	HCapture,
	HCaptureKey,
	HCaptureKind,
	HCaptureProj,
	sort_captures,
)
from lang.driftc.core.span import Span


def test_capture_ordering_is_deterministic() -> None:
	"""
	Captures sort by (root id, projection length, projection lexicographic).
	"""
	# root 2, field y
	c1 = HCapture(
		kind=HCaptureKind.REF,
		key=HCaptureKey(root_local=2, proj=(HCaptureProj(field="y"),)),
		span=Span(),
	)
	# root 1, no projection
	c2 = HCapture(
		kind=HCaptureKind.REF,
		key=HCaptureKey(root_local=1, proj=()),
		span=Span(),
	)
	# root 1, field b
	c3 = HCapture(
		kind=HCaptureKind.REF,
		key=HCaptureKey(root_local=1, proj=(HCaptureProj(field="b"),)),
		span=Span(),
	)
	sorted_caps = sort_captures([c1, c2, c3])
	assert [c.key for c in sorted_caps] == [c2.key, c3.key, c1.key]
