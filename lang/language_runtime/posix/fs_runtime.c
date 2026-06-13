/* Directory-walk backing `std.fs.read_dir` (POSIX), offloaded to the shared
 * blocking-syscall pool so it never blocks a VT carrier.  See fs_runtime.h for
 * the boundary contract and posix/blocking_pool.h for the job/refcount model. */
#include "fs_runtime.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>

#include "blocking_pool.h"

/* FileKind codes — must match the Drift-side `kind_of` mapping in
 * stdlib/std/fs/fs.drift. */
#define DRIFT_FK_FILE 0
#define DRIFT_FK_DIR 1
#define DRIFT_FK_SYMLINK 2
#define DRIFT_FK_OTHER 3
#define DRIFT_FK_UNKNOWN 4

/* Result status codes (stored on the job by the worker). */
#define DRIFT_FS_STATUS_OK 0
#define DRIFT_FS_STATUS_ERRNO 1
#define DRIFT_FS_STATUS_UTF8 2

/* Negative sentinels returned by drift_fs_read_dir (no handle).  Distinct so
 * callers can tell runtime backpressure / timeout / cancellation apart from an
 * underlying filesystem errno. */
#define DRIFT_FS_RC_ENOMEM (-1)
#define DRIFT_FS_RC_SATURATED (-2)
#define DRIFT_FS_RC_TIMEDOUT (-4)
#define DRIFT_FS_RC_CANCELLED (-5)

/* `EILSEQ` on Linux; the label stored as result errno for an invalid-UTF-8
 * filename (the runtime status, not an errno from the kernel). */
#ifndef EILSEQ
#define EILSEQ 84
#endif

typedef struct {
	char *name;       /* owned, UTF-8-validated, NUL-terminated */
	size_t name_len;
	int kind;         /* FileKind code */
} DriftDirEntC;

typedef struct DriftReadDirJob {
	DriftBlockingJob base;  /* job_fn / destroy_fn / vt / completed / expired / refcount / vt_resumed */
	char *path;             /* owned copy; freed in destroy_fn */
	int status;             /* DRIFT_FS_STATUS_* (close-error precedence resolved) */
	int err;                /* errno or EILSEQ */
	DriftDirEntC *entries;  /* C-owned snapshot (status ok); freed in destroy_fn */
	size_t count;
	size_t cap;
} DriftReadDirJob;

/* ---- result-handle table: mutex-protected, generation-tagged --------------
 * Maps an int64 handle to the surviving snapshot so the parked VT can read it
 * out across several accessor calls.  handle = (generation << 32) | slot_index;
 * a stale handle whose generation no longer matches its slot is rejected, so a
 * reused slot can never be addressed by an old handle (use-after-free becomes a
 * deterministic error, not corruption).  Concurrent read_dir() calls share the
 * table under one mutex. */
#define DRIFT_FS_MAX_RESULTS 1024

typedef struct {
	DriftReadDirJob *job;  /* NULL when free */
	uint32_t generation;
} FsResultSlot;

static pthread_mutex_t drift_fs_table_mu = PTHREAD_MUTEX_INITIALIZER;
static FsResultSlot drift_fs_table[DRIFT_FS_MAX_RESULTS];

static int64_t drift_fs_pack(uint32_t slot_index, uint32_t generation) {
	return ((int64_t)generation << 32) | (int64_t)slot_index;
}

/* Register a surviving job; returns a handle >= 1, or -1 if the table is full
 * (the caller then releases the job's VT stake and reports ENOMEM-ish). */
static int64_t drift_fs_table_register(DriftReadDirJob *job) {
	pthread_mutex_lock(&drift_fs_table_mu);
	for (uint32_t i = 0; i < DRIFT_FS_MAX_RESULTS; i++) {
		FsResultSlot *slot = &drift_fs_table[i];
		if (slot->job == NULL) {
			if (slot->generation == 0) {
				slot->generation = 1;  /* generation 0 reserved; handles are >= 1 */
			}
			slot->job = job;
			int64_t handle = drift_fs_pack(i, slot->generation);
			pthread_mutex_unlock(&drift_fs_table_mu);
			return handle;
		}
	}
	pthread_mutex_unlock(&drift_fs_table_mu);
	return -1;
}

