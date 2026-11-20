import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from leakslope import cli, core  # noqa: E402


class FakeProc(unittest.TestCase):
    def test_snapshot_and_kinds(self):
        with tempfile.TemporaryDirectory() as tmp:
            fd = os.path.join(tmp, "fd")
            os.mkdir(fd)
            for name, target in [("0", "/dev/null"), ("1", "pipe:[11]"),
                                 ("2", "socket:[22]"), ("3", "anon_inode:[eventfd]")]:
                os.symlink(target, os.path.join(fd, name))
            snap = core.snapshot(proc=tmp)
            self.assertEqual(snap.fds, 4)
            self.assertEqual([core.fd_kind(t) for t in snap.fd_targets],
                             ["file", "anon_inode", "pipe", "socket"])


class Ols(unittest.TestCase):
    def test_exact_line(self):
        xs = [1, 2, 4, 8, 16, 32]
        slope, intercept, r2 = core.ols(xs, [2 * x + 7 for x in xs])
        self.assertAlmostEqual(slope, 2.0, places=12)
        self.assertAlmostEqual(intercept, 7.0, places=9)
        self.assertAlmostEqual(r2, 1.0, places=12)

    def test_flat(self):
        slope, _, r2 = core.ols([1, 2, 4, 8], [3, 3, 3, 3])
        self.assertAlmostEqual(slope, 0.0, places=12)
        self.assertAlmostEqual(r2, 1.0, places=12)


