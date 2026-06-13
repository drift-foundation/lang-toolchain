#ifndef DRIFT_FS_RUNTIME_H
#define DRIFT_FS_RUNTIME_H

#include <stdint.h>

#include "string_runtime.h"

/* Directory-walk backing `std.fs.read_dir` (POSIX), VT-safe by offload.
 *
 * The ENTIRE walk — opendir, the readdir enumeration, per-entry UTF-8
 * validation, fstatat(..., AT_SYMLINK_NOFOLLOW) classification, and closedir —
 * runs on ONE job on the shared blocking-syscall pool (posix/blocking_pool.h)
 * while the calling VT is parked, so a stalling NFS/FUSE/autofs directory can
 * never block a carrier thread.  Only drift_fs_read_dir touches the filesystem;
 * every drift_fs_result_* accessor is a pure in-memory read of the already
 * materialized snapshot and is safe to call on a carrier.
 *
 * drift_fs_read_dir returns a result handle >= 1 (a snapshot OR a resolved
 * error, query via the accessors), or a frozen negative sentinel.  The sentinels
 * are distinct so callers can tell runtime conditions apart from a filesystem
 * errno:
 *   -1  ENOMEM (allocation failed; no handle)
 *   -2  saturation (blocking-pool queue full -> backpressure; no handle)
 *   -4  timed out (deadline fired before completion; abandoned; no handle)
 *   -5  cancelled (the VT was cancelled; abandoned; no handle)
 * On -4/-5 the worker keeps the snapshot and frees it when its syscall finally
 * returns.  Negative sentinels carry no handle and need no free.
 *
 * deadline_ms is an ABSOLUTE monotonic-millisecond deadline (thread.now_ms() +
 * timeout), or 0 for no deadline. */

int64_t drift_fs_read_dir(DriftString path, int64_t deadline_ms);

/* Result status: 0 ok (snapshot present) / 1 errno error / 2 invalid-utf8. */
int64_t drift_fs_result_status(int64_t handle);

/* errno (status 1) or EILSEQ (status 2). */
int64_t drift_fs_result_errno(int64_t handle);

/* Entry count (status 0). */
int64_t drift_fs_result_count(int64_t handle);

/* UTF-8-validated name of entry `idx` (fresh DriftString, owned by Drift). */
DriftString drift_fs_result_name(int64_t handle, int64_t idx);

/* FileKind code of entry `idx`: 0 File / 1 Dir / 2 Symlink / 3 Other / 4 Unknown. */
int64_t drift_fs_result_kind(int64_t handle, int64_t idx);

/* Release the VT's stake on the result; frees the snapshot when the last
 * stake drops.  Returns 0. */
int64_t drift_fs_result_free(int64_t handle);

/* Test-only: count of directory walks that have entered a pool worker.  Used as
 * a worker-entry barrier in the saturation regression. */
int64_t drift_fs_test_walk_entries(void);

#endif  // DRIFT_FS_RUNTIME_H
