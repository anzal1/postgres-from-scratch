"""B+tree leaf-level simulation: what UUIDv4 vs UUIDv7 vs a sequence cost on insert.

Model: ~290 keys per 8 KB leaf (16-byte uuid + tuple header + line pointer).
Splits are 50/50, except a split of the rightmost leaf leaves the left page
90% full (Postgres nbtree fillfactor rule). Internal pages are assumed cached.
Leaves go through an LRU buffer pool of POOL pages (Postgres uses clock-sweep;
LRU is close enough here). Touching a leaf that is not in the pool is a read.
A freshly split page is an allocation, not a read. Evicting a page is a write.
Not modelled: WAL, full-page images after checkpoints, internal pages, the heap.
v7 assumes one steady insert stream at ~10 keys per millisecond.
"""
import bisect, json, os, random, sys
from collections import OrderedDict

N = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
CAP, POOL, FILL = 290, 2000, 0.90
random.seed(1729)

def keys(kind):
    if kind == "seq":
        for i in range(N): yield i
    elif kind == "v4":
        for _ in range(N): yield random.getrandbits(128)
    else:  # v7: 48-bit ms timestamp, ~10 inserts per ms, random tail
        for i in range(N): yield ((1_700_000_000_000 + i // 10) << 80) | random.getrandbits(80)

def run(kind):
    seps, leaves, ids = [None], [[]], [0]   # seps[i] = min key of leaf i
    pool, nid = OrderedDict({0: True}), 1
    reads = allocs = writes = splits = 0
    curve = []
    def touch(pid, new=False):
        nonlocal reads, allocs, writes
        if pid in pool: pool.move_to_end(pid)
        else:
            if new: allocs += 1
            else: reads += 1
            pool[pid] = True
            if len(pool) > POOL:
                pool.popitem(last=False); writes += 1
    for n, k in enumerate(keys(kind), 1):
        i = max(0, bisect.bisect_right(seps, k, 1) - 1)
        leaf = leaves[i]; touch(ids[i]); bisect.insort(leaf, k)
        if len(leaf) > CAP:
            splits += 1
            cut = int(CAP * FILL) if i == len(leaves) - 1 else len(leaf) // 2
            right = leaf[cut:]; del leaf[cut:]
            leaves.insert(i + 1, right); seps.insert(i + 1, right[0]); ids.insert(i + 1, nid)
            touch(nid, new=True); nid += 1
        if n % (N // 200) == 0: curve.append([n, reads, len(leaves)])
    fills = [len(l) / CAP for l in leaves]
    return dict(kind=kind, leaves=len(leaves), splits=splits, reads=reads, allocs=allocs, writes=writes,
                avg_fill=round(sum(fills) / len(fills), 3), curve=curve,
                fill_sample=[round(f, 3) for f in fills[:: max(1, len(fills) // 400)]])

out = {k: run(k) for k in ("v4", "v7", "seq")}
out["params"] = dict(N=N, keys_per_leaf=CAP, pool_pages=POOL, rightmost_fill=FILL, seed=1729)
here = os.path.dirname(os.path.abspath(__file__))
json.dump(out, open(os.path.join(here, "uuid_sim.json"), "w"))
for k in ("v4", "v7", "seq"):
    r = out[k]; print(f"{k:4} leaves={r['leaves']:6} splits={r['splits']:6} fill={r['avg_fill']:.2f} reads={r['reads']:8} allocs={r['allocs']:6} writes={r['writes']:8}")
