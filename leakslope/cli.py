"""CLI: ``python -m leakslope probe path.py:func``."""

from __future__ import annotations

import argparse
import importlib.util
import json
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="leakslope")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe", help="measure resources per call")
    p.add_argument("target", help="path/to/file.py:function")
    p.add_argument("--blocks", default=",".join(map(str, DEFAULT_BLOCKS)),
                   help="comma separated block sizes (default 1,1,2,4,8,16)")
    p.add_argument("--naive", action="store_true",
                   help="also run the before/after rule and print it")
    p.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    fn = load_callable(args.target)
    blocks = tuple(int(x) for x in args.blocks.split(",") if x.strip())
    result = probe(fn, blocks=blocks)
    if args.naive:
        result["naive"] = naive_delta(fn, calls=sum(blocks))
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"target      {args.target}")
        print(f"calls       {result['call' + 's']}")
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
    return 1 if result["verdict"] == "leak" else 0


if __name__ == "__main__":
    sys.exit(main())