class Labels(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(core._classify([0, 0, 0], 0.0, 1.0), "clean")
        self.assertEqual(core._classify([1, 2, 4, 8, 16, 32], 1.0, 1.0), "leak")
        self.assertEqual(core._classify([1, 1, 1, 1], 0.0, 1.0), "one-time")
        self.assertEqual(core._classify([0, 0, 0, 0, 1, 3], 0.0995, 0.95), "slow")
        self.assertEqual(core._classify([0, 0, 1, 2, 4, 8], 0.25, 0.95), "ambiguous")
        # growth that is not a clean line is not promoted to "leak"
        self.assertEqual(core._classify([0, 0, 0, 0, 16, 32], 1.0, 0.60), "ambiguous")


class ScheduleValidation(unittest.TestCase):
    def test_single_block_rejected(self):
        with self.assertRaises(ValueError):
            core.probe(lambda: None, blocks=(32,))

    def test_empty_blocks_rejected(self):
        with self.assertRaises(ValueError):
            core.probe(lambda: None, blocks=())


class RealLeaks(unittest.TestCase):
    def test_file_leak_detected_with_attribution(self):
        handles = []
        with tempfile.TemporaryDirectory() as tmp:
            def leaky():
                handles.append(open(os.path.join(tmp, f"{len(handles)}.txt"), "w"))
            try:
                result = core.probe(leaky, blocks=(1, 1, 2, 4))
            finally:
                for h in handles:
                    h.close()
        self.assertEqual(result["verdict"], "leak")
        fd_axis = result["axes"][0]
        self.assertEqual(fd_axis["total"], 8)
        self.assertAlmostEqual(fd_axis["per_call"], 1.0, places=9)
        self.assertEqual(fd_axis["attribution"].get("file"), 8)

    def test_clean_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.txt")

            def clean():
                with open(path, "w") as fh:
                    fh.write("x")
            result = core.probe(clean, blocks=(1, 1, 2, 4, 8))
        self.assertEqual(result["verdict"], "clean")
        self.assertEqual(result["axes"][0]["total"], 0)

    def test_one_time_init_is_not_a_leak_but_naive_flags_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lazy.txt")

            def make():
                state = {"fh": None}

                def lazy():
                    if state["fh"] is None:
                        state["fh"] = open(path, "w")
                    state["fh"].write("x")
                    state["fh"].flush()
                return lazy, state
            lazy, state = make()
            result = core.probe(lazy, blocks=(1, 1, 2, 4, 8))
            state["fh"].close()
            self.assertEqual(result["verdict"], "one-time")
            self.assertEqual(result["axes"][0]["total"], 1)
            lazy2, state2 = make()
            naive = core.naive_delta(lazy2, calls=32)
            state2["fh"].close()
        self.assertTrue(naive["flags"])
        self.assertEqual(naive["fd_growth"], 1)

    def test_thread_leak_detected(self):
        stop = threading.Event()
        threads = []

        def leaky():
            t = threading.Thread(target=stop.wait, daemon=True)
            t.start()
            threads.append(t)
        try:
            result = core.probe(leaky, blocks=(1, 1, 2, 4))
        finally:
            stop.set()
            for t in threads:
                t.join(timeout=5)
        self.assertEqual(result["verdict"], "leak")
        th_axis = result["axes"][1]
        self.assertEqual(th_axis["total"], 8)
        self.assertAlmostEqual(th_axis["per_call"], 1.0, places=9)

    def test_slow_leak_is_reported_as_slow(self):
        handles = []
        state = {"n": 0}
        with tempfile.TemporaryDirectory() as tmp:
            def slow():
                state["n"] += 1
                if state["n"] % 10 == 0:
                    handles.append(open(os.path.join(tmp, f"{len(handles)}.txt"), "w"))
            try:
                result = core.probe(slow, blocks=(1, 1, 2, 4, 8, 16))
            finally:
                for h in handles:
                    h.close()
        self.assertEqual(result["verdict"], "slow")
        self.assertAlmostEqual(result["axes"][0]["per_call"], 0.0995, places=4)
        self.assertEqual(result["axes"][0]["total"], 3)

    def test_aa_runs_are_clean(self):
        for _ in range(12):
            result = core.probe(lambda: None, blocks=(1, 1, 2, 4, 8))
            self.assertEqual(result["verdict"], "clean", result)


class CliBehaviour(unittest.TestCase):
    def _run(self, source, *argv):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "target.py")
            with open(path, "w") as fh:
                fh.write(source)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cli.main(["probe", f"{path}:" + argv[0], *argv[1:]])
            return code, buf.getvalue()

    def test_clean_exit_zero(self):
        code, out = self._run(
            "def f():\n    with open('/dev/null') as fh:\n        fh.read()\n", "f")
        self.assertEqual(code, 0)
        self.assertIn("verdict     clean", out)

    def test_leak_exit_one_and_json(self):
        code, out = self._run(
            "KEEP = []\n"
            "def f():\n"
            "    KEEP.append(open('/dev/null'))\n", "f", "--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload["verdict"], "leak")
        self.assertAlmostEqual(payload["axes"][0]["per_call"], 1.0, places=9)

    def test_single_block_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "target.py")
            with open(path, "w") as fh:
                fh.write("def f():\n    return 1\n")
            for blocks in ("32", ""):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as ctx:
                        cli.main(["probe", f"{path}:f", "--blocks", blocks])
                self.assertEqual(ctx.exception.code, 2)

    def test_slow_leak_exit_one(self):
        code, out = self._run(
            "KEEP = []\n"
            "STATE = {'n': 0}\n"
            "def f():\n"
            "    STATE['n'] += 1\n"
            "    if STATE['n'] % 10 == 0:\n"
            "        KEEP.append(open('/dev/null'))\n", "f")
        self.assertEqual(code, 1)
        self.assertIn("label=slow", out)

    def test_naive_runs_in_a_fresh_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "target.py")
            db = os.path.join(tmp, "lazy.txt")
            with open(path, "w") as fh:
                fh.write(
                    "STATE = {'fh': None}\n"
                    "def lazy():\n"
                    "    if STATE['fh'] is None:\n"
                    f"        STATE['fh'] = open({db!r}, 'w')\n"
                    "    STATE['fh'].write('x')\n")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cli.main(["probe", f"{path}:lazy", "--naive", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["verdict"], "one-time")
        self.assertTrue(payload["naive"]["flags"])
        self.assertEqual(payload["naive"]["fd_growth"], 1)

    def test_naive_flag_in_output(self):
        code, out = self._run(
            "def f():\n    return 1\n", "f", "--naive")
        self.assertEqual(code, 0)
        self.assertIn("naive rule", out)


if __name__ == "__main__":
    unittest.main()
