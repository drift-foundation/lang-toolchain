from __future__ import annotations

import json
import os
import sys
from typing import Any

_cached_flags: dict[str, bool] | None = None


def _parse_debug_flags() -> dict[str, bool]:
	# Internal compiler debug-flag channel.  Distinct from `DRIFT_DEBUG`,
	# which is the user-facing dual-runtime lane selector — these two
	# concepts share neither vocabulary nor consumer.  Set
	# `DRIFT_COMPILER_DEBUG=1` to enable every internal flag, or
	# `DRIFT_COMPILER_DEBUG='{"convergence_parity": true}'` to opt into a
	# specific named flag.
	raw = os.environ.get("DRIFT_COMPILER_DEBUG")
	if not raw:
		return {}
	if raw in ("1", "true", "True", "YES", "yes"):
		return {"*": True}
	try:
		data = json.loads(raw)
	except Exception as exc:
		print(f"[drift:debug] invalid DRIFT_COMPILER_DEBUG JSON: {exc}", file=sys.stderr)
		return {}
	if not isinstance(data, dict):
		print("[drift:debug] DRIFT_COMPILER_DEBUG must be a JSON object", file=sys.stderr)
		return {}
	flags: dict[str, bool] = {}
	for key, val in data.items():
		if isinstance(val, bool):
			flags[str(key)] = val
	return flags


def _flags() -> dict[str, bool]:
	global _cached_flags
	if _cached_flags is None:
		_cached_flags = _parse_debug_flags()
	return _cached_flags


def enabled(name: str) -> bool:
	flags = _flags()
	if flags.get("*"):
		return True
	return bool(flags.get(name))


def enabled_any(*names: str) -> bool:
	return any(enabled(name) for name in names)
