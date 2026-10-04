"""Command line front end: ./grade <layer> ..."""
import argparse
import hashlib
import os
import sys
import time

from . import pager


def tag(word, tty):
    if not tty or os.environ.get("NO_COLOR"):
        return word
    return "\033[1;%sm%s\033[0m" % ("32" if word == "PASS" else "31", word)


def digest(data):
    return "%d bytes  sha %s" % (len(data), hashlib.sha256(data).hexdigest()[:8])


def describe_fault(f, reordered):
    if f.kind == "dropped":
        what = "never reached the disk"
    elif f.kind == "applied":
        what = "landed in full"
    else:
        lost = [s for s in range(8) if s not in f.sectors]
        what = "torn, sectors %s landed, %s lost" % (
            " ".join(map(str, f.sectors)), " ".join(map(str, lost)))
    return "write #%d %s" % (f.seq, what) + ("  (out of order)" if reordered and f.kind != "dropped" else "")


def show_failure(res, bin_arg, tty):
    if res.error:
        print("%s  seed %d  %s" % (tag("FAIL", tty), res.seed, res.error))
    else:
        b = res.bad[0]
        more = "  (+%d more pages)" % (len(res.bad) - 1) if len(res.bad) > 1 else ""
        print("%s  seed %d  page %d  %s%s" % (tag("FAIL", tty), res.seed, b.page, b.why, more))
        print()
        rows = [("last synced", digest(b.synced) if b.synced else "nothing, the page was empty")]
        rows += [("written since", digest(w)) for w in b.pending] or [("written since", "nothing")]
        rows.append(("came back", b.got if isinstance(b.got, str) else digest(b.got) + "  (matches none of the above)"))
        rows += [("the disk", describe_fault(f, res.report.reordered)) for f in b.faults]
        for label, text in rows:
            print("    %-16s%s" % (label, text))
    print()
    print("    replay it:")
    print("      ./grade pager --bin %s --seed %d --runs 1 --verbose" % (bin_arg, res.seed))
    print()


def verbose_line(res):
    if res.error:
        return "seed %d  %s" % (res.seed, res.error)
    kinds = [f.kind for f in res.report.faults]
    return "seed %d  %d ops  crash %s write/sync #%d  dropped %d  applied %d  torn %d%s  %d pages checked" % (
        res.seed, res.ops, res.when, res.kill_at, kinds.count("dropped"), kinds.count("applied"),
        kinds.count("torn"), "  reordered" if res.report.reordered else "", res.checked)


def grade_pager(args):
    tty = sys.stdout.isatty()
    if not os.access(args.bin, os.X_OK):
        sys.exit("grade: %s is not an executable file" % args.bin)
    first, last = args.seed, args.seed + args.runs - 1
    print("pager  layer 1  seeds %d to %d  %s" % (first, last, args.bin))
    print()
    totals = {"dropped": 0, "applied": 0, "torn": 0, "reordered": 0, "corrupt": 0, "checked": 0}
    failed, ran, started = [], 0, time.time()
    for seed in range(first, last + 1):
        res = pager.run_once(args.bin, seed, args.verbose)
        ran += 1
        if args.verbose:
            print(verbose_line(res))
            for f in (res.report.faults if res.report else []):
                print("    " + describe_fault(f, res.report.reordered))
        elif tty:
            print("\r  run %d of %d" % (ran, args.runs), end="", flush=True)
        if res.report:
            for f in res.report.faults:
                totals[f.kind] += 1
            totals["reordered"] += res.report.reordered
        totals["corrupt"] += res.corrupt
        totals["checked"] += res.checked
        if not res.ok:
            failed.append(res)
            if tty and not args.verbose:
                print("\r\033[K", end="")
            if len(failed) == 1:
                show_failure(res, args.bin, tty)
            if not args.keep_going:
                break
    if tty and not args.verbose and not failed:
        print("\r\033[K", end="")
    secs = time.time() - started
    if failed:
        if args.keep_going:
            print("%s  pager  %d of %d runs failed, first at seed %d  (%.1fs)" % (
                tag("FAIL", tty), len(failed), ran, failed[0].seed, secs))
        else:
            print("%s  pager  failed at seed %d, run %d of %d  (%.1fs)" % (
                tag("FAIL", tty), failed[0].seed, ran, args.runs, secs))
        return 1
    print("%s  pager  %d of %d runs survived  %d pages checked  (%.1fs)" % (
        tag("PASS", tty), ran, ran, totals["checked"], secs))
    print("      disk faults       %d dropped, %d applied, %d torn, reordered in %d runs" % (
        totals["dropped"], totals["applied"], totals["torn"], totals["reordered"]))
    print("      reported corrupt  %d pages, none returned wrong data" % totals["corrupt"])
    return 0


COMING = ["pool", "btree", "wal", "mvcc", "sql", "exec", "plan", "locks",
          "wire", "replica", "orm", "chaos"]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in COMING:
        print("the %s grader isn't out yet. layer 1 (pager) is live, the rest land in order." % argv[0])
        print("watch the repo to get each one as it ships.")
        return 2
    ap = argparse.ArgumentParser(prog="grade", description="Postgres From Scratch graders.")
    sub = ap.add_subparsers(dest="layer", required=True)
    p = sub.add_parser("pager", help="layer 1: fixed-size pages and checksums")
    p.add_argument("--bin", required=True, help="path to your pager binary")
    p.add_argument("--seed", type=int, default=1, help="seed of the first run (default 1)")
    p.add_argument("--runs", type=int, default=200, help="number of crash runs (default 200)")
    p.add_argument("--verbose", action="store_true", help="one line per run, plus every disk fault")
    p.add_argument("--keep-going", action="store_true", help="do not stop at the first failure")
    args = ap.parse_args(argv)
    return {"pager": grade_pager}[args.layer](args)
