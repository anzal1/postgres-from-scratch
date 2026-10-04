"""A virtual block device that lies the way real disks do.

The disk keeps a durable image plus a log of writes that have not been
fsynced. FSYNC makes the log durable. crash() decides, from a seed, what
happened to each unsynced write: dropped, applied, or torn. See faults.md.
The wire format is in PROTOCOL.md.
"""
import os
import random
import shutil
import socket
import struct
import tempfile
import threading
from collections import namedtuple

SECTOR = 512
PAGE = 4096
SECTORS = PAGE // SECTOR
READ, WRITE, FSYNC, SIZE = 1, 2, 3, 4

Fault = namedtuple("Fault", "seq page kind sectors")  # kind: dropped, applied, torn
CrashReport = namedtuple("CrashReport", "faults reordered")


class Faults:
    """Odds for one unsynced write. Whatever drop and tear leave over is applied."""

    def __init__(self, drop=0.35, tear=0.30, reorder=0.5):
        self.drop, self.tear, self.reorder = drop, tear, reorder


class Disk:
    def __init__(self):
        self.durable = {}   # page_no -> 4096 bytes, survives a crash
        self.pending = []   # (seq, page_no, data) not yet fsynced, oldest first
        self.nwrites = 0

    def read(self, page):
        for _, p, data in reversed(self.pending):  # unsynced data is visible, like a page cache
            if p == page:
                return data
        return self.durable.get(page, bytes(PAGE))

    def write(self, page, data):
        self.nwrites += 1
        self.pending.append((self.nwrites, page, data))

    def fsync(self):
        for _, page, data in self.pending:
            self.durable[page] = data
        self.pending = []

    def size(self):
        pages = [p for _, p, _ in self.pending] + list(self.durable)
        return max(pages) + 1 if pages else 0

    def crash(self, seed, faults=None):
        """Resolve every unsynced write. Same seed and same writes, same disk."""
        faults = faults or Faults()
        rng = random.Random("crash:%d" % seed)
        lands, report = [], []
        for seq, page, data in self.pending:
            r = rng.random()
            if r < faults.drop:
                report.append(Fault(seq, page, "dropped", ()))
            elif r < faults.drop + faults.tear:
                sectors = tuple(sorted(rng.sample(range(SECTORS), rng.randint(1, SECTORS - 1))))
                lands.append((seq, page, data, sectors, "torn"))
            else:
                lands.append((seq, page, data, tuple(range(SECTORS)), "applied"))
        order = list(lands)
        if rng.random() < faults.reorder:
            rng.shuffle(order)
        for seq, page, data, sectors, kind in order:
            image = bytearray(self.durable.get(page, bytes(PAGE)))
            for s in sectors:
                image[s * SECTOR:(s + 1) * SECTOR] = data[s * SECTOR:(s + 1) * SECTOR]
            self.durable[page] = bytes(image)
            report.append(Fault(seq, page, kind, sectors))
        self.pending = []
        reordered = [x[0] for x in order] != [x[0] for x in lands]
        return CrashReport(sorted(report), reordered)


def recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


class DiskServer:
    """Serves a Disk on a Unix socket, and can SIGKILL the learner mid-request."""

    def __init__(self, disk):
        self.disk = disk
        self.dir = tempfile.mkdtemp(prefix="pp-")
        self.path = os.path.join(self.dir, "disk.sock")
        self.lock = threading.Lock()
        self.crashed = threading.Event()
        self.victim = None
        self.kill_at = None
        self.when = "after"
        self.count = 0
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        self.sock.listen(4)
        threading.Thread(target=self._accept, daemon=True).start()

    def arm(self, victim, kill_at, when):
        """Kill victim when its kill_at-th WRITE or FSYNC arrives, before or after applying it."""
        self.victim, self.kill_at, self.when, self.count = victim, kill_at, when, 0
        self.crashed.clear()

    def disarm(self):
        self.kill_at = None

    def close(self):
        self.sock.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        while True:
            head = recv_exact(conn, 4)
            body = recv_exact(conn, struct.unpack(">I", head)[0]) if head else None
            if body is None:
                break
            with self.lock:
                hit = False
                if body[:1] in (bytes([WRITE]), bytes([FSYNC])) and self.kill_at is not None:
                    self.count += 1
                    hit = self.count == self.kill_at
                if hit and self.when == "before":
                    self._fire(conn)
                    return
                reply = self._apply(body)
                if hit:
                    self._fire(conn)
                    return
            conn.sendall(struct.pack(">I", len(reply)) + reply)
        conn.close()

    def _fire(self, conn):
        self.crashed.set()
        self.victim.kill()
        conn.close()

    def _apply(self, body):
        op = body[0] if body else 0
        if op == READ and len(body) == 5:
            return b"\0" + self.disk.read(struct.unpack(">I", body[1:5])[0])
        if op == WRITE and len(body) == 5 + PAGE:
            self.disk.write(struct.unpack(">I", body[1:5])[0], body[5:])
            return b"\0"
        if op == FSYNC and len(body) == 1:
            self.disk.fsync()
            return b"\0"
        if op == SIZE and len(body) == 1:
            return b"\0" + struct.pack(">I", self.disk.size())
        return b"\1bad request"