/* Resolve a handle under the table mutex; NULL if stale/invalid. */
static DriftReadDirJob *drift_fs_table_lookup_locked(int64_t handle) {
	if (handle < 1) {
		return NULL;
	}
	uint32_t slot_index = (uint32_t)(handle & 0xFFFFFFFF);
	uint32_t generation = (uint32_t)((uint64_t)handle >> 32);
	if (slot_index >= DRIFT_FS_MAX_RESULTS) {
		return NULL;
	}
	FsResultSlot *slot = &drift_fs_table[slot_index];
	if (slot->job == NULL || slot->generation != generation) {
		return NULL;
	}
	return slot->job;
}

/* Remove a handle from the table (bumping its generation) and return the job,
 * or NULL if the handle was already stale/freed (double-free guard). */
static DriftReadDirJob *drift_fs_table_remove(int64_t handle) {
	pthread_mutex_lock(&drift_fs_table_mu);
	DriftReadDirJob *job = drift_fs_table_lookup_locked(handle);
	if (job != NULL) {
		uint32_t slot_index = (uint32_t)(handle & 0xFFFFFFFF);
		drift_fs_table[slot_index].job = NULL;
		drift_fs_table[slot_index].generation += 1;  /* invalidate the handle */
	}
	pthread_mutex_unlock(&drift_fs_table_mu);
	return job;
}

/* ---- UTF-8 validation (RFC 3629) ------------------------------------------ */
static int drift_fs_is_valid_utf8(const unsigned char *s, size_t len) {
	size_t i = 0;
	while (i < len) {
		unsigned char c = s[i];
		if (c < 0x80) {
			i += 1;
		} else if ((c & 0xE0) == 0xC0) {
			if (i + 1 >= len || (s[i + 1] & 0xC0) != 0x80 || c < 0xC2) {
				return 0;
			}
			i += 2;
		} else if ((c & 0xF0) == 0xE0) {
			if (i + 2 >= len || (s[i + 1] & 0xC0) != 0x80 ||
			    (s[i + 2] & 0xC0) != 0x80) {
				return 0;
			}
			if (c == 0xE0 && s[i + 1] < 0xA0) { return 0; }  /* overlong */
			if (c == 0xED && s[i + 1] >= 0xA0) { return 0; }  /* surrogate */
			i += 3;
		} else if ((c & 0xF8) == 0xF0) {
			if (i + 3 >= len || (s[i + 1] & 0xC0) != 0x80 ||
			    (s[i + 2] & 0xC0) != 0x80 || (s[i + 3] & 0xC0) != 0x80) {
				return 0;
			}
			if (c == 0xF0 && s[i + 1] < 0x90) { return 0; }            /* overlong */
			if (c > 0xF4 || (c == 0xF4 && s[i + 1] >= 0x90)) { return 0; } /* > U+10FFFF */
			i += 4;
		} else {
			return 0;
		}
	}
	return 1;
}

static int drift_fs_classify(mode_t mode) {
	if (S_ISLNK(mode)) { return DRIFT_FK_SYMLINK; }
	if (S_ISDIR(mode)) { return DRIFT_FK_DIR; }
	if (S_ISREG(mode)) { return DRIFT_FK_FILE; }
	return DRIFT_FK_OTHER;
}

static void drift_fs_entries_free(DriftReadDirJob *job) {
	if (job->entries) {
		for (size_t i = 0; i < job->count; i++) {
			free(job->entries[i].name);
		}
		free(job->entries);
		job->entries = NULL;
	}
	job->count = 0;
	job->cap = 0;
}

/* Sets job->status/err and discards any partial snapshot. */
static void drift_fs_set_error(DriftReadDirJob *job, int status, int err) {
	drift_fs_entries_free(job);
	job->status = status;
	job->err = err;
}

