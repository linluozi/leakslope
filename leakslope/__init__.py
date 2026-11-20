"""leakslope — per-call OS resource leak adjudication."""

from .core import DEFAULT_BLOCKS, Snapshot, fd_kind, naive_delta, ols, probe, snapshot

__all__ = ["DEFAULT_BLOCKS", "Snapshot", "fd_kind", "naive_delta", "ols", "probe", "snapshot"]
__version__ = "0.1.0"
