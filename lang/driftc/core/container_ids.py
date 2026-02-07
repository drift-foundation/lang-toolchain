# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2026-01-13
"""
Canonical container IDs used in runtime error payloads.

These must match the stdlib constants (e.g., std.containers.array.ARRAY_CONTAINER_ID).
"""

ARRAY_CONTAINER_ID = "std.containers:Array"
DEQUE_CONTAINER_ID = "std.containers:Deque"
RAW_BUFFER_CONTAINER_ID = "std.mem:RawBuffer"
STRING_CONTAINER_ID = "std.core:String"
HASH_MAP_CONTAINER_ID = "std.containers:HashMap"
HASH_SET_CONTAINER_ID = "std.containers:HashSet"
