# regex bench results

provenance: commit 32d676bbd4f3, driftc 0.33.88 / ABI 22, host slryzen, 2026-07-26T13:49:13.351947+00:00
load samples: [0.91, 0.91, 0.92, 0.93, 0.94, 0.94, 0.94, 0.97, 1.05, 1.08, 1.04, 1.02]

## timing (same-launch medians, us)

- scan_all_carrier_2m: median-of-medians 175196 us (launch medians: 175125, 176077, 175196, 175067, 175639)
- find_nomatch_2m: median-of-medians 268177 us (launch medians: 268177, 282494, 266953, 267850, 270676)
- find_nomatch_view_2m: median-of-medians 271027 us (launch medians: 271110, 269940, 266355, 278016, 271027)
- alt_nomatch_512k: median-of-medians 114368 us (launch medians: 114135, 109041, 118764, 118078, 114368)
- zw_1k_x20000: median-of-medians 963 us (launch medians: 960, 959, 963, 976, 979)
- short_hit_x20000: median-of-medians 4391 us (launch medians: 4387, 4391, 4352, 4539, 4418)
- short_miss_x20000: median-of-medians 22216 us (launch medians: 22216, 22200, 22086, 23503, 22344)
- anchor_x20000: median-of-medians 7230 us (launch medians: 7162, 7230, 7237, 7301, 7167)
- compile_p1_x2000: median-of-medians 258 us (launch medians: 258, 346, 257, 257, 260)
- compile_alt_x2000: median-of-medians 2203 us (launch medians: 2218, 2283, 2200, 2201, 2203)
- scratch_four_x200000: median-of-medians 7438 us (launch medians: 7440, 7569, 7438, 7400, 7360)
- scratch_packed_x200000: median-of-medians 3257 us (launch medians: 3257, 3144, 3259, 3255, 3258)

## small-subject suite (PRIMARY gate): ns/search

| row | size | scenario | form | ns/search | searches/s |
|---|---|---|---|---|---|
| sm_early_64 | 64 | early | string | 173 | 5,769,941 |
| sm_early_128 | 128 | early | string | 174 | 5,757,185 |
| sm_early_256 | 256 | early | string | 172 | 5,816,828 |
| sm_early_512 | 512 | early | string | 173 | 5,795,252 |
| sm_early_1024 | 1024 | early | string | 173 | 5,795,252 |
| sm_early_4096 | 4096 | early | string | 174 | 5,747,126 |
| sm_late_64 | 64 | late | string | 8,861 | 112,855 |
| sm_late_128 | 128 | late | string | 19,526 | 51,214 |
| sm_late_256 | 256 | late | string | 46,896 | 21,324 |
| sm_late_512 | 512 | late | string | 75,640 | 13,221 |
| sm_late_1024 | 1024 | late | string | 153,311 | 6,523 |
| sm_late_4096 | 4096 | late | string | 608,558 | 1,643 |
| sm_nomatch_64 | 64 | nomatch | string | 9,720 | 102,882 |
| sm_nomatch_128 | 128 | nomatch | string | 21,269 | 47,016 |
| sm_nomatch_256 | 256 | nomatch | string | 37,751 | 26,489 |
| sm_nomatch_512 | 512 | nomatch | string | 75,380 | 13,266 |
| sm_nomatch_1024 | 1024 | nomatch | string | 150,926 | 6,626 |
| sm_nomatch_4096 | 4096 | nomatch | string | 591,274 | 1,691 |
| sm_anchored_64 | 64 | anchored | string | 1,993 | 501,750 |
| sm_anchored_128 | 128 | anchored | string | 3,984 | 251,012 |
| sm_anchored_256 | 256 | anchored | string | 7,960 | 125,621 |
| sm_anchored_512 | 512 | anchored | string | 15,831 | 63,168 |
| sm_anchored_1024 | 1024 | anchored | string | 31,657 | 31,588 |
| sm_anchored_4096 | 4096 | anchored | string | 126,426 | 7,910 |
| sm_alt_64 | 64 | alt | string | 6,375 | 156,867 |
| sm_alt_128 | 128 | alt | string | 12,814 | 78,041 |
| sm_alt_256 | 256 | alt | string | 25,400 | 39,370 |
| sm_alt_512 | 512 | alt | string | 50,721 | 19,716 |
| sm_alt_1024 | 1024 | alt | string | 101,329 | 9,869 |
| sm_alt_4096 | 4096 | alt | string | 404,186 | 2,474 |
| sv_late_256 | 256 | late | view | 46,931 | 21,308 |
| sv_late_4096 | 4096 | late | view | 605,844 | 1,651 |
| sv_nomatch_256 | 256 | nomatch | view | 37,553 | 26,629 |
| sv_nomatch_4096 | 4096 | nomatch | view | 602,776 | 1,659 |
| cm_late_256 | 256 | late | compile+match | 46,925 | 21,311 |
| cm_nomatch_256 | 256 | nomatch | compile+match | 37,753 | 26,488 |
| cm_anchored_256 | 256 | anchored | compile+match | 8,018 | 124,725 |
| cm_alt_256 | 256 | alt | compile+match | 26,944 | 37,115 |

## count windows (matching-only = op - compile twin; obs=pred, residual must be zero)

