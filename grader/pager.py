"""Layer 1 grading: crash a pager at random points and check what survived."""
import os
import random
import subprocess
import threading
from dataclasses import dataclass, field

from .disk import Disk, DiskServer

PAGES = 64
MAX_PAYLOAD = 4000
TIMEOUT = 10  # seconds to wait for one reply


class Learner:
    """The binary under test, driven one line at a time."""

    def __init__(self, binary, sock_path, verbose=False):
        env = dict(os.environ, DISK_SOCKET=sock_path)
        self.proc = subprocess.Popen(
            [os.path.abspath(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=None if verbose else subprocess.DEVNULL, env=env)
        self.timed_out = False

    def _timeout(self):
        self.timed_out = True
        self.proc.kill()

    def ask(self, line):
        """Send a command, return the reply line, or None if the process died."""
        try:
            self.proc.stdin.write(line.encode() + b"\n")
            self.proc.stdin.flush()
        except OSError:
            return None
        timer = threading.Timer(TIMEOUT, self._timeout)
        timer.start()
        try:
            reply = self.proc.stdout.readline()
        finally:
            timer.cancel()
        return reply.decode().strip() if reply else None

    def finish(self):
        """Ask for a clean exit, and kill the process if it lingers."""
        try:
            self.proc.stdin.write(b"exit\n")
            self.proc.stdin.flush()
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self._close()

    def kill(self):
        self.proc.kill()
        self.proc.wait()
        self._close()

    def _close(self):
        for pipe in (self.proc.stdin, self.proc.stdout):
            try:
                pipe.close()
            except OSError:
                pass


@dataclass
class Bad:
    page: int
    why: str
    got: str
    synced: bytes
    pending: list
    faults: list


@dataclass
class Result:
    seed: int
    error: str = None          # the binary misbehaved before we could judge it
    bad: list = field(default_factory=list)   # pages that failed, in page order
    report: object = None
    ops: int = 0
    kill_at: int = 0
    when: str = ""
    corrupt: int = 0           # pages the binary reported corrupt
    checked: int = 0

    @property
    def ok(self):
        return not self.error and not self.bad


def make_ops(rng):
    """A random mix of writes and syncs. Payloads are random bytes, often longer than a sector."""
    ops = []
    for _ in range(rng.randint(30, 150)):
        if rng.random() < 0.85:
            size = rng.randint(1, 300) if rng.random() < 0.25 else rng.randint(600, MAX_PAYLOAD)
            ops.append(("write", rng.randrange(PAGES), rng.getrandbits(8 * size).to_bytes(size, "little")))
        else:
            ops.append(("sync", None, None))
    return ops


def judge(page, got, synced, pending, history, torn):
    """None if the page is acceptable, else a reason."""
    if got == "corrupt":
        # Only a torn write can honestly leave a page unreadable.
        return None if torn else "page reported corrupt, but no write to it was torn"
    allowed = list(pending)
    if page in synced:
        allowed.append(synced[page])
    elif got == "empty":
        return None
    if got == "empty":
        return "page was synced but came back empty"
    if got in allowed:
        return None
    if got in history.get(page, []):
        return "page came back older than its last sync"
    return "silent corruption: returned data that was never written"


def run_once(binary, seed, verbose=False):
    rng = random.Random("workload:%d" % seed)
    ops = make_ops(rng)
    res = Result(seed, ops=len(ops), kill_at=rng.randint(1, len(ops)), when=rng.choice(("before", "after")))
    disk = Disk()
    server = DiskServer(disk)
    try:
        run_workload(binary, ops, res, disk, server, verbose)
    finally:
        server.close()
    return res


def run_workload(binary, ops, res, disk, server, verbose):
    synced, pending, history = {}, {}, {}
    learner = Learner(binary, server.path, verbose)
    server.arm(learner.proc, res.kill_at, res.when)
    for kind, page, data in ops:
        if kind == "write":
            pending.setdefault(page, []).append(data)  # counts even if the crash eats the reply
            history.setdefault(page, []).append(data)
            line = "write %d %s" % (page, data.hex())
        else:
            line = "sync"
        reply = learner.ask(line)
        if reply is None:
            if not server.crashed.is_set():
                learner.kill()
                res.error = "no reply to '%s' in %ds" % (line.split()[0], TIMEOUT) if learner.timed_out \
                    else "binary exited early on '%s'" % line.split()[0]
                return
            break
        if reply != "ok":
            learner.kill()
            res.error = "expected 'ok' for '%s', got '%s'" % (line.split()[0], reply[:60])
            return
        if kind == "sync":
            for p, writes in pending.items():
                synced[p] = writes[-1]
            pending = {}
    learner.kill()  # a no-op if the disk already did it
    server.disarm()
    with server.lock:
        res.report = disk.crash(res.seed)

    learner = Learner(binary, server.path, verbose)
    torn = {f.page for f in res.report.faults if f.kind == "torn"}
    for page in range(PAGES):
        reply = learner.ask("read %d" % page)
        got = None
        if reply in ("empty", "corrupt"):
            got = reply
        elif reply:
            try:
                got = bytes.fromhex(reply)
            except ValueError:
                pass
        if got is None or (isinstance(got, bytes) and not 0 < len(got) <= MAX_PAYLOAD):
            learner.kill()
            res.error = "bad reply to 'read %d': '%s'" % (page, (reply or "")[:60])
            return
        res.checked += 1
        res.corrupt += got == "corrupt"
        why = judge(page, got, synced, pending.get(page, []), history, page in torn)
        if why:
            faults = [f for f in res.report.faults if f.page == page]
            res.bad.append(Bad(page, why, got, synced.get(page), pending.get(page, []), faults))
    learner.finish()
