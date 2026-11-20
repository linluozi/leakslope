"""CLI: ``python -m leakslope probe path.py:func``."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys

from .core import DEFAULT_BLOCKS, naive_delta, probe


def load_callable(spec: str):
    """Load ``path.py:func`` and return the callable."""
    if ":" not in spec:
        raise SystemExit(f"expected path.py:func, got {spec!r}")
    path, name = spec.rsplit(":", 1)
    module_spec = importlib.util.spec_from_file_location("leakslope_target", path)
    if module_spec is None or module_spec.loader is None:
        raise SystemExit(f"cannot import {path!r}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    try:
        fn = getattr(module, name)
    except AttributeError:
        raise SystemExit(f"{path!r} has no attribute {name!r}")
    if not callable(fn):
        raise SystemExit(f"{name!r} is not callable")
    return fn


def _naive_in_fresh_process(target: str, calls: int) -> dict:
    """Run the before/after arm in a new interpreter.

    The probe arm initialises lazy state by calling the target.  Running the
    naive arm in the same process would therefore hide exactly the first-call
    growth it is supposed to detect, so it gets its own process.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "leakslope", "_naive", target, "--calls", str(calls)],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise SystemExit("naive arm failed in a fresh process:\n"
                         + (proc.stderr or proc.stdout)[-2000:])
    return json.loads(proc.stdout.strip().splitlines()[-1])["naive"]


def _parse_blocks(parser, raw: str):
    try:
        blocks = tuple(int(x) for x in raw.split(",") if x.strip())
    except ValueError:
        parser.error("--blocks must be a comma-separated list of integers")
    if len(blocks) < 2:
        parser.error("--blocks needs at least two blocks; "
                     "a single block cannot estimate a slope")
    if any(b <= 0 for b in blocks):
        parser.error("--blocks values must be positive")
    return blocks


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="leakslope")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="measure resources per call")
    p.add_argument("target", help="path/to/file.py:function")
    p.add_argument("--blocks", default=",".join(map(str, DEFAULT_BLOCKS)),
                   help="comma separated block sizes (default 1,1,2,4,8,16)")
    p.add_argument("--naive", action="store_true",
                   help="also run the before/after rule in a fresh process")
    p.add_argument("--calls", type=int, default=32,
                   help="calls for the --naive arm (default 32)")
    p.add_argument("--json", action="store_true", help="emit JSON")

    hidden = sub.add_parser("_naive")
    hidden.add_argument("target")
    hidden.add_argument("--calls", type=int, default=32)

    args = parser.parse_args(argv)

    if args.cmd == "_naive":
        print(json.dumps({"naive": naive_delta(load_callable(args.target),
                                               calls=args.calls)}, sort_keys=True))
        return 0

    blocks = _parse_blocks(parser, args.blocks)
    fn = load_callable(args.target)
    try:
        result = probe(fn, blocks=blocks)
    except ValueError as exc:
        parser.error(str(exc))
    if args.naive:
        result["naive"] = _naive_in_fresh_process(args.target, args.calls)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"target      {args.target}")
        print(f"calls       {result['calls']}")
        for axis in result["axes"]:
            print(f"{axis['axis']:<10} total={axis['total']:<4} "
                  f"per_call={axis['per_call']:<7} r2={axis['r2']:<6} "
                  f"label={axis['label']}")
            if axis["attribution"]:
                print(f"{'':<10} new fds: {axis['attribution']}")
        if "naive" in result:
            print(f"naive rule  fd_growth={result['naive']['fd_growth']} "
                  f"thread_growth={result['naive']['thread_growth']} "
                  f"flags={result['naive']['flags']}")
        print(f"verdict     {result['verdict']}")
    return 1 if result["verdict"] in ("leak", "slow", "ambiguous") else 0


if __name__ == "__main__":
    sys.exit(main())