| op | arrays | alloc calls | real allocs | real frees | noop frees | retain/release | ok |
|---|---|---|---|---|---|---|---|
| scan_all_64k | 367166 | 734332=734332 | 367166=367166 | 367166=367166 | 1005337=1005337 | 0/0+0n | PASS |
| scan_all_2m | 11744210 | 23488420=23488420 | 11744210=11744210 | 11744210=11744210 | 32156767=32156767 | 0/0+0n | PASS |
| find_nomatch_64k | 563818 | 1127636=1127636 | 563818=563818 | 563818=563818 | 1540667=1540667 | 0/0+0n | PASS |
| find_nomatch_2m | 18035578 | 36071156=36071156 | 18035578=18035578 | 18035578=18035578 | 49283267=49283267 | 0/0+0n | PASS |
| find_nomatch_view_64k | 563818 | 1127636=1127636 | 563818=563818 | 563818=563818 | 1540667=1540667 | 1/1+1n | PASS |
| alt_64k | 278632 | 557264=557264 | 278632=278632 | 278632=278632 | 827702=827702 | 0/0+0n | PASS |
| zw_x100 | 400 | 800=800 | 400=400 | 400=400 | 1200=1200 | 0/0+0n | PASS |
| short_hit_x100 | 1400 | 2800=2800 | 1400=1400 | 1400=1400 | 3700=3700 | 0/0+0n | PASS |
| short_miss_x100 | 7200 | 14400=14400 | 7200=7200 | 7200=7200 | 19600=19600 | 0/0+0n | PASS |
| anchor_x100 | 2200 | 4400=4400 | 2200=2200 | 2200=2200 | 5700=5700 | 0/0+0n | PASS |
| sc_late_64 | 59800 | 119600=119600 | 59800=59800 | 59800=59800 | 161700=161700 | 0/0+0n | PASS |
| sc_late_128 | 131000 | 262000=262000 | 131000=131000 | 131000=131000 | 352500=352500 | 0/0+0n | PASS |
| sc_late_256 | 311800 | 623600=623600 | 311800=311800 | 311800=311800 | 830100=830100 | 0/0+0n | PASS |
| sc_late_512 | 507800 | 1015600=1015600 | 507800=507800 | 507800=507800 | 1371300=1371300 | 0/0+0n | PASS |
| sc_late_1024 | 1027000 | 2054000=2054000 | 1027000=1027000 | 1027000=1027000 | 2771700=2771700 | 0/0+0n | PASS |
| sc_late_4096 | 4091800 | 8183600=8183600 | 4091800=4091800 | 4091800=4091800 | 11048100=11048100 | 0/0+0n | PASS |
| sc_nomatch_64 | 65000 | 130000=130000 | 65000=65000 | 65000=65000 | 175500=175500 | 0/0+0n | PASS |
| sc_nomatch_128 | 142600 | 285200=285200 | 142600=142600 | 142600=142600 | 382300=382300 | 0/0+0n | PASS |
| sc_nomatch_256 | 255000 | 510000=510000 | 255000=255000 | 255000=255000 | 688900=688900 | 0/0+0n | PASS |
| sc_nomatch_512 | 513000 | 1026000=1026000 | 513000=513000 | 513000=513000 | 1385100=1385100 | 0/0+0n | PASS |
| sc_nomatch_1024 | 1038600 | 2077200=2077200 | 1038600=1038600 | 1038600=1038600 | 2801500=2801500 | 0/0+0n | PASS |
| sc_nomatch_4096 | 4097000 | 8194000=8194000 | 4097000=4097000 | 4097000=4097000 | 11061900=11061900 | 0/0+0n | PASS |
| sc_early_256 | 1200 | 2400=2400 | 1200=1200 | 1200=1200 | 3200=3200 | 0/0+0n | PASS |
| sc_early_4096 | 1200 | 2400=2400 | 1200=1200 | 1200=1200 | 3200=3200 | 0/0+0n | PASS |
| sc_anchored_256 | 51400 | 102800=102800 | 51400=51400 | 51400=51400 | 128700=128700 | 0/0+0n | PASS |
| sc_anchored_4096 | 819400 | 1638800=1638800 | 819400=819400 | 819400=819400 | 2048700=2048700 | 0/0+0n | PASS |
| sc_alt_256 | 101600 | 203200=203200 | 101600=101600 | 101600=101600 | 304400=304400 | 0/0+0n | PASS |
| sc_alt_4096 | 1637600 | 3275200=3275200 | 1637600=1637600 | 1637600=1637600 | 4912400=4912400 | 0/0+0n | PASS |
| sc_late_view_256 | 311800 | 623600=623600 | 311800=311800 | 311800=311800 | 830100=830100 | 1/1+1n | PASS |
| sc_late_view_4096 | 4091800 | 8183600=8183600 | 4091800=4091800 | 4091800=4091800 | 11048100=11048100 | 1/1+1n | PASS |
| sc_nomatch_view_256 | 255000 | 510000=510000 | 255000=255000 | 255000=255000 | 688900=688900 | 1/1+1n | PASS |
| sc_nomatch_view_4096 | 4097000 | 8194000=8194000 | 4097000=4097000 | 4097000=4097000 | 11061900=11061900 | 1/1+1n | PASS |

failures: NONE
