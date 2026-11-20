"""A function that opens a resource on the first call and reuses it."""

STATE = {"fh": None}


def lazy():
    if STATE["fh"] is None:
        STATE["fh"] = open("/dev/null", "w")
    STATE["fh"].write("x")