/* Test-only: number of walks that have ENTERED a pool worker (incremented before
 * the artificial stall).  A saturation regression uses this as a worker-entry
 * barrier — waiting until all N pool workers are busy — so the admitted count is
 * deterministic rather than dependent on worker dequeue timing.  Read via the
 * drift_fs_test_walk_entries() boundary. */
static atomic_int drift_fs_walk_entries;

int64_t drift_fs_test_walk_entries(void) {
	return (int64_t)atomic_load(&drift_fs_walk_entries);
}

/* The actual walk: open -> enumerate -> validate -> fstatat -> close, resolving
 * the single final outcome (incl. close-error precedence) onto the job.  Runs on
 * a pool worker (off-carrier) or inline on the main thread. */
static void drift_fs_do_walk(DriftReadDirJob *job) {
	atomic_fetch_add(&drift_fs_walk_entries, 1);
	/* Test-only artificial stall (portable NFS-stall proxy for regressions). */
	const char *stall = getenv("DRIFT_FS_TEST_STALL_MS");
	if (stall && *stall) {
		long ms = atol(stall);
		if (ms > 0) {
			struct timespec ts = { ms / 1000, (ms % 1000) * 1000000L };
			nanosleep(&ts, NULL);
		}
	}

	/* Test-only deterministic fault injection (for the error-semantics
	 * regressions): force a specific entry's fstatat to "fail" (-> Unknown),
	 * force a readdir failure with a given errno after the first entry, and/or
	 * force a closedir failure with a given errno.  Together these pin the
	 * per-entry-degrade, read-error-wins-over-close, and close-only-rejects
	 * behaviours without needing a real broken filesystem. */
	const char *fstatat_fail_name = getenv("DRIFT_FS_TEST_FSTATAT_FAIL_NAME");
	const char *readdir_fail_env = getenv("DRIFT_FS_TEST_READDIR_FAIL_ERRNO");
	int readdir_fail_errno = (readdir_fail_env && *readdir_fail_env) ? atoi(readdir_fail_env) : 0;
	const char *close_fail_env = getenv("DRIFT_FS_TEST_CLOSE_FAIL_ERRNO");
	int close_fail_errno = (close_fail_env && *close_fail_env) ? atoi(close_fail_env) : 0;

	DIR *dir = opendir(job->path);
	if (dir == NULL) {
		drift_fs_set_error(job, DRIFT_FS_STATUS_ERRNO, errno);
		return;
	}
	int dfd = dirfd(dir);

	int read_status = DRIFT_FS_STATUS_OK;
	int read_err = 0;
	for (;;) {
		errno = 0;
		struct dirent *de = readdir(dir);
		if (de == NULL) {
			if (errno != 0) {
				read_status = DRIFT_FS_STATUS_ERRNO;
				read_err = errno;
			}
			break;
		}
		if (de->d_name[0] == '.' &&
		    (de->d_name[1] == '\0' ||
		     (de->d_name[1] == '.' && de->d_name[2] == '\0'))) {
			continue;  /* skip "." and ".." */
		}
		size_t nlen = strlen(de->d_name);
		if (!drift_fs_is_valid_utf8((const unsigned char *)de->d_name, nlen)) {
			read_status = DRIFT_FS_STATUS_UTF8;
			read_err = EILSEQ;
			break;
		}

		int kind;
		struct stat st;
		if (fstatat(dfd, de->d_name, &st, AT_SYMLINK_NOFOLLOW) != 0) {
			kind = DRIFT_FK_UNKNOWN;  /* per-entry failure degrades, never fails the call */
		} else {
			kind = drift_fs_classify(st.st_mode);
		}
		if (fstatat_fail_name && strcmp(de->d_name, fstatat_fail_name) == 0) {
			kind = DRIFT_FK_UNKNOWN;  /* test hook: simulate this entry's fstatat failing */
		}

		if (job->count == job->cap) {
			size_t ncap = job->cap ? job->cap * 2 : 16;
			DriftDirEntC *grown = realloc(job->entries, ncap * sizeof(DriftDirEntC));
			if (!grown) {
				read_status = DRIFT_FS_STATUS_ERRNO;
				read_err = ENOMEM;
				break;
			}
			job->entries = grown;
			job->cap = ncap;
		}
		char *namecopy = malloc(nlen + 1);
		if (!namecopy) {
			read_status = DRIFT_FS_STATUS_ERRNO;
			read_err = ENOMEM;
			break;
		}
		memcpy(namecopy, de->d_name, nlen + 1);
		job->entries[job->count].name = namecopy;
		job->entries[job->count].name_len = nlen;
		job->entries[job->count].kind = kind;
		job->count++;

		if (readdir_fail_errno) {
			/* Test hook: simulate a readdir failure mid-scan (after a partial
			 * snapshot exists) so the read-error-vs-close-error precedence and
			 * the no-partial-snapshot guarantee are exercised. */
			read_status = DRIFT_FS_STATUS_ERRNO;
			read_err = readdir_fail_errno;
			break;
		}
	}

	int close_rc = closedir(dir);  /* always close */
	int close_errno = errno;
	if (close_fail_errno) {
		close_rc = -1;  /* test hook: simulate closedir failure */
		close_errno = close_fail_errno;
	}

	/* Close-error precedence: read/validate error wins; else a close failure
	 * is the error; else success.  Never hand back a snapshot after a close
	 * failure. */
	if (read_status != DRIFT_FS_STATUS_OK) {
		drift_fs_set_error(job, read_status, read_err);
	} else if (close_rc != 0) {
		drift_fs_set_error(job, DRIFT_FS_STATUS_ERRNO, close_errno);
	} else {
		job->status = DRIFT_FS_STATUS_OK;
		job->err = 0;
	}
}

