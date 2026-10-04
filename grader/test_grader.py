"""Tests for the fault model, the disk server and the layer 1 grader.

Run from the repo root:  python3 -m unittest grader.test_grader -v
"""
import os
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grader import pager
from grader.disk import PAGE, SECTOR, SECTORS, Disk, DiskServer, Faults

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def page_of(byte):
    return bytes([byte]) * PAGE


def disk_with(*writes):
    d = Disk()
    for page, data in writes:
        d.write(page, data)
    return d


class FaultModel(unittest.TestCase):
    def test_fsynced_writes_survive_any_crash(self):
        d = disk_with((1, page_of(1)), (2, page_of(2)))
        d.fsync()
        d.crash(7, Faults(drop=1.0))
        self.assertEqual(d.durable, {1: page_of(1), 2: page_of(2)})

    def test_drop(self):
        d = disk_with((1, page_of(1)), (2, page_of(2)))
        report = d.crash(3, Faults(drop=1.0))
        self.assertEqual(d.durable, {})
        self.assertEqual([f.kind for f in report.faults], ["dropped", "dropped"])

    def test_apply(self):
        d = disk_with((1, page_of(1)), (2, page_of(2)))
        d.crash(3, Faults(drop=0.0, tear=0.0))
        self.assertEqual(d.durable, {1: page_of(1), 2: page_of(2)})

    def test_tear_keeps_whole_sectors_of_old_and_new(self):
        old, new = page_of(0xAA), page_of(0xBB)
        d = Disk()
        d.write(5, old)
        d.fsync()
        d.write(5, new)
        report = d.crash(11, Faults(drop=0.0, tear=1.0))
        landed = report.faults[0].sectors
        self.assertTrue(0 < len(landed) < SECTORS)
        image = d.durable[5]
        for s in range(SECTORS):
            sector = image[s * SECTOR:(s + 1) * SECTOR]
            self.assertEqual(sector, (new if s in landed else old)[:SECTOR])

    def test_same_seed_same_disk(self):
        writes = [(i % 5, page_of(i + 1)) for i in range(20)]
        for seed in range(10):
            a, b = disk_with(*writes), disk_with(*writes)
            ra, rb = a.crash(seed), b.crash(seed)
            self.assertEqual(a.durable, b.durable)
            self.assertEqual(ra, rb)

    def test_different_seeds_differ(self):
        writes = [(i % 5, page_of(i + 1)) for i in range(20)]
        images = set()
        for seed in range(10):
            d = disk_with(*writes)
            d.crash(seed)
            images.add(repr(sorted(d.durable.items())))
        self.assertGreater(len(images), 5)

    def test_default_odds_produce_all_three_outcomes(self):
        kinds = set()
        for seed in range(20):
            report = disk_with(*[(i, page_of(i + 1)) for i in range(10)]).crash(seed)
            kinds |= {f.kind for f in report.faults}
        self.assertEqual(kinds, {"dropped", "applied", "torn"})

    def test_reorder_is_deterministic_and_changes_the_winner(self):
        winners = {}
        for seed in range(20):
            d = disk_with((9, page_of(1)), (9, page_of(2)))
            d.crash(seed, Faults(drop=0.0, tear=0.0, reorder=1.0))
            winners[seed] = d.durable[9][0]
            again = disk_with((9, page_of(1)), (9, page_of(2)))
            again.crash(seed, Faults(drop=0.0, tear=0.0, reorder=1.0))
            self.assertEqual(again.durable[9][0], winners[seed])
        self.assertEqual(set(winners.values()), {1, 2})

    def test_no_reorder_keeps_issue_order(self):
        for seed in range(10):
            d = disk_with((9, page_of(1)), (9, page_of(2)))
            report = d.crash(seed, Faults(drop=0.0, tear=0.0, reorder=0.0))
            self.assertFalse(report.reordered)
            self.assertEqual(d.durable[9], page_of(2))

    def test_reads_see_unsynced_writes_and_zeros_for_new_pages(self):
        d = disk_with((4, page_of(4)))
        self.assertEqual(d.read(4), page_of(4))
        self.assertEqual(d.read(8), bytes(PAGE))
        self.assertEqual(d.size(), 5)


class DiskServerWire(unittest.TestCase):
    def setUp(self):
        self.disk = Disk()
        self.server = DiskServer(self.disk)
        self.conn = socket.socket(socket.AF_UNIX)
        self.conn.connect(self.server.path)
        self.f = self.conn.makefile("rwb")

    def tearDown(self):
        self.conn.close()
        self.server.close()

    def call(self, body):
        self.f.write(struct.pack(">I", len(body)) + body)
        self.f.flush()
        (n,) = struct.unpack(">I", self.f.read(4))
        return self.f.read(n)

    def test_round_trip(self):
        self.assertEqual(self.call(bytes([4])), b"\0" + struct.pack(">I", 0))
        self.assertEqual(self.call(bytes([1]) + struct.pack(">I", 3)), b"\0" + bytes(PAGE))
        self.assertEqual(self.call(bytes([2]) + struct.pack(">I", 3) + page_of(7)), b"\0")
        self.assertEqual(self.call(bytes([1]) + struct.pack(">I", 3)), b"\0" + page_of(7))
        self.assertEqual(self.call(bytes([3])), b"\0")
        self.assertEqual(self.disk.durable[3], page_of(7))
        self.assertEqual(self.call(bytes([4])), b"\0" + struct.pack(">I", 4))

    def test_bad_request_gets_an_error(self):
        self.assertEqual(self.call(bytes([2, 0, 0, 0, 1, 9])), b"\1bad request")
        self.assertEqual(self.call(bytes([99])), b"\1bad request")


