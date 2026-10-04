# Fault model

The virtual disk holds two things: a durable image, and a log of writes that have not been fsynced. This is the model of a disk with a volatile write cache, which is what most drives are.

## Normal operation

- WRITE appends to the log of unsynced writes.
- READ returns the newest data for the page, looking at the log first and the durable image second. A process sees its own writes whether or not they are durable.
- FSYNC applies every logged write to the durable image, in order, and empties the log.

## A crash

A crash is the grader sending SIGKILL to your process. It happens at the Nth WRITE or FSYNC request, where N is picked from the seed. The grader also picks, from the seed, whether the kill lands just before the disk applies that request or just after. Either way you never see the reply. If the process is still running after the whole workload, the grader kills it then.

After the kill, each unsynced write in the log is resolved on its own, using a random generator seeded from the run's seed:

| outcome | odds | what happens |
|---------|------|--------------|
| dropped | 35%  | the write never reaches the durable image |
| applied | 35%  | the whole 4096 bytes reach the image |
| torn    | 30%  | a random subset of 1 to 7 of the page's 8 sectors reach the image, and the rest of the page keeps what it had |

Writes already covered by an acknowledged FSYNC are never touched.

A torn write overlays whole 512 byte sectors. The sectors that did not land keep their previous contents, which are the older data or zeros. So a torn page is a patchwork of two versions of the page, and each sector on its own is intact.

## Reordering

With 50% probability the surviving writes (the applied and torn ones) are applied in a shuffled order instead of the order they were issued. This matters when two unsynced writes touch the same page. The older one can land last, so the page ends up holding a value that is older than another unsynced write, though never older than the last sync. Dropping some writes and applying later ones is also a reordering, and it happens whether or not the shuffle does.

## What a correct pager sees

For each page, afterwards, the durable image holds one of:

- the payload from the last acknowledged sync,
- the payload of an unsynced write issued after it, or
- a patchwork of those, which the pager must detect and report as `corrupt`.

A pager that returns the patchwork as valid data has silently corrupted the page, and fails.

## What the model leaves out

- Misdirected writes (data landing on the wrong page) and bit rot. A page checksum catches both, but this model does not inject them.
- Write ordering barriers. FSYNC here is a full barrier, as it is on a correctly working disk.
- A disk that lies about FSYNC. The model assumes FSYNC means durable.
- Torn writes inside a sector. A sector is atomic.

## Determinism

`Disk.crash(seed)` uses only `random.Random("crash:<seed>")`, so the same seed and the same unsynced writes always give the same image. The workload uses a separate generator seeded the same way, so a failing seed replays exactly, provided your binary sends the same disk requests for the same commands.
