# Grader protocols

Your binary talks to the grader in two ways. It sends commands and replies over stdin and stdout, and it does all of its disk I/O against a virtual disk that the grader owns. The virtual disk is how the grader gets to lie about fsync.

You can write the disk client in any language. In most it takes about 40 lines.

## The virtual disk

The grader sets the environment variable `DISK_SOCKET` to the path of a Unix domain socket. Connect to it once at startup and keep the connection open. Do not open files on the real filesystem for your page data. The disk starts empty on every run.

Sizes: a page is 4096 bytes, a sector is 512 bytes, and a page is 8 sectors. The disk promises that a single sector is written atomically, and nothing more.

All integers are big endian. Every message, in both directions, is a frame:

```
frame = length:u32  body[length]
```

Requests are strict request and reply. Send one frame, then read one frame, before sending the next.

### Requests

| op | name  | body                                   | what it does |
|----|-------|----------------------------------------|--------------|
| 1  | READ  | `01 page_no:u32`                       | returns the page |
| 2  | WRITE | `02 page_no:u32 data[4096]`            | writes a whole page |
| 3  | FSYNC | `03`                                   | makes every earlier write durable |
| 4  | SIZE  | `04`                                   | returns the file size in pages |

`page_no` is any value from 0 to 2^32 - 1. Pages are sparse. `data` is always exactly 4096 bytes, and partial pages are not allowed.

### Replies

```
ok     00 [result]
error  01 message (utf-8)
```

| request | result after the `00` byte |
|---------|----------------------------|
| READ    | 4096 bytes. A page that was never written reads as zeros. |
| WRITE   | nothing |
| FSYNC   | nothing |
| SIZE    | `pages:u32`, which is 1 + the highest page number ever written, or 0 for an empty disk |

A malformed request gets an error reply.

### What the disk promises

A WRITE that has been acknowledged is visible to later READs from the same process, like data in the OS page cache. It is not durable until a later FSYNC has been acknowledged. If the process is killed first, the write may be dropped, applied, or torn. The rules are in [faults.md](faults.md).

### Example in Go

```go
frame := binary.BigEndian.AppendUint32(nil, uint32(len(req)))
conn.Write(append(frame, req...))
io.ReadFull(conn, head[:])                       // 4 byte length
body := make([]byte, binary.BigEndian.Uint32(head[:]))
io.ReadFull(conn, body)                          // body[0] is the status
```

## The line protocol (layer 1)

The grader writes one command per line to your stdin and reads one reply line from your stdout. Flush stdout after every reply. Anything you write to stderr is ignored unless the grader runs with `--verbose`.

| command | reply |
|---------|-------|
| `write <page_no> <payload_hex>` | `ok` |
| `sync` | `ok`, sent only after everything written so far is durable |
| `read <page_no>` | the payload as hex, or `empty`, or `corrupt` |
| `exit` | no reply, the process exits |

Details:

- `page_no` is a decimal number from 0 to 63. The page numbers are yours to map onto disk pages however you like.
- `payload_hex` is 1 to 4000 bytes in lowercase hex, so there is room in each 4096 byte page for a header and a checksum.
- `read` returns the stored payload with its original length, not the padded page.
- `empty` means the page was never written. `corrupt` means the page fails your own integrity check. Reply `corrupt` only when you have a real reason to. The grader fails a pager that reports corrupt for a page no crash could have damaged.
- You may send the disk WRITE as soon as you get a `write`, or buffer and send it at `sync`, as long as `read` shows your own writes in the meantime.

## How a run goes

1. The grader starts your binary on an empty disk and sends a random mix of `write` and `sync` commands.
2. At a random point the grader SIGKILLs your process. This can happen in the middle of a command, between your disk requests.
3. The grader resolves every unsynced write on the disk with the fault model.
4. The grader starts your binary again on what is left, sends `read` for pages 0 to 63, and then `exit`.

A page passes if it reads back as the payload from the last acknowledged `sync`, or as any payload written after that sync, or as `corrupt` when a torn write could have damaged it. A page that was never synced may also read `empty`. Anything else fails, which covers silent corruption (data that was never written), a synced page that comes back empty, and a synced page that comes back older than its last sync.

## Running the grader

```
./grade pager --bin <path> [--seed N] [--runs N] [--verbose] [--keep-going]
```

- `--seed N` is the seed of the first run. Run `i` uses seed `N + i`. The default is 1.
- `--runs N` is the number of crash runs. The default is 200.
- `--verbose` prints one line per run and every disk fault, and passes your stderr through.
- `--keep-going` does not stop at the first failure.

The same seed against a deterministic binary replays the same workload and the same faults. A failure prints the command to replay it.
