#!/usr/bin/env bash
# drift_janitor.sh — sweep stale Drift session-scoped scratch dirs from /tmp.
#
# Drift processes write scratch under /tmp/drift-$USER/session-* so that  # drift-tmp-root-audit: allow janitor docstring
# interrupted/OOM-killed sessions can be reclaimed safely. /tmp is tmpfs on
# most Linux setups, so unswept scratch costs RAM, not disk.
#
# Default behavior is DRY-RUN: lists what would be deleted. Pass --apply to
# actually delete. -xdev avoids crossing filesystems; -prune ensures we
# decide per-session-root and never recurse into a session we are about to
# delete.
#
# Default age threshold: 360 minutes (6 hours). Override with --minutes N.
#
# Exit status: 0 on success (including dry-run with zero or many matches);
#              non-zero only if the base dir is unreadable.

set -euo pipefail

MINUTES=360
APPLY=0
USER_NAME="${USER:-${LOGNAME:-unknown}}"
BASE="/tmp/drift-${USER_NAME}"  # drift-tmp-root-audit: allow janitor target namespace

usage() {
	cat <<EOF
Usage: $(basename "$0") [--apply] [--minutes N] [--base DIR]

Sweep Drift session scratch directories under \$BASE (default: $BASE).

Options:
  --apply         Actually delete matching session dirs. Without this flag,
                  the script prints what it would delete and exits.
  --minutes N     Only consider session dirs older than N minutes. Default: 360 (6h).
  --base DIR      Override the parent dir. Default: /tmp/drift-\$USER.  drift-tmp-root-audit: allow janitor help text
  -h, --help      Show this help.

The match pattern is hard-coded to 'session-*' under \$BASE so this script
cannot accidentally delete anything outside the Drift namespace.
EOF
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--apply) APPLY=1; shift ;;
		--minutes) MINUTES="$2"; shift 2 ;;
		--base) BASE="$2"; shift 2 ;;
		-h|--help) usage; exit 0 ;;
		*) echo "unknown arg: $1" >&2; usage; exit 2 ;;
	esac
done

if [[ ! -d "$BASE" ]]; then
	echo "[drift-janitor] base dir does not exist: $BASE (nothing to do)"
	exit 0
fi

# -xdev: never cross filesystem boundary (defense if /tmp is bind-mounted).
# -mindepth 1 -maxdepth 1: only consider direct children of $BASE.
# -type d -name 'session-*': only Drift-prefixed session dirs.
# -mmin +N: older than N minutes.
# -prune: do not descend into matched dirs before acting.

if [[ "$APPLY" -eq 1 ]]; then
	echo "[drift-janitor] APPLY mode: deleting session-* under $BASE older than $MINUTES min"
	find "$BASE" -xdev -mindepth 1 -maxdepth 1 \
		-type d -name 'session-*' -mmin "+$MINUTES" -prune \
		-exec rm -rf -- {} +
	echo "[drift-janitor] done"
else
	echo "[drift-janitor] DRY-RUN: would delete the following (use --apply to remove):"
	find "$BASE" -xdev -mindepth 1 -maxdepth 1 \
		-type d -name 'session-*' -mmin "+$MINUTES" -prune \
		-print
fi
