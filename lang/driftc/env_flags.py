from __future__ import annotations

import os
from typing import Mapping

_TRUE_STRINGS = frozenset({"1", "true", "yes", "on"})


def env_true(name: str, env: Mapping[str, str] | None = None) -> bool:
	source = os.environ if env is None else env
	raw = source.get(name, "")
	return raw.strip().lower() in _TRUE_STRINGS

