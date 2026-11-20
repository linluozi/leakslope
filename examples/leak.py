"""A function that leaks one file descriptor per call."""
KEEP = []


def leak():
    KEEP.append(open("/dev/null"))