static void drift_fs_read_dir_job_fn(DriftBlockingJob *base) {
	drift_fs_do_walk((DriftReadDirJob *)base);
}

static void drift_fs_read_dir_job_destroy(DriftBlockingJob *base) {
	DriftReadDirJob *job = (DriftReadDirJob *)base;
	drift_fs_entries_free(job);
	free(job->path);
	free(job);
}

int64_t drift_fs_read_dir(DriftString path_in, int64_t deadline_ms) {
	/* `path_in` arrives with a transferred refcount; release the stake here. */
	DRIFT_OWNED_STRING DriftString path = path_in;
	char *cpath = drift_string_to_cstr(path);
	if (cpath == NULL) {
		return DRIFT_FS_RC_ENOMEM;
	}

	DriftReadDirJob *job = calloc(1, sizeof(DriftReadDirJob));
	if (!job) {
		free(cpath);
		return DRIFT_FS_RC_ENOMEM;
	}
	job->base.job_fn = drift_fs_read_dir_job_fn;
	job->base.destroy_fn = drift_fs_read_dir_job_destroy;
	job->path = cpath;

	uint64_t vt = drift_thread_current_vt_handle();
	if (vt == 0) {
		/* Main thread (not a carrier): run inline, no pool, refcount = 1. */
		atomic_store(&job->base.refcount, 1);
		drift_fs_do_walk(job);
		int64_t handle = drift_fs_table_register(job);
		if (handle < 0) {
			drift_blocking_job_release(&job->base);  /* table full: drop it */
			return DRIFT_FS_RC_ENOMEM;
		}
		return handle;
	}

	/* VT path: submit to the shared blocking pool and park. */
	job->base.vt = vt;
	atomic_store(&job->base.refcount, 2);  /* VT stake + worker stake */
	if (drift_blocking_submit(&job->base) != 0) {
		/* Not submitted: no worker stake; free directly. */
		free(job->path);
		free(job);
		return DRIFT_FS_RC_SATURATED;
	}

	if (deadline_ms > 0) {
		drift_reactor_register_timer((uint64_t)deadline_ms, vt);
	}
	drift_thread_park(0);
	atomic_exchange(&job->base.vt_resumed, 1);  /* claim this VT's wakeup */

	if (atomic_load(&job->base.completed)) {
		if (deadline_ms > 0) {
			drift_reactor_cancel_vt_timers(vt);
		}
		int64_t handle = drift_fs_table_register(job);
		if (handle < 0) {
			drift_blocking_job_release(&job->base);  /* table full */
			return DRIFT_FS_RC_ENOMEM;
		}
		return handle;  /* VT keeps its stake until fs_result_free */
	}
	/* Woken with the job not yet complete — abandon it.  Drop the VT stake;
	 * the worker frees the snapshot when its in-flight syscall finally returns.
	 * The snapshot never reaches Drift and the VT never touches the job again.
	 * Distinguish cancellation (the VT was cancelled) from a plain deadline
	 * timeout so the caller gets the right error kind. */
	int cancelled = drift_thread_is_cancelled() ? 1 : 0;
	atomic_store(&job->base.expired, 1);
	drift_blocking_job_release(&job->base);
	/* Cancel our deadline timer so it cannot fire late and deposit a stale wake
	 * for the caller's next park (on the timeout path it already fired; on the
	 * cancel path it is still pending). */
	if (deadline_ms > 0) {
		drift_reactor_cancel_vt_timers(vt);
	}
	return cancelled ? DRIFT_FS_RC_CANCELLED : DRIFT_FS_RC_TIMEDOUT;
}

