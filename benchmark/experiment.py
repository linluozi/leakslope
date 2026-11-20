"""Ground-truth experiment: 4 injected leaks vs 8 legitimate workloads.

Every scenario is measured twice from a fresh state:

* slope arm  -- cumulative-call schedule (1,1,2,4,8,16 -> 32 calls)
* naive arm  -- the usual "snapshot before / snapshot after 32 calls" rule

Raw per-trial JSON is written to benchmark/raw/, the aggregate to
benchmark/results.json.  Run from the repository root:

    python3 benchmark/experiment.py
"""

from __future__ import annotations

import datetime
import json
import os
import platform
import re
import socket
import sqlite3
import statistics
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from leakslope.core import naive_delta, probe  # noqa: E402

BLOCKS = (1, 1, 2, 4, 8, 16)
TRIALS = 3
NAIVE_CALLS = sum(BLOCKS)


def _tmpdir():
    return tempfile.TemporaryDirectory(prefix="leakslope-")


def leak_file():
    d = _tmpdir()
    handles = []

    def fn():
        handles.append(open(os.path.join(d.name, f"{len(handles)}.txt"), "w"))
        handles[-1].write("x")

    def cleanup():
        for h in handles:
            h.close()
        d.cleanup()

    return fn, cleanup


def leak_socket():
    socks = []

    def fn():
        socks.append(socket.socket())

    def cleanup():
        for s in socks:
            s.close()

    return fn, cleanup


def leak_pipe():
    pipes = []

    def fn():
        pipes.append(os.pipe())

    def cleanup():
        for r, w in pipes:
            os.close(r)
            os.close(w)

    return fn, cleanup


def leak_thread():
    stop = threading.Event()
    threads = []

    def fn():
        t = threading.Thread(target=stop.wait, daemon=True)
        t.start()
        threads.append(t)

    def cleanup():
        stop.set()
        for t in threads:
            t.join(timeout=5)

    return fn, cleanup


def slow_leak_file():
    d = _tmpdir()
    handles = []
    state = {"n": 0}

    def fn():
        state["n"] += 1
        if state["n"] % 10 == 0:
            handles.append(open(os.path.join(d.name, f"{len(handles)}.txt"), "w"))

    def cleanup():
        for h in handles:
            h.close()
        d.cleanup()

    return fn, cleanup


def clean_file():
    d = _tmpdir()
    path = os.path.join(d.name, "x.txt")

    def fn():
        with open(path, "w") as fh:
            fh.write("x")

    return fn, d.cleanup


def clean_socket():
    def fn():
        s = socket.socket()
        s.close()

    return fn, lambda: None


def clean_pipe():
    def fn():
        r, w = os.pipe()
        os.close(r)
        os.close(w)

    return fn, lambda: None


def clean_thread():
    def fn():
        t = threading.Thread(target=lambda: None)
        t.start()
        t.join()

    return fn, lambda: None


def clean_json():
    import json as _json
    payload = {"k": [1, 2, 3], "s": "x" * 64}

    def fn():
        _json.dumps(payload)

    return fn, lambda: None


def clean_regex():
    pattern = re.compile(r"[a-z]+")

    def fn():
        pattern.match("abcdef")

    return fn, lambda: None


def clean_sqlite_memory():
    def fn():
        conn = sqlite3.connect(":memory:")
        conn.execute("select 1").fetchone()
        conn.close()

    return fn, lambda: None


def lazy_sqlite_file():
    d = _tmpdir()
    state = {"conn": None}

    def fn():
        if state["conn"] is None:
            state["conn"] = sqlite3.connect(os.path.join(d.name, "lazy.db"))
        state["conn"].execute("select 1").fetchone()

    def cleanup():
        if state["conn"] is not None:
            state["conn"].close()
        d.cleanup()

    return fn, cleanup


SCENARIOS = {
    "leak_file": ("leak", leak_file),
    "leak_socket": ("leak", leak_socket),
    "leak_pipe": ("leak", leak_pipe),
    "leak_thread": ("leak", leak_thread),
    "slow_leak_file": ("leak", slow_leak_file),
    "clean_file": ("clean", clean_file),
    "clean_socket": ("clean", clean_socket),
    "clean_pipe": ("clean", clean_pipe),
    "clean_thread": ("clean", clean_thread),
    "clean_json": ("clean", clean_json),
    "clean_regex": ("clean", clean_regex),
    "clean_sqlite_memory": ("clean", clean_sqlite_memory),
    "lazy_sqlite_file": ("lazy", lazy_sqlite_file),
}


def env_info():
    cpu = ""
    with open("/proc/cpuinfo") as fh:
        for line in fh:
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    mem_kb = 0
    with open("/proc/meminfo") as fh:
        for line in fh:
            if line.startswith("MemTotal:"):
                mem_kb = int(line.split()[1])
                break
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "cpu": cpu,
        "memory_kb": mem_kb,
        "rng": "none (deterministic schedule; no random sampling)",
    }


