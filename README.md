# leakslope

**Per-call OS resource leak adjudication.** `leakslope` decides whether a function leaks
file descriptors or threads by fitting *resources vs cumulative calls* instead of
comparing two snapshots, so a legitimate one-time initialisation is not reported as a leak.

`Upstream: none` — this is an original tool, not a fork or a patch of another project.

Why this exists: the usual leak check is `before = fds(); call_n_times(); after = fds()`
and "after > before" is called a leak. That rule cannot distinguish *one fd per call*
(a leak) from *one fd on the first call* (a connection pool, a log handler, a lazily
opened database). `leakslope` calls the target on a doubling schedule, fits a line, and
classifies the result as `clean`, `one-time`, `leak` or `ambiguous`; it also reports
which kinds of fd appeared (`file`, `socket`, `pipe`, `anon_inode`).

## Usage

```bash
python3 -m leakslope probe examples/leak.py:leak --naive
python3 -m leakslope probe examples/clean.py:clean --json
```

Exit status is `1` when the verdict is `leak`, `0` otherwise, so it can be used as a
CI gate. `--blocks` changes the call schedule; `--naive` additionally prints what the
before/after rule would have said.

```
target      examples/leak.py:leak
calls       [1, 2, 4, 8, 16, 32]
fd         total=32   per_call=1.0     r2=1.0   label=leak
           new fds: {'file': 32}
thread     total=0    per_call=0.0     r2=1.0   label=clean
naive rule  fd_growth=32 thread_growth=0 flags=True
verdict     leak
```

## Evidence

`benchmark/experiment.py` builds 12 workloads: 4 with an injected per-call leak and
8 legitimate ones (7 that release everything, 1 that opens a database once and then
reuses it). Each workload is measured in 3 independent trials from a fresh state, in
two arms: the slope schedule and the naive before/after rule. 36 raw records are in
`benchmark/raw/`, the aggregate in `benchmark/results.json`.

| workload | injected rate | measured per-call | r² | naive Δ (32 calls) | slope verdict |
|---|---|---|---|---|---|
| leak_file | +1 fd/call | 1.000 | 1.000 | +32 fds | leak |
| leak_socket | +1 fd/call | 1.000 | 1.000 | +32 fds | leak |
| leak_pipe | +2 fds/call | 2.000 | 1.000 | +64 fds | leak |
| leak_thread | +1 thread/call | 1.000 (thread) | 1.000 | +32 threads | leak |
| clean_file | 0 | 0.000 | 1.000 | 0 | clean |
| clean_socket | 0 | 0.000 | 1.000 | 0 | clean |
| clean_pipe | 0 | 0.000 | 1.000 | 0 | clean |
| clean_thread | 0 | 0.000 | 1.000 | 0 | clean |
| clean_json | 0 | 0.000 | 1.000 | 0 | clean |
| clean_regex | 0 | 0.000 | 1.000 | 0 | clean |
| clean_sqlite_memory | 0 | 0.000 | 1.000 | 0 | clean |
| lazy_sqlite_file | one-time | 0.000 | 1.000 | **+1 fd** | one-time |

Three-trial medians; all 36 per-trial verdicts agreed with the median. Summary from
`benchmark/results.json`:

- leak detection: **4/4** scenarios at the exact injected rate;
- slope false positives: **0/8** non-leaking scenarios;
- naive false positives: **1/8** — `lazy_sqlite_file` is flagged in **3/3** trials
  (one fd created on the first call and reused afterwards);
- A/A calibration: **30/30** sweeps of a no-op classified `clean`.

A second arm checks the opposite direction on code nobody in this repository wrote:
`benchmark/realworld.py` probes **23 standard-library callables** (json, re, hashlib,
zlib, gzip, csv, urllib.parse, pathlib, os.stat, tempfile, socketpair, subprocess,
sqlite3, datetime, random, math, statistics, uuid) twice each. Result: **0/23** leak
verdicts, **0/23** one-time, 46 raw records under `benchmark/raw/realworld/`, 0.08-0.09 s across two runs.
The only resource-opening cases (`TemporaryFile`, `mkstemp`, `socketpair`, sqlite3)
close their resource inside the call, and the slope says so.

