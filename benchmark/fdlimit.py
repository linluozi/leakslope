"""Verify the consequence of a 1-fd-per-call leak under a small fd limit.

Runs a child process with RLIMIT_NOFILE soft=hard=128, keeps every opened
file, and reports the index of the first EMFILE.  This is a mechanism check,
not a benchmark: the point is that the slope measured on 32 calls predicts
when a real process dies.

    python3 benchmark/fdlimit.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

CHILD = r"""
import json, resource
resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
keep = []
fail = None
for i in range(1, 400):
    try:
        keep.append(open("/dev/null"))
    except OSError as exc:
        fail = {"call": i, "errno": exc.errno, "error": exc.strerror}
        break
print(json.dumps({"first_fail": fail, "opened": len(keep),
                  "limit": resource.getrlimit(resource.RLIMIT_NOFILE)[0]}))
"""


def main():
    proc = subprocess.run([sys.executable, "-c", CHILD], capture_output=True,
                          text=True, timeout=60)
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        return 1
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fdlimit.json")
    payload["command"] = "RLIMIT_NOFILE soft=hard=128; open('/dev/null') kept in a list"
    with open(out, "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
