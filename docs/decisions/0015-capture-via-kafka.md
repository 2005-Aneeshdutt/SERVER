# 0015: Capturing traffic for distillation through Kafka, best-effort and off the hot path

**Status:** accepted (M5). Code: `gateway/capture/`, `distill/consumer.py`, `distill/replay.py`.

## The problem
Distillation (M6–M12) needs the gateway's own traffic: the prompts, the strong model's answers,
and the metadata. That's a lot of data per request, and several things will read it:
- a writer that stores metadata in Postgres and content in Parquet;
- later, shadow comparison and drift monitoring;
- **rebuilding a dataset from scratch** after a validator or the PII scrubber changes.

At the same time, capture must never slow a request down or fail it (hard rule 2).

## Decision
**Hot path:** `_finish()` calls `CaptureProducer.submit()`, a `put_nowait` into a bounded
in-process queue. It does no serialization, no regexes and no I/O. A full queue drops the event
and counts it.

**Background task, same process:**
1. scrub PII (`gateway/capture/pii.py`);
2. compute fingerprints;
3. serialize the versioned event;
4. **batch**: up to 200 events or ~512 KB per Kafka message, as JSON lines, flushed every
   200 ms, then handed to `aiokafka` with `acks=1` and no waiting on delivery. It yields to the
   event loop between events, so building a batch never blocks requests.

Kafka down means events are dropped and counted, with one reconnect attempt every 5 s. Requests
are never affected; a unit test asserts exactly that.

**Kafka:** a single KRaft broker under the compose `distill` profile. Messages carry no key:
one message batches many API keys' events, so per-key ordering inside Kafka is not kept. Nothing
downstream needs it, because every event carries its id and timestamp. (The first version sent
one message per event keyed by API key; the cost below is why that changed.)

**Consumer:** at-least-once. It commits offsets only after Postgres and Parquet both succeed:
- Postgres rows are keyed on the event id (`ON CONFLICT DO NOTHING`), so a replay never
  duplicates rows. The integration test replays a whole topic from offset 0 into the same
  tables and finds 0 new rows.
- Parquet files are written atomically (write to a temp file, then rename). Readers still dedupe
  on event_id, because a replay with different batch boundaries can write an event twice.

## Privacy: opt-in and scrubbing before the event leaves the process
- Content (messages and response) is captured **only** for keys with `distill_opt_in = true`.
  Every other key produces metadata and fingerprints only. Both default to off.
- Scrubbing runs **inside the gateway, before Kafka**, so no raw content is ever written to
  Kafka, Postgres or Parquet. It replaces emails, phone numbers and Luhn-valid card numbers.
- It's basic, regex-based scrubbing. The test file lists what it misses (obfuscated emails,
  spelled-out numbers). Fine for this project's public, simulated data; not a compliance tool.

## Simulated traffic is labelled at the source
A key flag `simulated`, or the `X-Gateway-Simulated: 1` header, sets `simulated: true` on the
event. That flag then flows into the Postgres row and the Parquet partition path
(`simulated=true/`). Every number built from this data will say whether it's simulated.

## Why Kafka here, when it was rejected for request logs
For request logs, Kafka was considered and rejected (ADRs 0004 and 0009 cover what that
path needs instead). There a replicated log was only a durability fix, and it was
disproportionate at this scale. Here the reasons are different:
- **Fan-out:** several independent readers, each at its own pace.
- **Replay:** datasets must be re-buildable from the raw event stream.
- **Size:** keeping prompt and response payloads out of the hot Postgres tables.
Those are what a log is for.

**What it is not:** durable. One broker has no replication, and `acks=1` is acknowledged by that
broker alone. Capture is best-effort by design. Nothing on the money path (spend, limits)
depends on it.

## Rejected alternatives
- **Write content to Postgres directly from the gateway:** it puts DB writes of large payloads
  on every replica's background loop, and couples training data to the operational database.
- **Redis Streams:** that Redis holds the limiters and spend. Filling its memory with megabytes
  of prompts under `noeviction` would make the limiters fail closed.
- **Scrub in the consumer:** raw PII would then sit in Kafka for its retention period.

## Cost: what it took to get to "~0", and what's left
The first version measured badly. With k6, p50 overhead at 50 RPS went from ~5 ms to ~15 ms with
capture on. Profiling inside the container (`loadtest/profile_capture.py`) found three separate
causes:

1. **A pre-existing gateway bug: `sniffio` wasn't installed.** httpx and httpcore try
   `import sniffio` on every connection-pool operation. Each failed import scans `sys.path` on
   disk, which showed up as thousands of `posix.stat` calls per second. Installing it (a tiny
   pure-Python package) cut the gateway's **baseline** CPU per request, with or without capture.
2. **The scrubber.** Whole-text regexes ran at every character and backtracked. Rewritten as
   "search outwards from each `@`" plus a linear `\d+` scan: ~3-5x cheaper.
3. **One Kafka message per event.** aiokafka's per-message Python overhead dominated. Batching
   events into JSON-lines messages amortizes it.

After those fixes, a fourth problem showed up, and it was in the consumer, not the gateway:

4. **Tiny files into a synced folder.** The consumer wrote one Parquet file per poll into a
   bind-mounted `./data/capture`. In this checkout that folder is synced by OneDrive, which then
   spent ~1 CPU core uploading thousands of small files, starving Docker Desktop's VM. The
   consumer now buffers (one file per 5,000 events or 60 s; tiny files are a known data-lake
   anti-pattern anyway) and writes to a named Docker volume.

**Result** (`loadtest/RESULTS.md`, k6; `loadtest/capture_cpu.md`, in-process):
- k6, 1 replica, 30 and 50 RPS, three rounds: capture off, on, and on with the consumer paused
  are **indistinguishable within run-to-run noise**, in p50, p99 and gateway CPU per request.
- In-process (more sensitive): **~+0.4 ms of CPU per request** in the worst case (2 KB prompts
  packed with PII). One round of three was an outlier.

**Bottom line:** capture adds no wait to any request, because there's no I/O on the hot path.
Its CPU cost is ~0.4 ms per request in the worst case. That's invisible at moderate load, and
near saturation it would cost a few percent of capacity.