The leak is not theoretical: `benchmark/fdlimit.py` runs a child with
`RLIMIT_NOFILE` soft=hard=128 and keeps every opened file. The first `EMFILE`
(`errno 24`) arrives at **call 126** after **125** successful opens (3 fds already
open). At one fd per call, a test that loops the leaking function 1,000 times exhausts
the default 1,024 limit.

## Tests

```bash
python3 -m unittest discover -s tests -v
# 12 tests, OK (0.038-0.040 s across three consecutive runs)
```

Coverage: `/proc` parsing and fd classification on a synthetic proc tree; OLS against
an exact line and a flat series; every label boundary; real fd leak (exact slope and
attribution), real thread leak, clean round-trip; a one-time initialisation that the
naive rule flags and the slope rule does not; 12 A/A sweeps; CLI exit codes, JSON
output, and `--naive` rendering.

## Quality gates

This project has no upstream and no patch, so the opt-lab gates G1–G5 are not
applicable; the substitute evidence is listed here. Every command is runnable from the
repository root.

| gate | result | reproduce |
|---|---|---|
| upstream tests (G1) | N/A — original tool, no upstream | — |
| differential (G2) | 36/36 raw records reproducible; 3/3 trials per scenario agree | `python3 benchmark/experiment.py` |
| timing/rate accuracy (G3) | 4/4 leaks at exact rate, r²=1.000 | same |
| real-workload false positives (G3b) | 0/8 slope vs 1/8 naive | same |
| A/A calibration (G4) | 30/30 clean | same |
| real-corpus false positives | 0/23 stdlib callables, 46 records | `python3 benchmark/realworld.py` |
| memory (G5) | N/A — measurement is O(1) counters, no allocation per call | — |
| mechanism check | EMFILE at call 126 with limit 128 | `python3 benchmark/fdlimit.py` |

## Limitations

- Linux only: fd targets come from `/proc/self/fd`; the code falls back to `/dev/fd`.
- Threads are counted, not attributed; fd attribution is by target kind and cannot say
  which call site opened the fd.
- asyncio tasks, memory and subprocesses are out of scope.
- The classification threshold is `per_call >= 0.5` with `r² >= 0.90`; a leak slower
  than one resource per two calls needs a larger schedule (`--blocks`) to resolve.
- Counts are process-wide. Run the probe in a dedicated process when other threads in
  the same process may open fds concurrently.
- `probe` executes the target 32 times by default (the sum of `--blocks`). Use a
  disposable process for targets with destructive side effects.

## Environment

- CPython 3.12.3, Linux 6.8.0-124-generic x86_64 (glibc 2.39)
- AMD EPYC 7K62, 4 vCPU, 3,812,564 kB RAM
- No RNG: the call schedule is deterministic, so no seed is involved.
- Result files: `benchmark/results.json`, `benchmark/realworld.json`,
  `benchmark/fdlimit.json`, `benchmark/raw/*.json`.

## 本轮创新

以前做泄漏判定都是「跑 N 次前后各数一次」，本仓第一次把资源数当成**累计调用次数的函数**来拟合：
一次性懒加载是阶跃（斜率 0），真泄漏是直线（斜率 k）。在 12 个有注入真值的工作负载上，
斜率法 **4/4 检出且速率精确到 1.000/2.000**、**0/8 误报**；同一批数据上「前后差值」把
`lazy_sqlite_file` 这种合法的首次打开误判为泄漏（**3/3 次**）。台账里 gcphase 测 GC 阶段、
memtruth 测 tracemalloc/RSS、peakgate 测每次调用的分配峰值，没有一条把 **fd/线程的按调用斜率**
作为判据，也没有把「一次性初始化」与「每调用泄漏」在测量层分开。
