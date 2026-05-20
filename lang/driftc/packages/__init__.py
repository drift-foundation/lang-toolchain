# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Package artifacts and package-root module providers.

Trust binding is v1-only: every loaded `.dmp` is validated against
the role-tagged trust store (`trust_v1`) by `provider_v1`, which
composes the author claim (`<pkg>.author-claim`) and the cert
claim (`<pkg>.cert-claim.<kid>.json`) sidecars produced by
`drift-author publish` and `drift-deploy` respectively.  The
pre-v1 `.sig` envelope path is gone.
"""

from __future__ import annotations

__all__ = [
	"dmir_pkg_v0",
	"provisional_dmir_v0",
	"provider_v1",
	"trust_v1",
]
