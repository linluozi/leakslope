"""leakslope — per-call OS resource leak adjudication.

The unit of measurement is a *slope*: resources as a function of the
cumulative number of calls.  A one-time lazy initialisation produces a step
(slope ~ 0), a real leak produces a line (slope = k).  The naive
"snapshot before / snapshot after" rule cannot tell the two apart.
"""

from __future__ import annotations

import collections
import os
import threading
from dataclasses import dataclass

PROC = "/proc/self"
DEFAULT_BLOCKS = (1, 1, 2, 4, 8, 16)


@dataclass(frozen=True)
class Snapshot:
    fds: int
    fd_targets: tuple
    threads: int


def snapshot(proc: str = PROC) -> Snapshot:
    """Count open fds and live threads, and record what each fd points at."""
    fd_dir = os.path.join(proc, "fd")
    try:
        names = os.listdir(fd_dir)
    except OSError:
        # Non-Linux fallback: count what we can via os.listdir of /dev/fd.
        names = os.listdir("/dev/fd")
        fd_dir = "/dev/fd"
    targets = []
    for name in names:
        try:
            targets.append(os.readlink(os.path.join(fd_dir, name)))
        except OSError:
            targets.append("<gone>")
    return Snapshot(len(names), tuple(sorted(targets)), threading.active_count())


def fd_kind(target: str) -> str:
    """Classify an fd target string from /proc/self/fd/<n>."""
    if target.startswith("socket:"):
        return "socket"
    if target.startswith("pipe:"):
        return "pipe"
    if target.startswith("anon_inode:"):
        return "anon_inode"
    if target == "<gone>":
        return "<gone>"
    return "file"


def ols(xs, ys):
    """Ordinary least squares; returns (slope, intercept, r_squared)."""
    n = len(xs)
    if n < 2:
        return 0.0, float(ys[0]) if ys else 0.0, 1.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, my, 1.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 if ss_tot == 0 else 1.0 - ss_res / ss_tot
    return slope, intercept, r2


def _classify(ys, slope: float, r2: float,
              leak_slope: float = 0.5, ambiguous_slope: float = 0.2) -> str:
    """Label a growth series.

    A genuine one-time initialisation is not "small slope": it is growth that
    *stops* after the first measured point.  Growth that keeps accumulating is a
    leak, even if the per-call rate is below ``leak_slope`` -- it is reported as
    ``slow`` instead of being hidden behind the one-time label.
    """
    if not ys:
        return "clean"
    total = ys[-1]
    if total == 0:
        return "clean"
    if ys[0] > 0 and all(g <= ys[0] for g in ys[1:]):
        return "one-time"
    if slope >= leak_slope and r2 >= 0.90:
        return "leak"
    if abs(slope) >= ambiguous_slope:
        return "ambiguous"
    return "slow"


@dataclass
class Axis:
    name: str
    total: int
    slope: float
    r2: float
    label: str
    attribution: dict

    def as_dict(self):
        return {
            "axis": self.name,
            "total": self.total,
            "per_call": round(self.slope, 4),
            "r2": round(self.r2, 4),
            "label": self.label,
            "attribution": self.attribution,
        }


def _attribution(before: Snapshot, after: Snapshot) -> dict:
    new = collections.Counter(after.fd_targets) - collections.Counter(before.fd_targets)
    out = collections.Counter()
    for target, count in new.items():
        out[fd_kind(target)] += count
    return dict(sorted(out.items()))


def probe(fn, blocks=DEFAULT_BLOCKS, proc: str = PROC) -> dict:
    """Call ``fn`` with a doubling schedule; fit resources vs cumulative calls.

    ``fn`` is called once per unit, so cumulative call counts are the running
    sum of ``blocks`` (default 1,2,4,8,16,32).  No warmup is performed: the
    point is to keep a possible first-call initialisation inside the window.
    """
    if len(blocks) < 2:
        raise ValueError("probe needs at least two call blocks; "
                         "a single block cannot estimate a slope")
    if any(int(b) <= 0 for b in blocks):
        raise ValueError("block sizes must be positive integers")
    base = snapshot(proc)
    xs, fd_ys, th_ys = [], [], []
    calls = 0
    for block in blocks:
        for _ in range(block):
            fn()
        calls += block
        snap = snapshot(proc)
        xs.append(calls)
        fd_ys.append(snap.fds - base.fds)
        th_ys.append(snap.threads - base.threads)
    end = snapshot(proc)

    fd_slope, _, fd_r2 = ols(xs, fd_ys)
    th_slope, _, th_r2 = ols(xs, th_ys)
    fd_axis = Axis("fd", fd_ys[-1], fd_slope, fd_r2,
                   _classify(fd_ys, fd_slope, fd_r2), _attribution(base, end))
    th_axis = Axis("thread", th_ys[-1], th_slope, th_r2,
                   _classify(th_ys, th_slope, th_r2), {})
    axes = [fd_axis, th_axis]
    labels = {a.label for a in axes}
    if "leak" in labels:
        overall = "leak"
    elif "slow" in labels:
        overall = "slow"
    elif "ambiguous" in labels:
        overall = "ambiguous"
    elif "one-time" in labels:
        overall = "one-time"
    else:
        overall = "clean"
    return {
        "calls": xs,
        "fd_growth": fd_ys,
        "thread_growth": th_ys,
        "axes": [a.as_dict() for a in axes],
        "verdict": overall,
        "total_calls": calls,
    }


def naive_delta(fn, calls: int = 32, proc: str = PROC) -> dict:
    """The common before/after rule, kept only as a comparison baseline."""
    before = snapshot(proc)
    for _ in range(calls):
        fn()
    after = snapshot(proc)
    return {
        "calls": calls,
        "fd_growth": after.fds - before.fds,
        "thread_growth": after.threads - before.threads,
        "flags": (after.fds - before.fds) > 0 or (after.threads - before.threads) > 0,
    }
