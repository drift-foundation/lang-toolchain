# Aborted full-suite run — host OOM, 2026-08-11

Preserved for audit per pushcoin.reviewer request. This run is INCONCLUSIVE.

## Outcome counts before abort

    PASSED lines: 3896
    FAILED lines: 2
    last progress: 49%   (no pytest summary line was ever written)

## The two failures, verbatim from the log

    9345:[gw8] [ 48%] FAILED lang/tests/driver/test_logger_no_attrs_overload.py::test_info_no_attrs
    9347:[gw0] [ 48%] FAILED lang/tests/driver/test_borrow_in_cast_no_double_free.py::test_borrow_directly_bound_still_ok

## Host condition: kernel OOM killer fired

    Aug 11 09:15:29 slryzen kernel: Out of memory: Killed process 2309 (wireplumber) total-vm:401348kB, anon-rss:1536kB, file-rss:6144kB, shmem-rss:0kB, UID:1000 pgtables:136kB oom_score_adj:200
    Aug 11 09:18:04 slryzen kernel: claude invoked oom-killer: gfp_mask=0x140cca(GFP_HIGHUSER_MOVABLE|__GFP_COMP), order=0, oom_score_adj=0
    Aug 11 09:18:11 slryzen kernel: oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=user.slice,mems_allowed=0,global_oom,task_memcg=/user.slice/user-109.slice/user@109.service/session.slice/wireplumber.service,task=wireplumber,pid=2205425,uid=109
    Aug 11 09:18:11 slryzen kernel: Out of memory: Killed process 2205425 (wireplumber) total-vm:533496kB, anon-rss:4724kB, file-rss:7152kB, shmem-rss:0kB, UID:109 pgtables:296kB oom_score_adj:200

Suite process gone by 2026-08-11T15:19:08Z; last log write 15:15:29Z; OOM kill 15:18:11Z.

## Same two files, run in isolation on the SAME tree, immediately after

    PYTHONPATH=. .venv/bin/python -m pytest \
      lang/tests/driver/test_logger_no_attrs_overload.py \
      lang/tests/driver/test_borrow_in_cast_no_double_free.py -q
    -> 7 passed in 196.17s

## Pre-existing hardcoded timeout (not introduced by this finding)

    43:		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(60),
    46:	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=10)

Line 46 uses a hardcoded `timeout=10` for the binary run while line 43 correctly
uses `sanitizer_timeout(60)`. Under -n16 with the host starving, a hardcoded
10s subprocess budget is the documented false-TimeoutExpired shape. This is an
observation about the existing test, NOT a claim that it explains the failure.

## Standing conclusion

Host conditions were invalid (kernel OOM). This run is neither a green gate nor
evidence that the build-info patch caused the failures. Only a clean rerun on the
same reviewed diff decides criterion 6.
