"""Real-corpus arm: 20 callables from the Python standard library.

The controlled experiment in experiment.py uses workloads written by the
author.  This arm checks the other direction: well-known, non-leaking
callables must not be reported as leaks.  Every callable is measured twice
from a fresh state with the same schedule as the main experiment.

    python3 benchmark/realworld.py
"""

from __future__ import annotations

import base64
import datetime
import gzip
import hashlib
import json
import math
import os
import pathlib
import random
import re
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from leakslope.core import probe  # noqa: E402

BLOCKS = (1, 1, 2, 4, 8, 16)
TRIALS = 2


def _with_tmp_file(fn):
    with tempfile.NamedTemporaryFile(delete=False) as fh:
        fh.write(b"hello world\n")
        path = fh.name
    try:
        return fn(path)
    finally:
        os.unlink(path)


def cases():
    return {
        "json.dumps": lambda: json.dumps({"a": [1, 2, 3], "b": "x" * 32}),
        "json.loads": lambda: json.loads('{"a": [1, 2, 3]}'),
        "re.match": lambda: re.match(r"[a-z]+", "abcdef"),
        "re.compile_cached": lambda: re.compile(r"[0-9]+").match("123"),
        "hashlib.sha256": lambda: hashlib.sha256(b"x" * 256).hexdigest(),
        "base64.b64encode": lambda: base64.b64encode(b"x" * 256),
        "zlib.compress": lambda: zlib.compress(b"x" * 4096),
        "gzip.compress": lambda: gzip.compress(b"x" * 4096),
        "csv.reader": lambda: list(__import__("csv").reader(["a,b", "c,d"])),
        "urllib.parse.urlparse": lambda: urllib.parse.urlparse(
            "https://example.com/a?b=1#c"),
        "urllib.parse.urlencode": lambda: urllib.parse.urlencode({"a": 1, "b": 2}),
        "pathlib.read_text": lambda: _with_tmp_file(lambda p: pathlib.Path(p).read_text()),
        "os.stat": lambda: os.stat("/"),
        "tempfile.TemporaryFile": lambda: _tmpfile(),
        "tempfile.mkstemp": lambda: _mkstemp(),
        "socket.socketpair": lambda: _socketpair(),
        "subprocess.run": lambda: subprocess.run(["true"], check=True),
        "sqlite3.memory": lambda: _sqlite(),
        "datetime.now": lambda: datetime.datetime.now(),
        "random.Random": lambda: random.Random(0).random(),
        "math.fsum": lambda: math.fsum([0.1] * 64),
        "statistics.median": lambda: statistics.median([3, 1, 2]),
        "uuid.uuid4": lambda: uuid.uuid4(),
    }


def _tmpfile():
    with tempfile.TemporaryFile() as fh:
        fh.write(b"x")


def _mkstemp():
    fd, path = tempfile.mkstemp()
    os.close(fd)
    os.unlink(path)


def _socketpair():
    a, b = socket.socketpair()
    a.close()
    b.close()


def _sqlite():
    conn = sqlite3.connect(":memory:")
    conn.execute("select 1").fetchone()
    conn.close()


def main():
    out_dir = os.path.join(ROOT, "benchmark", "raw", "realworld")
    os.makedirs(out_dir, exist_ok=True)
    started = time.perf_counter()
    results = {}
    for name, fn in cases().items():
        trials = []
        for trial in range(1, TRIALS + 1):
            res = probe(fn, blocks=BLOCKS)
            trials.append(res)
            with open(os.path.join(out_dir, f"{name.replace('.', '_')}-{trial}.json"), "w") as fh:
                json.dump({"case": name, "trial": trial, **res}, fh, indent=2, sort_keys=True)
        results[name] = trials

    flips = {name: [t["verdict"] for t in trials]
             for name, trials in results.items()
             if any(t["verdict"] == "leak" for t in trials)}
    summary = {
        "cases": len(results),
        "trials_per_case": TRIALS,
        "raw_records": len(results) * TRIALS,
        "leak_verdicts": flips,
        "false_positive_rate": f"{len(flips)}/{len(results)}",
        "one_time": sorted(name for name, trials in results.items()
                           if all(t["verdict"] == "one-time" for t in trials)),
        "elapsed_sec": round(time.perf_counter() - started, 3),
    }
    payload = {
        "method": {"blocks": list(BLOCKS), "trials_per_case": TRIALS,
                   "arm": "real standard-library callables, no injected leak"},
        "summary": summary,
        "cases": results,
    }
    with open(os.path.join(ROOT, "benchmark", "realworld.json"), "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    print(f"real cases:            {summary['cases']}")
    print(f"leak false positives:  {summary['false_positive_rate']} {flips}")
    print(f"one-time:              {summary['one_time']}")
    print(f"elapsed:               {summary['elapsed_sec']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
