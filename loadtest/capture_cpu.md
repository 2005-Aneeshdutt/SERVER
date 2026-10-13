# Capture CPU per request, measured in-process

_From `loadtest/profile_capture.py`, run inside the gateway container (its docstring has the
exact commands). 2 KB prompts packed with emails and phone numbers, the worst case for scrubbing.
Constant 30 requests/s for 30 s, one gateway process. Modes:_
- _`off`: capture disabled;_
- _`null`: events scrubbed and serialized, then discarded (no Kafka client);_
- _`on`: full capture to Kafka._

## Final setup: buffered consumer writing to a named Docker volume

| Round | off | null | on |
|---|---|---|---|
| 1 | 8.41 ms | 8.54 ms | 8.80 ms |
| 2 | 7.71 ms | 9.19 ms | 11.24 ms |
| 3 | 8.33 ms | 8.27 ms | 8.78 ms |

Rounds 1 and 3 agree: **capture costs ~+0.4 ms of CPU per request**. Round 2 is an outlier in
both capture modes. It's kept as measured; the host's OneDrive client was still busy during
this run (see below).

## How we got there (earlier configurations, same script)

**A. Consumer writing a small Parquet file per poll into a bind-mounted `./data/capture`:**

| Round | off | null | on |
|---|---|---|---|
| 1 | 7.97 ms | 8.42 ms | 13.40 ms |
| 2 | 7.86 ms | 7.79 ms | 13.20 ms |
| 3 | 7.50 ms | 8.05 ms | 13.20 ms |
| 4 | 7.85 ms | 7.93 ms | 13.51 ms |

**B. The same, with the consumer stopped** (the broker still receives every event):

| Round | off | null | on |
|---|---|---|---|
| 1 | 7.88 ms | 8.16 ms | 8.56 ms |
| 2 | 8.12 ms | 8.30 ms | 8.67 ms |
| 3 | 7.98 ms | 8.20 ms | 8.40 ms |

**Why A cost ~5 ms more:** not gateway code.
- A CPU-time profile of the `on` run showed aiokafka using ~0.4 s in total, while *every*
  function in the process ran slower.
- The consumer wrote one tiny Parquet file per poll (8,613 files from the load tests) into a
  bind mount inside a **OneDrive-synced** checkout. On the host, OneDrive then used ~1 CPU core
  syncing them, and that starved Docker Desktop's VM, gateway included.
- Fixes: the consumer now buffers (5,000 events or 60 s per file) and writes to a named Docker
  volume, outside any synced folder. That is the final table above.
