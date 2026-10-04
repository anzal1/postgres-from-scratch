# Postgres From Scratch

Build a database that `psql` can connect to, starting from one empty file. Every layer comes with a grader that crashes your code on purpose.

![The layer 1 grader catching a pager that returns torn pages, then passing one that checksums them](assets/demo.gif)

Status: the layer 1 grader works today. Layers 2 to 4 are next, then the rest in order. Watch the repo to get each one as it lands.

## Outages you'll recreate

Each of these has bitten a real system. You'll cause every one of them in your own database, watch it happen, and then fix it.

- **A power cut leaves pages half written.** Wikimedia lost its master and four replicas this way in [2005](https://meta.wikimedia.org/wiki/February_2005_server_crash). You'll add checksums that catch it.
- **fsync says yes after the data is gone.** That's Postgres ["fsyncgate"](https://wiki.postgresql.org/wiki/Fsync_Errors), 2018. You'll order your log writes so a lie from the disk can't lose a commit.
- **The data outgrows memory and every query hits disk.** Foursquare was down for [11 hours](https://www.infoq.com/news/2010/10/4square_mongodb_outage/) in 2010 when one shard went past its RAM.
- **Two transactions are each correct and together aren't.** The [ACIDRain paper](https://www.bailis.org/papers/acidrain-sigmod2017.pdf) found 22 bugs like this in shopping apps running on over 2 million sites.
- **The planner guesses wrong and picks a plan that scans everything.** Figma had intermittent outages from exactly this in [2020](https://www.figma.com/blog/post-mortem-service-disruption-on-january-21-22-2020/).
- **A migration waits for a lock and every query queues behind it.** Honeycomb, [2024](https://status.honeycomb.io/incidents/ycgvhvs201q1).
- **A failover leaves two primaries.** GitHub was degraded for 24 hours in [2018](https://github.blog/news-insights/company-news/oct21-post-incident-analysis/).
- **Vacuum falls behind until the database stops taking writes.** Sentry in [2015](https://blog.sentry.io/transaction-id-wraparound-in-postgres/), Mailchimp in [2019](https://mailchimp.com/what-we-learned-from-the-recent-mandrill-outage/).

## The layers

Fork the repo and tick these off as your grader runs go green. The evening counts are rough, for someone who already writes code most days.

- [ ] **1. pager.** Fixed-size pages with checksums, on a disk that lies. About 2 evenings. `./grade pager`
- [ ] **2. buffer pool.** A page cache with pinning and eviction. About 3 evenings. `./grade pool`
- [ ] **3. B+tree.** The primary key index. About 5 evenings. `./grade btree`
- [ ] **4. WAL.** Write-ahead logging and crash recovery. About 6 evenings. `./grade wal`
- [ ] **5. MVCC.** Snapshots, isolation levels and vacuum. About 6 evenings. `./grade mvcc`
- [ ] **6. SQL.** Tokenizer, parser, catalog and binder. About 5 evenings. `./grade sql`
- [ ] **7. executor.** Scans, joins, sorts and aggregates that can spill to disk. About 5 evenings. `./grade exec`
- [ ] **8. planner.** Statistics, a cost model, join order and `EXPLAIN`. About 6 evenings. `./grade plan`
- [ ] **9. locks.** A lock manager, deadlock detection and online index builds. About 4 evenings. `./grade locks`
- [ ] **10. wire.** The Postgres v3 protocol, plus a connection pooler. About 4 evenings. `./grade wire`
- [ ] **11. replica.** Streaming your WAL to a second node. About 4 evenings. `./grade replica`
- [ ] **12. ORM.** A small ORM of your own, then a real one pointed at your server. About 4 evenings. `./grade orm`
- [ ] **13. the chaos run.** A real app on your database while the grader cuts power, adds latency and runs a migration at peak load. About 4 evenings. `./grade chaos`

That's around 58 evenings in total. The pager from layer 1 is still underneath when the ORM connects in layer 12.

## Where you end up

```
$ ./yourdb --data ./data --port 5433 &
$ psql -h localhost -p 5433
yourdb=> create table orders (id bigint primary key, total int);
CREATE TABLE
yourdb=> insert into orders values (1, 4200);
INSERT 0 1
yourdb=> \q
$ kill -9 %1
$ ./yourdb --data ./data --port 5433 &
$ psql -h localhost -p 5433 -c "select * from orders"
 id | total
----+-------
  1 |  4200
```

Your database survives being killed mid-write and speaks the Postgres wire protocol, so `psql`, Prisma and Drizzle connect to it without complaint.

## How the grader works

For layers 1 to 4 your binary doesn't write to a real file. It talks to a virtual disk that the grader runs, over a Unix socket whose path arrives in `DISK_SOCKET`. Because the grader owns the disk, it can behave like real hardware on a bad day. It can drop writes you never fsynced, apply them out of order, or tear a page so only some of its 512-byte sectors land. Then it kills your process with `SIGKILL`, restarts it, and checks what you return.

The grader sends commands on stdin, one per line (`write`, `sync`, `read`). The full protocol fits on one page in [grader/PROTOCOL.md](grader/PROTOCOL.md), and the fault model is in [grader/faults.md](grader/faults.md). Any language that can open a socket works.

```bash
./grade pager --bin ./yourdb                                  # 200 seeded crash runs
./grade pager --bin ./yourdb --seed 17 --runs 1 --verbose   # replay one failure
```

From layer 10 on, the grader connects over the Postgres protocol and sends SQL.

## Before you start

Try this warm-up first. Write a program that stores fixed-size records in a file and reads record N back by seeking straight to it. If that takes you an evening or less, you're ready for layer 1.

You'll want one systems language you're comfortable in. Rust, Go, Zig and C all work, because the grader only talks to your binary over stdin and a socket. A Mac or a Linux machine is enough, with no cloud account or GPU.

## Layer 1: pager

Covers what happens between `write()` returning and your data actually being safe.

- Block devices, sectors, the OS page cache, dirty pages and writeback
- `fsync`, `fdatasync` and `O_DIRECT`, and why `rename` alone isn't durable
- SSD internals: the flash translation layer, erase blocks and write amplification
- Torn writes, and checksums as the way to detect them

**Build.** A pager over the grader's virtual disk, with a checksum on every page and a header recording the format version. It must say `corrupt` for a torn page instead of returning what's there.

**Measure.** fsync latency on your machine. Then sequential against random 4 KB reads, with the page cache cold and warm. Keep the ratio, because your cost model in layer 8 uses it.

**Grader.** Writes thousands of pages and injects power loss at random points. Afterwards each page must hold its old contents, its new contents, or fail its checksum. Silent corruption fails the layer.

## Layer 2: buffer pool

The disk is slow, so you keep hot pages in memory, and now memory and disk can disagree.

- Slotted pages, tuple headers, variable-length rows and free-space tracking
- A buffer pool with pin counts and dirty bits
- Eviction with LRU, clock and LRU-K, and why one big table scan wrecks plain LRU
- Ring buffers for sequential scans, which is how Postgres protects its cache

**Build.** A heap file of rows on slotted pages, behind a buffer pool with a configurable size.

**Measure.** Hit rate for a point-lookup workload, then the same workload with a full table scan running beside it. Try plain LRU first, then a scan ring.

**Grader.** Random inserts, updates and deletes against a buffer pool smaller than the data, checked against a model. It also checks that no pinned page is evicted and no dirty page is lost.

## Layer 3: B+tree

The structure behind every index you've created.

- Search, insert, splits, deletes, merges and redistribution
- Range scans along leaf links
- Fill factor, and why a split costs two page writes plus a parent update
- Latch crabbing, single-threaded first and concurrent later

**Build.** A B+tree over your pages, used as the heap's primary key index.

**Measure.** Insert a million rows keyed by UUIDv4, then by UUIDv7, then by an integer sequence. Count page splits, tree height, pages touched per insert and buffer pool misses. A leaf-level simulation of this experiment lives in [experiments/uuid-btree](experiments/uuid-btree) if you want a preview of the answer.

**Grader.** Property tests against a sorted-map model, range scans included. After every operation it checks key order, height balance, minimum fill and sibling links.

## Layer 4: WAL

This is the layer where a committed transaction starts surviving a power cut.

- Log records, log sequence numbers, and the rule that the log reaches disk before the page does
- Redo and undo, with as much of ARIES as you need
- Checkpoints, which bound how long recovery takes
- Full-page writes, which fix torn pages
- Group commit, which raises throughput as more clients commit at once

**Build.** A WAL in front of the heap and the B+tree, recovery on startup, and checkpointing.

**Measure.** Commits per second with one fsync per commit, then with group commit at 1, 8 and 64 concurrent writers.

**Grader.** Ten thousand randomized crash schedules. After each restart every committed transaction is present, every uncommitted one is gone, and the B+tree still passes its structural checks.

## Layer 5: MVCC

With MVCC, readers never block writers. You pay for it with old row versions that someone has to clean up.

- Transaction IDs, `xmin` and `xmax`, and tuple versions
- Snapshots and visibility rules
- What read committed, repeatable read and serializable each allow
- Write skew, and why snapshot isolation isn't serializable
- Vacuum, dead tuples, bloat and transaction ID wraparound

**Build.** MVCC on your heap, snapshot isolation, and a vacuum process.

**Measure.** Reproduce write skew with the classic on-call doctors schedule, then fix it once with explicit row locks and once by detecting the dangerous read-write pattern. Separately, hold one transaction open for ten minutes under write load and watch the table grow, since vacuum can't remove anything that transaction might still see.

**Grader.** A history checker. It runs randomized concurrent transactions, records every read and write, and checks the history against the anomalies each isolation level forbids.

## Layer 6: SQL

Turns SQL text into a tree the rest of your database can work with.

- A tokenizer and recursive descent parser for a real SQL subset: `SELECT` with joins, `WHERE`, `GROUP BY`, `ORDER BY` and `LIMIT`, plus `INSERT`, `UPDATE`, `DELETE` and `CREATE TABLE`
- A system catalog stored in your own tables
- Binding, which covers name resolution, types and the error messages users see

**Build.** Parser, catalog and binder. Your database now accepts SQL over the line protocol.

**Grader.** A corpus of valid and invalid statements. Valid ones must produce the right tree, and invalid ones must fail with the right error class at the right position.

## Layer 7: executor

- The iterator model, with `open`, `next` and `close`
- Sequential, index and index-only scans
- Nested loop, hash and merge joins
- Sort and hash aggregate, spilling to disk when memory runs out

**Build.** An executor for every operator above, with a memory budget per query.

**Measure.** Run one join as nested loop, hash and merge across growing table sizes. Then give a hash join a memory budget smaller than its build side and watch it spill.

**Grader.** Differential testing. Random queries run on your database and on SQLite, and the results must match.

## Layer 8: planner

The planner decides whether your query is fast before a single row is read.

- Statistics, including row counts, histograms, most-common values and distinct estimates
- Cardinality estimation, and the independence assumption that breaks it
- A cost model built from your layer 1 measurements
- Join ordering, with dynamic programming for small joins and a greedy search for large ones
- `EXPLAIN` and `EXPLAIN ANALYZE`, showing estimated against actual rows

**Build.** A cost-based planner with `EXPLAIN ANALYZE`.

**Measure.** Create two correlated columns, like `city` and `zip`. Your planner will underestimate the matching rows by orders of magnitude and choose a nested loop that runs for minutes. Add multi-column statistics and run it again.

**Grader.** Checks that plans return correct results, and that on a fixed benchmark your plans stay within a set factor of the best plan an exhaustive search finds.

## Layer 9: locks

Lock queues are behind a lot of database postmortems, and the mechanics fit in one layer.

- Lock modes and the compatibility matrix
- The lock queue, and why one waiting lock blocks everything that arrives after it
- Deadlock detection with a wait-for graph
- Online schema changes, such as building an index without blocking writes

**Build.** A lock manager with table and row locks, a deadlock detector, `ALTER TABLE`, and `CREATE INDEX CONCURRENTLY`.

**Measure.** Start a slow `SELECT` and run `ALTER TABLE ADD COLUMN` behind it. Ordinary reads that arrive next queue behind the `ALTER`, which is itself waiting on the `SELECT`. Add a lock timeout and run it again.

**Grader.** Concurrent workloads must either finish or report a deadlock within a time limit. It fails silent hangs and any write lost during a concurrent index build.

## Layer 10: wire

After this layer, existing Postgres tools can connect to your database.

- The Postgres v3 protocol: startup, auth, simple query, row descriptions and data rows
- The extended protocol, with parse, bind, execute and prepared statements
- `COPY` for bulk loads
- What a connection costs when each one gets its own process, and why poolers exist
- Session pooling against transaction pooling, and what transaction pooling breaks

**Build.** A Postgres-compatible front end, then a small pooler in front of it.

**Milestone.** `psql -h localhost -p 5433` connects, creates a table and queries it.

**Measure.** Connection setup cost against query cost. Then 1,000 clients through your pooler against 1,000 direct connections.

**Grader.** Runs a standard Postgres driver's test suite against your server.

## Layer 11: replica

- Shipping the WAL you already have to a second node
- Streaming replication and replica lag
- Read-your-writes, and how a read replica breaks it
- Failover basics and split brain

**Build.** A streaming replica that replays your WAL and reports its lag.

**Measure.** Write to the primary and read from the replica in the same request, at rising load. Count how often a user doesn't see their own write.

**Grader.** Kills the primary at random points and checks that the replica never shows a transaction the primary didn't commit.

## Layer 12: ORM

- Query builders and the SQL they emit
- Identity map, unit of work, and lazy and eager loading
- N+1, and fixing it with `IN` lists, joins or the dataloader pattern
- Migrations, and which lock each kind takes

**Build.** A small ORM in any language. Then point a real ORM at your database.

**Measure.** Load the same page with lazy loading, eager loading and batching, through a proxy adding 1 ms, 20 ms and 80 ms of latency. Then run three common migrations against a table under write load and record how long each one blocks writers.

**Grader.** Runs a real ORM's integration tests against your server, plus a migration-under-load check.

## Layer 13: the chaos run

Take a small full-stack app that uses a real ORM and change its connection string to point at your database. Then run the chaos suite against it. The suite cuts the power, adds 80 ms of network latency, starts 200 concurrent writers, and runs a migration at peak load.

Publish what you measured: throughput, p99 latency, recovery time, replica lag, and what broke first.

You're done when the app survives the chaos suite and your write-up explains every failure it hit along the way.

## Where the format comes from

Building a whole system one layer at a time, each on top of the last, is the idea behind nand2tetris and geohot's fromthetransistor. The order of the storage layers follows Andy Pavlo's CMU 15-445 course. The grader's fault model borrows from research tools that crash real software the same way, especially ALICE, CrashMonkey and LazyFS.

## Further reading

- *Database Internals*, Alex Petrov
- Andy Pavlo's CMU 15-445 lectures
- *Designing Data-Intensive Applications*, Martin Kleppmann
- The Postgres source, especially `src/backend/access/heap` and `src/backend/storage`
- Jepsen's reports, for how real databases fail under faults

---

Postgres and PostgreSQL are trademarks or registered trademarks of the PostgreSQL Community Association of Canada. This project is not affiliated with or endorsed by the PostgreSQL Global Development Group.