# A small pager in Python, so the tests can also check pagers that are wrong in other ways.
PY_PAGER = '''#!/usr/bin/env python3
import os, socket, struct, sys, zlib
MODE = "%s"
f = socket.socket(socket.AF_UNIX)
f.connect(os.environ["DISK_SOCKET"])
f = f.makefile("rwb")
def call(body):
    f.write(struct.pack(">I", len(body)) + body); f.flush()
    return f.read(struct.unpack(">I", f.read(4))[0])[1:]
for line in sys.stdin:
    w = line.split()
    if w[0] == "write":
        body = struct.pack(">H", len(w[2]) // 2) + bytes.fromhex(w[2])
        body = body.ljust(PAGE - 4, b"\\0")
        call(bytes([2]) + struct.pack(">I", int(w[1])) + struct.pack(">I", zlib.crc32(body)) + body)
        print("ok", flush=True)
    elif w[0] == "sync":
        if MODE != "nosync":
            call(bytes([3]))
        print("ok", flush=True)
    elif w[0] == "read":
        raw = call(bytes([1]) + struct.pack(">I", int(w[1])))
        if MODE == "corrupt":
            print("corrupt", flush=True)
        elif not any(raw):
            print("empty", flush=True)
        elif zlib.crc32(raw[4:]) != struct.unpack(">I", raw[:4])[0]:
            print("corrupt", flush=True)
        else:
            n = struct.unpack(">H", raw[4:6])[0]
            print(raw[6:6 + n].hex(), flush=True)
    elif w[0] == "exit":
        break
'''.replace("PAGE", "4096")


class Grader(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="gradertest-")
        cls.bins = {}
        if shutil.which("go"):
            for name in ("pager-naive", "pager-checksum"):
                out = os.path.join(cls.tmp, name)
                src = os.path.join(ROOT, "solutions", name, "main.go")
                if os.path.exists(src):
                    subprocess.run(["go", "build", "-o", out, src], check=True)
                    cls.bins[name] = out
        for mode in ("ok", "corrupt", "nosync"):
            path = os.path.join(cls.tmp, "py-" + mode)
            with open(path, "w") as fh:
                fh.write(PY_PAGER % mode)
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
            cls.bins["py-" + mode] = path

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def failures(self, name, seeds):
        results = [pager.run_once(self.bins[name], s) for s in seeds]
        return [r for r in results if not r.ok]

    def need(self, name):
        if name not in self.bins:
            self.skipTest("%s not available (needs Go and solutions/)" % name)

    def test_naive_pager_fails_with_silent_corruption(self):
        self.need("pager-naive")
        bad = self.failures("pager-naive", range(1, 41))
        self.assertTrue(bad, "naive pager should fail within 40 seeds")
        self.assertIn("silent corruption", bad[0].bad[0].why)

    def test_failing_seed_replays(self):
        self.need("pager-naive")
        seed = self.failures("pager-naive", range(1, 41))[0].seed
        a = pager.run_once(self.bins["pager-naive"], seed)
        b = pager.run_once(self.bins["pager-naive"], seed)
        self.assertFalse(a.ok)
        self.assertEqual((a.bad[0].page, a.bad[0].got), (b.bad[0].page, b.bad[0].got))

    def test_checksum_pager_passes_200_runs(self):
        self.need("pager-checksum")
        self.assertEqual(self.failures("pager-checksum", range(1, 201)), [])

    def test_python_checksum_pager_passes(self):
        self.assertEqual(self.failures("py-ok", range(1, 41)), [])

    def test_pager_that_never_fsyncs_loses_synced_data(self):
        bad = self.failures("py-nosync", range(1, 41))
        self.assertTrue(bad)
        reasons = " ".join(b.why for r in bad for b in r.bad)
        self.assertIn("came back", reasons)

    def test_pager_that_always_says_corrupt_fails(self):
        bad = self.failures("py-corrupt", range(1, 11))
        self.assertEqual(len(bad), 10)
        self.assertIn("no write to it was torn", bad[0].bad[0].why)

    def test_a_binary_that_dies_is_reported_not_hung(self):
        res = pager.run_once("/usr/bin/true", 1)
        self.assertTrue(res.error)


if __name__ == "__main__":
    unittest.main()
