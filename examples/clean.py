"""A function that opens and closes one file descriptor per call."""


def clean():
    with open("/dev/null") as fh:
        fh.read()