int64_t drift_fs_result_status(int64_t handle) {
	pthread_mutex_lock(&drift_fs_table_mu);
	DriftReadDirJob *job = drift_fs_table_lookup_locked(handle);
	int status = (job != NULL) ? job->status : DRIFT_FS_STATUS_ERRNO;
	int err = (job != NULL) ? 0 : EINVAL;
	pthread_mutex_unlock(&drift_fs_table_mu);
	(void)err;
	return (int64_t)status;
}

int64_t drift_fs_result_errno(int64_t handle) {
	pthread_mutex_lock(&drift_fs_table_mu);
	DriftReadDirJob *job = drift_fs_table_lookup_locked(handle);
	int err = (job != NULL) ? job->err : EINVAL;
	pthread_mutex_unlock(&drift_fs_table_mu);
	return (int64_t)err;
}

int64_t drift_fs_result_count(int64_t handle) {
	pthread_mutex_lock(&drift_fs_table_mu);
	DriftReadDirJob *job = drift_fs_table_lookup_locked(handle);
	int64_t count = (job != NULL) ? (int64_t)job->count : 0;
	pthread_mutex_unlock(&drift_fs_table_mu);
	return count;
}

DriftString drift_fs_result_name(int64_t handle, int64_t idx) {
	pthread_mutex_lock(&drift_fs_table_mu);
	DriftReadDirJob *job = drift_fs_table_lookup_locked(handle);
	DriftString s = {0, NULL};
	if (job != NULL && idx >= 0 && (size_t)idx < job->count) {
		s = drift_string_from_utf8_bytes(job->entries[idx].name,
		                                 (drift_isize)job->entries[idx].name_len);
	}
	pthread_mutex_unlock(&drift_fs_table_mu);
	return s;
}

int64_t drift_fs_result_kind(int64_t handle, int64_t idx) {
	pthread_mutex_lock(&drift_fs_table_mu);
	DriftReadDirJob *job = drift_fs_table_lookup_locked(handle);
	int kind = DRIFT_FK_UNKNOWN;
	if (job != NULL && idx >= 0 && (size_t)idx < job->count) {
		kind = job->entries[idx].kind;
	}
	pthread_mutex_unlock(&drift_fs_table_mu);
	return (int64_t)kind;
}

int64_t drift_fs_result_free(int64_t handle) {
	DriftReadDirJob *job = drift_fs_table_remove(handle);
	if (job != NULL) {
		drift_blocking_job_release(&job->base);  /* drop the VT stake */
	}
	return 0;
}
