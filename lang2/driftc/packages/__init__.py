# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Package artifacts and package-root module providers.

Milestone 4 introduces unsigned package artifacts emitted by `driftc` for local
consumption (and later signing by `drift`). `driftc` remains the final
gatekeeper at use time and must refuse to consume untrusted or incompatible
package contents.
"""

from __future__ import annotations

__all__ = [
	"dmir_pkg_v0",
	"provisional_dmir_v0",
	"provider_v0",
]
