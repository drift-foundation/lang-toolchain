# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
`drift` tooling layer (package manager / publisher).

Pinned boundary:
- `lang.drift.*` MUST NOT import `lang.driftc.*` (compiler internals).
- Signing/publishing workflows live here; `driftc` is the offline gatekeeper.
"""