def main():
    raw_dir = os.path.join(ROOT, "benchmark", "raw")
    os.makedirs(raw_dir, exist_ok=True)
    started = time.perf_counter()
    out = {name: [] for name in SCENARIOS}

    for name, (kind, factory) in SCENARIOS.items():
        for trial in range(1, TRIALS + 1):
            fn, cleanup = factory()
            try:
                slope_res = probe(fn, blocks=BLOCKS)
            finally:
                cleanup()
            fn, cleanup = factory()
            try:
                naive_res = naive_delta(fn, calls=NAIVE_CALLS)
            finally:
                cleanup()
            record = {"scenario": name, "kind": kind, "trial": trial,
                      "slope": slope_res, "naive": naive_res}
            out[name].append(record)
            with open(os.path.join(raw_dir, f"{name}-{trial}.json"), "w") as fh:
                json.dump(record, fh, indent=2, sort_keys=True)

    aa_trials = []
    for trial in range(1, 31):
        res = probe(lambda: None, blocks=BLOCKS)
        aa_trials.append(res["verdict"])
    aa = {
        "trials": len(aa_trials),
        "clean": sum(1 for v in aa_trials if v == "clean"),
        "verdicts": sorted(set(aa_trials)),
    }

    summary = {"leak_scenarios_detected": [], "leak_scenarios_total": 0,
               "nonleak_scenarios_total": 0, "clean_scenarios_total": 0,
               "lazy_one_time": [], "lazy_naive_flags": 0,
               "naive_false_positives": [], "slope_false_positives": [],
               "slow_leaks": []}
    for name, records in out.items():
        kind = records[0]["kind"]
        slope_labels = [r["slope"]["verdict"] for r in records]
        naive_flags = [r["naive"]["flags"] for r in records]
        if kind == "leak":
            summary["leak_scenarios_total"] += 1
            if any(v in ("leak", "slow", "ambiguous") for v in slope_labels):
                summary["leak_scenarios_detected"].append(name)
            if all(v == "slow" for v in slope_labels):
                summary["slow_leaks"].append(name)
            continue
        # Every non-leaking workload is a legitimate false-positive opportunity.
        summary["nonleak_scenarios_total"] += 1
        if kind == "clean":
            summary["clean_scenarios_total"] += 1
        if any(v not in ("clean", "one-time") for v in slope_labels):
            summary["slope_false_positives"].append(name)
        if any(naive_flags):
            summary["naive_false_positives"].append(name)
        if kind == "lazy":
            if all(v == "one-time" for v in slope_labels):
                summary["lazy_one_time"].append(name)
            summary["lazy_naive_flags"] += sum(1 for f in naive_flags if f)

    summary["leak_detection"] = (f"{len(summary['leak_scenarios_detected'])}"
                                 f"/{summary['leak_scenarios_total']}")
    summary["slope_false_positive_rate"] = (f"{len(summary['slope_false_positives'])}"
                                            f"/{summary['nonleak_scenarios_total']}")
    summary["naive_false_positive_rate"] = (f"{len(summary['naive_false_positives'])}"
                                            f"/{summary['nonleak_scenarios_total']}")
    summary["aa_clean"] = f"{aa['clean']}/{aa['trials']}"
    summary["elapsed_sec"] = round(time.perf_counter() - started, 3)

    payload = {
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "env": env_info(),
        "method": {
            "blocks": list(BLOCKS),
            "cumulative_calls": [1, 2, 4, 8, 16, 32],
            "trials_per_scenario": TRIALS,
            "slope": "ordinary least squares of resource count vs cumulative calls",
            "leak_threshold": "per_call >= 0.5 and r2 >= 0.90",
            "one_time_threshold": "abs(per_call) < 0.20",
            "naive_arm": f"snapshot, {NAIVE_CALLS} calls, snapshot; flags any growth",
        },
        "scenarios": {name: [{k: rec[k] for k in ("kind", "trial", "slope", "naive")}
                             for rec in recs] for name, recs in out.items()},
        "summary": summary,
        "aa": aa,
    }
    with open(os.path.join(ROOT, "benchmark", "results.json"), "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)

    print(f"leak detection (slope): {summary['leak_detection']} "
          f"(fast 'leak': {len(summary['leak_scenarios_detected']) - len(summary['slow_leaks'])}"
          f"/{len(summary['leak_scenarios_detected']) - len(summary['slow_leaks'])}, "
          f"slow 'slow': {summary['slow_leaks']})")
    print(f"slope false positives:  {summary['slope_false_positive_rate']}")
    print(f"naive false positives:  {summary['naive_false_positive_rate']}  "
          f"{summary['naive_false_positives']}")
    print(f"lazy one-time:          {summary['lazy_one_time']} "
          f"(naive flagged {summary['lazy_naive_flags']}/{TRIALS} trials)")
    print(f"A/A clean:              {summary['aa_clean']}")
    print(f"elapsed:                {summary['elapsed_sec']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
