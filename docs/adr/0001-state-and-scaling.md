# ADR 0001 — State model and scaling

- **Status:** accepted; partially superseded — see Update
- **Date:** 2026-10-28

## Update (2026-10-28)

Step 1–2 of the migration path below shipped: `app/statebackend.py` adds a
`RedisBackend` (selected with `REDIS_URL`) that shares **RPM/TPM windows and
cooldowns** across instances, with `MemoryBackend` as the default. The limiter
is now `async` and runs against either backend; in-flight concurrency, latency
EMA and configuration remain in-process/on-disk.

Remaining before multi-instance can be called supported:

- **Shared configuration store** — each instance still reads its own SQLite
  file, so replicas would diverge on providers/keys/models.
- **Shared concurrency** — in-flight caps are per instance.
- Integration coverage of the two-instance cooldown case (unit-level today via
  `fakeredis`).

Until those land, the recommended topology remains one instance.

## Context

The router keeps its operational state in-process and persists configuration in
a single SQLite file:

- **Configuration** (providers, keys, models, targets, gateway keys, admin
  users, audit log) lives in SQLite (`DB_PATH`).
- **Runtime state** (sliding RPM/TPM windows, per-key cooldowns, in-flight
  concurrency, latency EMAs) lives in memory (`app/limiter.py`).
- **Logs** are buffered and flushed to SQLite.

This is simple, fast (no network on the hot path) and was measured at ~1–2%
latency overhead vs a direct call. It has one hard constraint: **the process is
the unit of coordination.** Two instances do not share rate/cooldown state, and
SQLite is single-writer.

## Decision

Run **one instance per deployment** (one worker, one replica). This is the
supported topology today.

Expose `/metrics` (Prometheus) so horizontal scaling can be revisited with
data. Document the constraint rather than imply multi-instance support.

## Why not "just add Redis" now

Moving limiter state to Redis is more than swapping a client:

1. **It is on the hot path.** `available/acquire/release/cool` run inside the
   async request for every upstream call. A synchronous Redis client would
   block the event loop; a correct implementation makes the limiter `async`
   and threads `await` through `router.execute`, the embeddings path and the
   admin reset — a broad refactor of the most latency-sensitive, best-tested
   code.
2. **Concurrency needs more than a counter.** In-flight caps must decrement on
   completion even if the client disconnects; that needs leases/TTLs and
   careful failure handling, not a naive `INCR`/`DECR`.
3. **Partial sharing is a trap.** Sharing cooldowns but not RPM windows gives
   false confidence and uneven limits. A shared store must cover RPM, TPM,
   cooldown and concurrency together.
4. **Correctness gates the metric.** Half-migrated state risks the exact
   behaviour (rate-limit accuracy) the service exists to provide.

## Consequences

- **Operational:** scale the gateway vertically; use a managed instance with a
  persistent disk for `DB_PATH`. Keep it warm (see README).
- **Correctness:** limits are exact within the single instance; there is no
  cross-instance coordination.
- **Data:** back up `DB_PATH` (or export config). `BOOTSTRAP_CONFIG` restores
  configuration on ephemeral filesystems.

## Migration path (when multi-instance is required)

1. Introduce an `async` state backend interface with two implementations:
   in-memory (default) and Redis (`redis.asyncio`).
2. Move **all** of RPM, TPM, cooldown and concurrency to the backend
   (sorted-set sliding windows for RPM/TPM; `SET … EX` for cooldowns; leased
   counters for concurrency).
3. Make the limiter `async`; await it in `router.execute`, embeddings and the
   admin reset. Keep the in-memory backend as the zero-dependency fast path.
4. Move configuration/audit/logs off SQLite onto a shared database, or keep a
   single writer for writes.
5. Add integration tests against `fakeredis`, plus a two-instance test that
   asserts a cooldown set by one instance is honoured by the other.

Until step 5 lands, the documented topology is single-instance.

## Alternatives considered

- **Sticky sessions in front of N instances.** Cooldown/limit state still isn't
  shared; a key cooled on instance A is still used by instance B. Rejected.
- **Stateless, no limits in the gateway.** Pushes all rate handling to
  providers and loses the core value. Rejected.
